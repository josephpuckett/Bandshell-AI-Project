#!/usr/bin/env bash
#
# install.sh — provision a Debian/Ubuntu host to run Bandshell Calendar.
#
# What it does (idempotent — safe to re-run):
#   1. apt: base tools, nginx, bubblewrap (script sandbox), Playwright's Chromium libs
#   2. installs `uv` for the app user (if missing)
#   3. `uv sync` in the project dir → ./.venv (CPython 3.12, from uv.lock)
#   4. downloads the Playwright Chromium build
#   5. writes an nginx site (HTTP + WebSocket upgrade, long read timeouts)
#   6. writes + enables a systemd unit that runs uvicorn on 127.0.0.1
#   7. writes /etc/bandshell/bandshell.env (template) + a sudoers rule so the app
#      can power the host off when idle (AUTO_SHUTDOWN_MINUTES, off by default)
#   8. runs smoke checks (app imports, headless Chromium launches, bwrap sandbox,
#      /api/health, and the auth probe must return 401)
#
# It does NOT touch your environment/secrets. Provide them in:
#     /etc/bandshell/bandshell.env      (KEY=VALUE lines, chmod 600)
# which the systemd unit loads (OPENROUTER_API_KEY, EVENTBRITE_TOKEN,
# GOOGLE_SERVICE_ACCOUNT_JSON, EVENTS_SPREADSHEET_ID, NGROK_*, ...).
# Pass a downloaded file with --env-file instead of hand-editing; Windows/Drive
# line endings (CRLF) and a UTF-8 BOM are normalised, and invalid lines are
# reported rather than silently breaking the service.
#
# Optional public URL: if the env file sets NGROK_URL + NGROK_AUTHTOKEN, this
# installs ngrok, authenticates it and runs `ngrok http <nginx port> --url <URL>`
# as a systemd unit (after the app). `--no-ngrok` skips it.
# The apt repo suite is fixed at 'bookworm' (what ngrok publishes — Debian 13
# "trixie" has no dist there), with --ngrok-repo-suite to override and a static
# tarball fallback if apt can't serve the package.
#
# Authentication: nginx HTTP Basic auth is ON by default (the app itself has no
# login). Anyone with the URL could otherwise read your events, spend your
# OpenRouter credit and drive the server's browser. Rotate with:
#     sudo htpasswd /etc/nginx/<site>.htpasswd <user>
#
# Usage:
#   sudo bash deploy/install.sh                 # defaults: this script's repo dir
#   sudo bash deploy/install.sh --auth-user steve     # prompted for a password
#   sudo bash deploy/install.sh --nginx-port 80       # default is 8080
#   sudo bash deploy/install.sh --port 9000           # uvicorn port
#   sudo bash deploy/install.sh --domain events.example.org --email you@example.org
#   sudo bash deploy/install.sh --env-file ~/bandshell.env   # secrets from a file
#   sudo bash deploy/install.sh --google-key ~/sheets-key.json
#   sudo bash deploy/install.sh --auto-shutdown 60    # power off after 60 min idle
#   sudo bash deploy/install.sh --no-auth       # NOT recommended; warns loudly
#   sudo bash deploy/install.sh --no-service --no-sandbox-test
#
# Assumes the project is already cloned (you said you'd handle that).
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# Defaults (override with flags or env vars)
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PORT="${PORT:-8000}"                 # uvicorn (loopback only)
NGINX_PORT="${NGINX_PORT:-8080}"     # nginx listen port (firewall/tunnel target)
SERVER_NAME="${SERVER_NAME:-_}"      # nginx server_name; "_" = catch-all
SITE_NAME="${SITE_NAME:-bandshell}"
DOMAIN="${DOMAIN:-}"                 # set → configure certbot TLS for this name
EMAIL="${EMAIL:-}"                   # required with DOMAIN
APP_USER="${APP_USER:-}"             # default: owner of the project directory
AUTH_ENABLED=1                       # nginx basic auth (strongly recommended)
AUTH_USER="${AUTH_USER:-bandshell}"  # login name
AUTH_PASS="${AUTH_PASS:-}"           # password (else prompted, or auto-generated)
AUTH_PASS_EXPLICIT=0                 # 1 when supplied via flag/env
INSTALL_SERVICE="${INSTALL_SERVICE:-1}"
RUN_SANDBOX_TEST="${RUN_SANDBOX_TEST:-1}"
BROWSER_CHANNEL="chromium"
AUTO_SHUTDOWN_MINUTES="${AUTO_SHUTDOWN_MINUTES:-0}"   # 0 = never (the app's default)
AUTO_SHUTDOWN_MINUTES_EXPLICIT=0      # 1 when --auto-shutdown was passed
ENV_FILE_SRC="${ENV_FILE_SRC:-}"      # --env-file: downloaded KEY=VALUE file to install
GOOGLE_KEY_SRC="${GOOGLE_KEY_SRC:-}"  # --google-key: service-account JSON to install
NGROK_ENABLED="${NGROK_ENABLED:-1}"   # 0 with --no-ngrok
NGROK_AUTHTOKEN="${NGROK_AUTHTOKEN:-}"   # else read from $ENV_FILE
NGROK_URL="${NGROK_URL:-}"               # else read from $ENV_FILE
# ngrok's apt repo only publishes bookworm/bullseye (dists/trixie/Release → 404),
# so the suite is HARDCODED — deriving it from /etc/os-release breaks on Debian 13.
# The agent is a static binary, so a bookworm package is fine on newer releases.
NGROK_REPO_SUITE="${NGROK_REPO_SUITE:-bookworm}"
NGROK_BIN_DIR="${NGROK_BIN_DIR:-/usr/local/bin}"   # where the tarball fallback goes
SUDO_SHUTDOWN=1                       # write the sudoers rule for idle shutdown

# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
c_reset=$'\033[0m'; c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'
c_yellow=$'\033[1;33m'; c_red=$'\033[1;31m'

step() { printf '\n%s==>%s %s\n' "$c_blue" "$c_reset" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '%s  !!%s %s\n' "$c_yellow" "$c_reset" "$*" >&2; }
die()  { printf '%s error:%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }

# Print the leading comment block as --help text (self-maintaining: stops at the
# first non-comment line, so editing the header can't truncate the help).
usage() {
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "${BASH_SOURCE[0]}"
    exit 0
}

# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
while [ $# -gt 0 ]; do
    case "$1" in
        --app-dir)        APP_DIR="${2:?}"; shift 2 ;;
        --port)           PORT="${2:?}"; shift 2 ;;
        --nginx-port)     NGINX_PORT="${2:?}"; shift 2 ;;
        --server-name)    SERVER_NAME="${2:?}"; shift 2 ;;
        --domain)         DOMAIN="${2:?}"; shift 2 ;;
        --email)          EMAIL="${2:?}"; shift 2 ;;
        --user)           APP_USER="${2:?}"; shift 2 ;;
        --site-name)      SITE_NAME="${2:?}"; shift 2 ;;
        --auth-user)      AUTH_USER="${2:?}"; shift 2 ;;
        --auth-pass)      AUTH_PASS="${2:?}"; AUTH_PASS_EXPLICIT=1; shift 2 ;;
        --no-auth)        AUTH_ENABLED=0; shift ;;
        --auto-shutdown)  AUTO_SHUTDOWN_MINUTES="${2:?}"; AUTO_SHUTDOWN_MINUTES_EXPLICIT=1; shift 2 ;;
        --env-file)       ENV_FILE_SRC="${2:?}"; shift 2 ;;
        --google-key)     GOOGLE_KEY_SRC="${2:?}"; shift 2 ;;
        --ngrok-url)      NGROK_URL="${2:?}"; shift 2 ;;
        --ngrok-authtoken) NGROK_AUTHTOKEN="${2:?}"; shift 2 ;;
        --no-ngrok)       NGROK_ENABLED=0; shift ;;
        --ngrok-repo-suite) NGROK_REPO_SUITE="${2:?}"; shift 2 ;;
        --no-shutdown-sudo) SUDO_SHUTDOWN=0; shift ;;
        --no-service)     INSTALL_SERVICE=0; shift ;;
        --no-sandbox-test) RUN_SANDBOX_TEST=0; shift ;;
        -h|--help)        usage ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "run me with sudo/root (I install apt packages and systemd units)"

APP_DIR="$(cd "$APP_DIR" && pwd)" || die "project dir not found: $APP_DIR"
[ -f "$APP_DIR/main.py" ] || die "$APP_DIR does not look like the project (no main.py)"
[ -f "$APP_DIR/uv.lock" ] || warn "no uv.lock in $APP_DIR — 'uv sync' will resolve fresh versions"

if [ -z "$APP_USER" ]; then
    APP_USER="$(stat -c '%U' "$APP_DIR")"
fi
id -u "$APP_USER" >/dev/null 2>&1 || die "no such user: $APP_USER"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
if [ -z "$APP_HOME" ] || [ ! -d "$APP_HOME" ]; then
    die "could not resolve a home directory for $APP_USER"
fi
if [ "$APP_USER" = "root" ]; then
    warn "project is owned by root; the service will run as root. Consider --user <name>"
fi

if [ -n "$DOMAIN" ] && [ -z "$EMAIL" ]; then
    die "--domain needs --email (for certbot registration)"
fi

if [ -n "${AUTH_PASSWORD:-}" ] && [ -z "$AUTH_PASS" ]; then
    AUTH_PASS="$AUTH_PASSWORD"; AUTH_PASS_EXPLICIT=1
fi
if [ "$AUTH_ENABLED" = "0" ] && [ "$AUTH_PASS_EXPLICIT" = "1" ]; then
    die "--no-auth conflicts with --auth-pass/AUTH_PASSWORD"
fi
HTPASSWD_FILE="/etc/nginx/${SITE_NAME}.htpasswd"
ENV_FILE="/etc/bandshell/bandshell.env"
# Resolve the power-off binary ONCE so the env file, the app's sudo command and
# the sudoers rule all name the same path (Debian ships /sbin/shutdown and
# /usr/sbin/shutdown; which one exists depends on usr-merge).
SHUTDOWN_BIN="$(command -v shutdown || echo /usr/sbin/shutdown)"
case "$AUTO_SHUTDOWN_MINUTES" in
    ''|*[!0-9.]*) die "--auto-shutdown expects minutes, e.g. 60 (or 0.5 to test)" ;;
esac
LIMIT_ZONE="${SITE_NAME//-/_}_cost"
PROXY_SNIPPET="/etc/nginx/snippets/${SITE_NAME}-proxy.conf"

as_user() { sudo -u "$APP_USER" -H -- "$@"; }
# Read a KEY's value out of the installed env file (used for NGROK_URL/TOKEN,
# which live in the same file as everything else). Last occurrence wins, and
# surrounding whitespace/quotes are stripped the way systemd would.
env_value() {
    awk -v k="$1" '
        { line=$0; sub(/^[[:space:]]+/, "", line) }
        line ~ ("^" k "=") {
            v=line; sub(/^[^=]*=/, "", v)
            sub(/[[:space:]]+$/, "", v)
            gsub(/^"|"$/, "", v)
            found=1
        }
        END { if (found) print v }
    ' "$ENV_FILE" 2>/dev/null || true
}

# Fallback installer for the ngrok agent (official static build). Used when apt
# can't serve a package — e.g. a distro whose codename ngrok hasn't published for.
# NGROK_BIN_DIR exists so this is testable without touching /usr/local/bin.
install_ngrok_tarball() {
    ngrok_tmp="$(mktemp -d)"
    if curl -sSL --max-time 300 -o "$ngrok_tmp/ngrok.tgz" \
            "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz" \
       && tar xzf "$ngrok_tmp/ngrok.tgz" -C "$ngrok_tmp" \
       && install -d -m 0755 "$NGROK_BIN_DIR" \
       && install -m 0755 "$ngrok_tmp/ngrok" "$NGROK_BIN_DIR/ngrok"; then
        rm -rf "$ngrok_tmp"
        ok "ngrok installed from the official tarball → $NGROK_BIN_DIR/ngrok ($("$NGROK_BIN_DIR/ngrok" --version 2>/dev/null | head -1))"
        return 0
    fi
    rm -rf "$ngrok_tmp"
    warn "could not install ngrok (apt suite '$NGROK_REPO_SUITE' unavailable and the"
    warn "  tarball download failed). Install it by hand from https://ngrok.com/download"
    warn "  and re-run this script."
    return 1
}

# JSON check that survives a missing jq (step 1 installs jq, but don't let an
# unrelated apt hiccup make a key-file install fail for the wrong reason).
json_ok() {
    if command -v jq >/dev/null 2>&1; then
        jq -e . "$1" >/dev/null 2>&1
    else
        "$APP_DIR/.venv/bin/python" -c 'import json,sys; json.load(open(sys.argv[1]))' \
            "$1" >/dev/null 2>&1
    fi
}
json_field() {  # file, dotted field name at the top level
    if command -v jq >/dev/null 2>&1; then
        jq -r ".$2 // \"?\"" "$1" 2>/dev/null
    else
        "$APP_DIR/.venv/bin/python" -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],"?") or "?")' \
            "$1" "$2" 2>/dev/null || echo '?'
    fi
}

# --------------------------------------------------------------------------- #
# 1. Base packages
# --------------------------------------------------------------------------- #
step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    ca-certificates curl git jq unzip sudo \
    nginx bubblewrap procps \
    >/dev/null
ok "nginx, bubblewrap and base tools present"

# Playwright's Chromium needs a pile of shared libraries. Let Playwright work out
# the right package names for this distro (falls back to the known Debian set).
step "Installing Chromium system libraries"
if ! apt-get install -y -qq libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libatspi2.0-0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
        libpango-1.0-0 libcairo2 fonts-liberation >/dev/null 2>&1; then
    warn "some Chromium libraries failed to install; continuing (Playwright install-deps will retry)"
fi
ok "Chromium libraries installed"

# --------------------------------------------------------------------------- #
# 2. uv (installed for the app user, so the venv is user-owned)
# --------------------------------------------------------------------------- #
step "Installing uv"
UV_BIN=""
if [ -x "$APP_HOME/.local/bin/uv" ]; then
    UV_BIN="$APP_HOME/.local/bin/uv"
    ok "uv already installed ($("$UV_BIN" --version 2>/dev/null || echo unknown))"
elif command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
    ok "using existing uv at $UV_BIN"
else
    as_user bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' >/dev/null
    UV_BIN="$APP_HOME/.local/bin/uv"
    [ -x "$UV_BIN" ] || die "uv install failed (expected $UV_BIN)"
    ok "uv installed ($("$UV_BIN" --version))"
fi

# --------------------------------------------------------------------------- #
# 3. Python dependencies (this also fetches CPython 3.12 if the distro has 3.11)
# --------------------------------------------------------------------------- #
step "Installing Python dependencies with uv"
cd "$APP_DIR"
if [ -f uv.lock ]; then
    as_user "$UV_BIN" sync --frozen || die "uv sync --frozen failed (lockfile out of date?)"
else
    as_user "$UV_BIN" sync || die "uv sync failed"
fi
[ -x "$APP_DIR/.venv/bin/python" ] || die ".venv was not created at $APP_DIR/.venv"
# The sandbox binds <repo>/.venv read-only, so the venv MUST live here.
ok "venv ready: $("$APP_DIR/.venv/bin/python" -V 2>&1)"

as_user mkdir -p "$APP_DIR/logs" "$APP_DIR/generated"
ok "runtime dirs writable (logs/, generated/)"

# --------------------------------------------------------------------------- #
# 4. Playwright Chromium (+ its OS deps, which need root)
# --------------------------------------------------------------------------- #
step "Installing Playwright Chromium"
PLAYWRIGHT="$APP_DIR/.venv/bin/playwright"
[ -x "$PLAYWRIGHT" ] || die "playwright executable missing from the venv"

# Browser binaries go into the *app user's* cache, so run as that user.
as_user "$PLAYWRIGHT" install "$BROWSER_CHANNEL" \
    || die "playwright install failed"
# install-deps shells out to apt-get, so it must run as root. Called by absolute
# path so uv is not involved and the venv is never rewritten.
"$PLAYWRIGHT" install-deps "$BROWSER_CHANNEL" >/dev/null 2>&1 \
    || warn "playwright install-deps reported a problem (run it manually if the browser fails to start)"
ok "Chromium installed for $APP_USER"

# --------------------------------------------------------------------------- #
# 5. nginx
# --------------------------------------------------------------------------- #
step "Configuring nginx"
NGINX_CONF="/etc/nginx/sites-available/${SITE_NAME}.conf"
if [ -n "$DOMAIN" ]; then
    SERVER_NAME="$DOMAIN"
fi

# --- credentials ----------------------------------------------------------- #
if [ "$AUTH_ENABLED" = "1" ]; then
    step "Configuring web login (HTTP Basic auth)"
    apt-get install -y -qq apache2-utils >/dev/null

    # Reuse existing credentials when the user is already in the file and no new
    # password was supplied, so re-running the installer doesn't silently
    # change (and lock people out of) a working password.
    existing=0
    if [ -f "$HTPASSWD_FILE" ] && grep -q "^${AUTH_USER}:" "$HTPASSWD_FILE"; then
        existing=1
    fi

    if [ -z "$AUTH_PASS" ] && [ "$existing" = "1" ]; then
        ok "keeping the existing password for user '$AUTH_USER' in $HTPASSWD_FILE"
    else
        if [ -z "$AUTH_PASS" ]; then
            if [ -t 0 ] && [ -t 2 ]; then
                while :; do
                    printf '  Password for web login (%s): ' "$AUTH_USER" >&2
                    read -rs AUTH_PASS; printf '\n' >&2
                    printf '  Repeat password: ' >&2
                    read -rs confirm; printf '\n' >&2
                    [ "$AUTH_PASS" = "$confirm" ] && [ -n "$AUTH_PASS" ] && break
                    warn "passwords did not match (or were empty) — try again"
                done
            else
                # Non-interactive install: generate one and show it exactly once.
                AUTH_PASS="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-20)"
                AUTH_GENERATED=1
            fi
        fi
        [ -n "$AUTH_PASS" ] || die "empty password"
        if [ -f "$HTPASSWD_FILE" ]; then
            printf '%s\n' "$AUTH_PASS" | htpasswd -i "$HTPASSWD_FILE" "$AUTH_USER" >/dev/null
        else
            printf '%s\n' "$AUTH_PASS" | htpasswd -i -c "$HTPASSWD_FILE" "$AUTH_USER" >/dev/null
        fi
        chown root:www-data "$HTPASSWD_FILE" 2>/dev/null || true
        chmod 640 "$HTPASSWD_FILE"
        ok "web login configured for user '$AUTH_USER' (file: $HTPASSWD_FILE)"
    fi

    # The file has to exist before nginx starts serving this site.
    [ -s "$HTPASSWD_FILE" ] || die "missing $HTPASSWD_FILE"
    if [ -z "$DOMAIN" ]; then
        warn "Basic auth over plain HTTP sends the password in clear text."
        warn "Use --domain <name> (TLS via certbot) or a tunnel for anything public."
    fi
else
    warn "=============================================================="
    warn " Running WITHOUT authentication (--no-auth)."
    warn " Anyone who reaches this host can read your events/settings,"
    warn " spend your OpenRouter credit, drive the browser, and create"
    warn " Eventbrite drafts. Only do this on a private network."
    warn "=============================================================="
fi

# --- shared proxy settings (included by both locations) -------------------- #
install -d -m 0755 /etc/nginx/snippets
{
    printf '%s\n' "# Managed by Bandshell Calendar deploy/install.sh — shared proxy settings."
    printf '%s\n' "proxy_pass http://127.0.0.1:${PORT};"
    printf '%s\n' "proxy_http_version 1.1;"
    printf '%s\n' "proxy_set_header Host              \$host;"
    printf '%s\n' "proxy_set_header X-Real-IP         \$remote_addr;"
    printf '%s\n' "proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;"
    printf '%s\n' "proxy_set_header X-Forwarded-Proto \$scheme;"
    printf '%s\n' ""
    printf '%s\n' "# WebSocket pass-through (the live browser view)."
    printf '%s\n' "proxy_set_header Upgrade    \$http_upgrade;"
    printf '%s\n' "proxy_set_header Connection \$connection_upgrade;"
    printf '%s\n' ""
    printf '%s\n' "# Stream frames straight through instead of buffering them."
    printf '%s\n' "proxy_buffering off;"
    printf '%s\n' ""
    printf '%s\n' "# Long-lived streams: an onboarding/review session can idle for a while."
    printf '%s\n' "proxy_read_timeout 3600s;"
    printf '%s\n' "proxy_send_timeout 3600s;"
    if [ "$AUTH_ENABLED" = "1" ]; then
        printf '%s\n' ""
        printf '%s\n' "# The app has no login of its own — protect every route, including the"
        printf '%s\n' "# WebSocket upgrade (browsers replay cached credentials on it)."
        printf '%s\n' "auth_basic           \"Bandshell Calendar\";"
        printf '%s\n' "auth_basic_user_file ${HTPASSWD_FILE};"
    fi
} >"$PROXY_SNIPPET"

# --- site ------------------------------------------------------------------ #
cat >"$NGINX_CONF" <<NGINX
# Managed by Bandshell Calendar deploy/install.sh — re-run the script to update.
#
# The embedded live view is a WebSocket carrying JPEG frames (plus clicks/typing
# from the human). Without the Upgrade/Connection headers it silently 404s or
# closes with code 1006, while ordinary pages work fine.
map \$http_upgrade \$connection_upgrade {
    default upgrade;
    ''      close;
}

# Throttle the endpoints that spend money / take over the browser, so a stolen
# password (or a bored visitor) can't queue up expensive agent runs.
limit_req_zone \$binary_remote_addr zone=${LIMIT_ZONE}:10m rate=6r/m;

server {
    listen ${NGINX_PORT};
    listen [::]:${NGINX_PORT};
    server_name ${SERVER_NAME};

    # Far more than a browser tab needs; the JSON posts are tiny.
    client_max_body_size 8m;

    location / {
        include ${PROXY_SNIPPET};
    }

    # Onboarding runs an LLM agent; /run drives the browser. Regex locations are
    # matched before the prefix above, so these get the same proxy/auth settings
    # plus the rate limit.
    location ~ ^/api/websites/[^/]+/(onboard|run)\$ {
        limit_req zone=${LIMIT_ZONE} burst=3 nodelay;
        include ${PROXY_SNIPPET};
    }
}
NGINX

if [ -n "$DOMAIN" ]; then
    apt-get install -y -qq certbot python3-certbot-nginx >/dev/null || \
        warn "could not install certbot; run it manually for TLS"
fi
ln -sfn "$NGINX_CONF" "/etc/nginx/sites-enabled/${SITE_NAME}.conf"
# The distro default claims "server_name _;" on :80 and would shadow us — but
# only when we listen on :80 ourselves (on 8080 it's harmless, and a stray
# welcome page there is better than nothing).
if [ "$NGINX_PORT" = "80" ] && [ -L /etc/nginx/sites-enabled/default ]; then
    rm -f /etc/nginx/sites-enabled/default
    ok "disabled the default nginx site"
fi
# --------------------------------------------------------------------------- #
# Detect configs that would shadow this one BEFORE reloading: a duplicate
# `listen :PORT` makes the reload fail and leaves the OLD (unauthenticated)
# site serving, which looks exactly like "the auth isn't being used".
# --------------------------------------------------------------------------- #
VERIFY_FAILED=0
conflicts=""
for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
    [ -e "$f" ] || continue
    [ "$(readlink -f "$f")" = "$(readlink -f "$NGINX_CONF")" ] && continue
    if grep -qsE "proxy_pass[[:space:]]+https?://(127\.0\.0\.1|localhost):${PORT}([^0-9]|$)" "$f" \
       || grep -qsE "listen[[:space:]]+([0-9.\[\]*:)?${NGINX_PORT}([^0-9]|$)" "$f"; then
        conflicts="$conflicts $f"
    fi
done
if [ -n "$conflicts" ]; then
    warn "these nginx configs shadow this site (same listen port, or proxying to"
    warn "the app port ${PORT}) — they must go, or nginx will keep serving them:"
    for f in $conflicts; do warn "    $f"; done
    for f in $conflicts; do warn "    sudo rm -f $f && sudo systemctl reload nginx"; done
    VERIFY_FAILED=1
fi

nginx -t -q || die "nginx config test failed (see $NGINX_CONF)"
if systemctl reload nginx 2>/dev/null || systemctl restart nginx 2>/dev/null; then
    ok "nginx serving ${SERVER_NAME} on :${NGINX_PORT} → 127.0.0.1:${PORT}"
else
    warn "nginx FAILED to reload — the old config is still serving."
    warn "  usually another block already listens on :${NGINX_PORT}:"
    warn "    sudo nginx -t; sudo ss -ltnp | grep ':${NGINX_PORT}'"
    VERIFY_FAILED=1
fi

# --------------------------------------------------------------------------- #
# Verify the login is actually in force.
# --------------------------------------------------------------------------- #
if [ "$AUTH_ENABLED" = "1" ]; then
    if [ "$SERVER_NAME" = "_" ]; then
        probe_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
                      "http://127.0.0.1:${NGINX_PORT}/" 2>/dev/null || echo 000)"
    else
        probe_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
                      -H "Host: ${SERVER_NAME}" "http://127.0.0.1:${NGINX_PORT}/" 2>/dev/null || echo 000)"
    fi
    case "$probe_code" in
        401) ok "verified: login is enforced (no credentials → HTTP 401)" ;;
        200) warn "nginx served the app WITHOUT asking for a password (HTTP 200)!"
             warn "  another server block is answering on :${NGINX_PORT}"
             VERIFY_FAILED=1 ;;
        *)   warn "could not verify the login through nginx (HTTP $probe_code)"
             warn "  is nginx really listening on :${NGINX_PORT}? check: sudo ss -ltnp | grep ':${NGINX_PORT}'"
             VERIFY_FAILED=1 ;;
    esac
else
    warn "auth disabled (--no-auth): anything that reaches nginx gets full access"
fi

# --------------------------------------------------------------------------- #
# 5b. Environment file (secrets & settings for the service)
# --------------------------------------------------------------------------- #
step "Environment file"
install -d -m 0755 /etc/bandshell
# systemd does NOT inherit the shell you ran this script from, and the app reads
# os.getenv() at import time — so this file is the ONLY place the service picks
# values up from. (A .env file in the repo is not read.)

# --- install a downloaded env file (--env-file) ----------------------------
if [ -n "$ENV_FILE_SRC" ]; then
    [ -r "$ENV_FILE_SRC" ] || die "cannot read --env-file $ENV_FILE_SRC"
    tmp_env="$(mktemp)"
    # Normalise what a download usually brings: a UTF-8 BOM and CRLF line endings.
    # A stray \r silently becomes part of the last value (e.g. the API key), which
    # fails much later with a confusing auth error — strip it here.
    sed -e '1s/^\xEF\xBB\xBF//' -e 's/\r$//' "$ENV_FILE_SRC" >"$tmp_env"
    # 'export FOO=bar' is shell syntax, not systemd syntax: warn on the ORIGINAL
    # file (the filter below drops those lines, so checking afterwards is useless).
    if grep -qE '^[[:space:]]*export[[:space:]]' "$tmp_env"; then
        warn "$ENV_FILE_SRC contains 'export' lines — systemd takes the file literally,"
        warn "  so they are dropped. Use plain KEY=VALUE (no export, no quotes)."
    fi
    # systemd's EnvironmentFile has NO inline comments: "X=30  # thirty" makes the
    # value "30  # thirty" (which then fails numeric parsing or silently disables a
    # feature). Point it out rather than guessing which part is the value.
    inline_comments="$(grep -nE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=[^#]*[[:space:]]#' "$tmp_env" || true)"
    if [ -n "$inline_comments" ]; then
        warn "these lines have a trailing '# comment', which systemd keeps as part of the VALUE:"
        printf '%s\n' "$inline_comments" | head -10 | sed 's/^/      /' >&2
        warn "  EnvironmentFile has no inline comments — move the comment to its own line."
    fi
    if grep -qE "^[^#]*=[\"']" "$tmp_env"; then
        warn "$ENV_FILE_SRC appears to quote a value; systemd keeps the quote characters."
    fi
    # Keep only blank/comment/KEY=VALUE lines; show anything else instead of
    # installing a file that systemd would choke on.
    bad_lines="$(grep -nvE '^[[:space:]]*(#|$)' "$tmp_env" \
                 | grep -vE '^[0-9]+:[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=' || true)"
    if [ -n "$bad_lines" ]; then
        warn "these lines in $ENV_FILE_SRC are not KEY=VALUE (skipped):"
        printf '%s\n' "$bad_lines" | sed 's/^/      /' >&2
        grep -E '^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=|#|$)' "$tmp_env" >"$tmp_env.keep" || true
        mv "$tmp_env.keep" "$tmp_env"
    fi
    if [ -f "$ENV_FILE" ]; then
        cp -p "$ENV_FILE" "${ENV_FILE}.bak"
        ok "existing env file backed up to ${ENV_FILE}.bak"
    fi
    install -m 0600 "$tmp_env" "$ENV_FILE"
    rm -f "$tmp_env"
    chown root:root "$ENV_FILE" 2>/dev/null || true
    ok "installed secrets from $ENV_FILE_SRC ($(grep -cE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE") keys, mode 600)"
fi

# Create the environment file rather than just nagging about it, if it's absent.
if [ ! -f "$ENV_FILE" ]; then
    cat >"$ENV_FILE" <<'ENVEOF'
# Bandshell Calendar — read by the systemd unit (EnvironmentFile=).
#
# Format: KEY=VALUE, one per line. NOTE: this is NOT a shell script — do not use
# `export`, quotes, or $VAR expansion; systemd takes the text literally.
#
# Required for any AI action:
# OPENROUTER_API_KEY=sk-or-v1-...
#
# Optional:
# OPENROUTER_FALLBACK_MODELS=openai/gpt-4o-mini,mistralai/mistral-small-3.1-24b-instruct
# EVENTBRITE_TOKEN=
# GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account", ...}    # inline JSON or a path
# EVENTS_SPREADSHEET_ID=
# SCRIPT_SANDBOX=1          # auto|1|0 — 1 makes a broken sandbox fail loudly
# SANDBOX_TIMEOUT=120
# HEADLESS=1
# BROWSER=chromium
# BROWSER_FPS=4
# BROWSER_JPEG_QUALITY=80
#
# Optional public URL (ngrok). Set BOTH to have the installer set up the tunnel:
# NGROK_URL=https://your-name.ngrok-free.app     # a domain reserved in ngrok
# NGROK_AUTHTOKEN=2abc...                        # ngrok dashboard → authtoken
# (idle-shutdown keys are appended below, expanded with resolved paths)
ENVEOF
    # Tolerate a non-root run (e.g. local testing); the mode is the important bit.
    chown root:root "$ENV_FILE" 2>/dev/null || true
    chmod 600 "$ENV_FILE" || warn "could not chmod 600 $ENV_FILE"
    ok "created $ENV_FILE (mode 600) — put OPENROUTER_API_KEY in it"
fi

# Idle shutdown: append the keys with the resolved power-off path, so the env
# file and the sudoers rule (5c) always agree. An existing value is kept.
if ! grep -qE "^[[:space:]]*AUTO_SHUTDOWN_COMMAND=" "$ENV_FILE"; then
    {
        printf '\n%s\n' "# Power the host off after nobody has used the app for a while (0 = disabled)."
        printf '%s\n' "# Fractions are allowed (0.5 = 30s) which makes the timer easy to test. The UI's"
        printf '%s\n' "# 1s polls do NOT count as activity, so a forgotten tab can't keep a VM billing."
        printf '%s\n' "# Verify with AUTO_SHUTDOWN_DRY_RUN=1 (logs the decision, doesn't power off)."
        # NEVER append a second AUTO_SHUTDOWN_MINUTES: systemd takes the LAST value,
        # so a default here would silently override the downloaded file's setting.
        if grep -qE "^[[:space:]]*AUTO_SHUTDOWN_MINUTES=" "$ENV_FILE"; then
            printf '%s\n' "# (AUTO_SHUTDOWN_MINUTES is already set above — left as-is)"
        else
            printf 'AUTO_SHUTDOWN_MINUTES=%s\n' "$AUTO_SHUTDOWN_MINUTES"
        fi
        printf '%s\n' "# AUTO_SHUTDOWN_MAX_HOURS=0        # force off after N h uptime even if \"busy\""
        printf '%s\n' "# AUTO_SHUTDOWN_DRY_RUN=1"
        printf '%s\n' "# AUTO_SHUTDOWN_CHECK_SECONDS=30"
        printf 'AUTO_SHUTDOWN_COMMAND=sudo -n %s -h now\n' "$SHUTDOWN_BIN"
    } >>"$ENV_FILE"
fi
if [ "$AUTO_SHUTDOWN_MINUTES_EXPLICIT" = "1" ]; then
    # --auto-shutdown N: set the value (and un-comment it if it was disabled).
    if grep -qE "^[[:space:]]*AUTO_SHUTDOWN_MINUTES=" "$ENV_FILE"; then
        sed -i "s|^[[:space:]]*AUTO_SHUTDOWN_MINUTES=.*|AUTO_SHUTDOWN_MINUTES=$AUTO_SHUTDOWN_MINUTES|" "$ENV_FILE"
    else
        printf 'AUTO_SHUTDOWN_MINUTES=%s\n' "$AUTO_SHUTDOWN_MINUTES" >>"$ENV_FILE"
    fi
    ok "idle shutdown: power off after ${AUTO_SHUTDOWN_MINUTES} min of inactivity"
fi

# --- Google service-account key (--google-key) -----------------------------
GOOGLE_KEY_DEST="/etc/bandshell/google-service-account.json"
if [ -n "$GOOGLE_KEY_SRC" ]; then
    [ -r "$GOOGLE_KEY_SRC" ] || die "cannot read --google-key $GOOGLE_KEY_SRC"
    install -m 0640 "$GOOGLE_KEY_SRC" "$GOOGLE_KEY_DEST"
    # The app process (running as the app user) reads this at runtime, unlike the
    # env file which only systemd/root reads — so group-read for the app's group.
    chown root:"$(id -gn "$APP_USER")" "$GOOGLE_KEY_DEST" 2>/dev/null || true
    chmod 0640 "$GOOGLE_KEY_DEST" 2>/dev/null || true
    if json_ok "$GOOGLE_KEY_DEST"; then
        ok "google key installed: $GOOGLE_KEY_DEST (0640, owner root:$(id -gn "$APP_USER"))"
    else
        rm -f "$GOOGLE_KEY_DEST"
        die "$GOOGLE_KEY_SRC is not valid JSON — expected a service-account key file"
    fi
    ok "  client_email: $(json_field "$GOOGLE_KEY_DEST" client_email)"
    if ! grep -qE '^[[:space:]]*GOOGLE_SERVICE_ACCOUNT_JSON=' "$ENV_FILE"; then
        printf 'GOOGLE_SERVICE_ACCOUNT_JSON=%s\n' "$GOOGLE_KEY_DEST" >>"$ENV_FILE"
        ok "  set GOOGLE_SERVICE_ACCOUNT_JSON to that path"
    else
        existing_key="$(env_value GOOGLE_SERVICE_ACCOUNT_JSON)"
        if [ "$existing_key" != "$GOOGLE_KEY_DEST" ]; then
            warn "the env file already sets GOOGLE_SERVICE_ACCOUNT_JSON to something else:"
            warn "  $existing_key"
            warn "  leaving it as-is; delete that line to use $GOOGLE_KEY_DEST instead."
        fi
    fi
fi

# Sanity-check the storage key that IS configured, so a broken path is caught now
# instead of at the first save.
gkey="$(env_value GOOGLE_SERVICE_ACCOUNT_JSON)"
if [ -n "$gkey" ]; then
    case "$gkey" in
        /*)
            if [ ! -r "$gkey" ]; then
                warn "GOOGLE_SERVICE_ACCOUNT_JSON points at $gkey, which is not readable."
                warn "  Google Sheets storage will stay disabled until that's fixed."
            elif ! json_ok "$gkey"; then
                warn "GOOGLE_SERVICE_ACCOUNT_JSON points at $gkey, which is not valid JSON."
            fi ;;
        \{*)
            inline_tmp="$(mktemp)"
            printf '%s' "$gkey" >"$inline_tmp"
            if ! json_ok "$inline_tmp"; then
                rm -f "$inline_tmp"
                warn "GOOGLE_SERVICE_ACCOUNT_JSON looks like inline JSON but does not parse."
                warn "  Inline JSON in an EnvironmentFile is fragile — prefer a file path."
            fi
            rm -f "$inline_tmp" ;;
        *)
            warn "GOOGLE_SERVICE_ACCOUNT_JSON is neither a path nor JSON — Sheets storage"
            warn "  will stay disabled (use --google-key to install the file)." ;;
    esac
fi
if [ -n "$(env_value EVENTS_SPREADSHEET_ID)" ] && [ -z "$gkey" ]; then
    warn "EVENTS_SPREADSHEET_ID is set but GOOGLE_SERVICE_ACCOUNT_JSON is not —"
    warn "  persistence needs both (pass --google-key with the key file)."
fi

# Did the caller export keys in their shell, expecting the service to see them?
if [ -n "${OPENROUTER_API_KEY:-}" ] && ! grep -qE "^[[:space:]]*OPENROUTER_API_KEY=[^[:space:]#]" "$ENV_FILE"; then
    warn "OPENROUTER_API_KEY is set in YOUR shell, but systemd does not inherit it."
    warn "  Put it in $ENV_FILE instead, then: sudo systemctl restart bandshell"
fi
if ! grep -qE "^[[:space:]]*OPENROUTER_API_KEY=[^[:space:]#]" "$ENV_FILE"; then
    warn "$ENV_FILE has no OPENROUTER_API_KEY yet — the UI will load,"
    warn "  but every AI action (onboarding) will fail until you add it."
fi

# --------------------------------------------------------------------------- #
# 5c. Sudoers rule for idle shutdown
# --------------------------------------------------------------------------- #
step "Idle-shutdown privilege"
if [ "$SUDO_SHUTDOWN" = "1" ]; then
    SUDOERS_FILE="/etc/sudoers.d/${SITE_NAME}-shutdown"
    cat >"$SUDOERS_FILE.tmp" <<SUDOEOF
# Managed by Bandshell Calendar deploy/install.sh — lets the app power this host
# off when idle (AUTO_SHUTDOWN_MINUTES in $ENV_FILE).
#
# Only these exact commands, only for the service user, only without a password:
# the app runs "sudo -n $SHUTDOWN_BIN -h now" (see idle.py). Nothing else is granted.
# NOTE: no backticks in this heredoc — it is unquoted, so they would be executed.
$APP_USER ALL=(root) NOPASSWD: $SHUTDOWN_BIN -h now, /usr/sbin/poweroff, /sbin/poweroff
SUDOEOF
    if visudo -c -f "$SUDOERS_FILE.tmp" >/dev/null 2>&1; then
        # sudoers.d ignores filenames containing a '.' and requires 0440 root:root.
        install -m 0440 "$SUDOERS_FILE.tmp" "$SUDOERS_FILE" 2>/dev/null \
            || cp -f "$SUDOERS_FILE.tmp" "$SUDOERS_FILE"
        rm -f "$SUDOERS_FILE.tmp"
        chown root:root "$SUDOERS_FILE" 2>/dev/null || true
        chmod 0440 "$SUDOERS_FILE" 2>/dev/null || true
        ok "sudoers: $APP_USER may run '$SHUTDOWN_BIN -h now' without a password"
    else
        rm -f "$SUDOERS_FILE.tmp"
        warn "could not validate the sudoers rule for $APP_USER — idle shutdown will"
        warn "  log a permission error instead of powering off (everything else works)."
    fi
    # A sudoers file owned by anyone but root is IGNORED by sudo (silently).
    if [ -f "$SUDOERS_FILE" ] && [ "$(stat -c '%U:%a' "$SUDOERS_FILE" 2>/dev/null)" != "root:440" ]; then
        warn "  $SUDOERS_FILE is $(stat -c '%U %a' "$SUDOERS_FILE" 2>/dev/null) —"
        warn "  sudo needs root:root 0440. Fix with: sudo chown root:root $SUDOERS_FILE && sudo chmod 440 $SUDOERS_FILE"
    fi
else
    warn "skipped the sudoers rule (--no-shutdown-sudo): idle shutdown will not work"
fi

# --------------------------------------------------------------------------- #
# 6. systemd service
# --------------------------------------------------------------------------- #
if [ "$INSTALL_SERVICE" = "1" ]; then
    step "Installing systemd service"
    install -d -m 0755 /etc/bandshell
    cat >/etc/systemd/system/bandshell.service <<UNIT
[Unit]
Description=Bandshell Calendar (FastAPI + Playwright)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=$(id -gn "$APP_USER")
WorkingDirectory=${APP_DIR}

# Defaults; anything in the env file below wins (it is read afterwards).
Environment=HEADLESS=1
Environment=BROWSER=chromium
Environment=BROWSER_FPS=4
Environment=PYTHONUNBUFFERED=1
# Secrets & settings — see deploy/bandshell.env.example
EnvironmentFile=-/etc/bandshell/bandshell.env

# --workers 1 is REQUIRED: all state lives in this process (and one browser).
# --proxy-headers so X-Forwarded-Proto from nginx is honoured.
# --timeout-graceful-shutdown bounds SIGTERM handling; an open WebSocket would
# otherwise make uvicorn wait indefinitely (systemd kills at TimeoutStopSec).
ExecStart=${APP_DIR}/.venv/bin/uvicorn main:app \\
    --host 127.0.0.1 --port ${PORT} \\
    --workers 1 \\
    --proxy-headers --forwarded-allow-ips=127.0.0.1 \\
    --timeout-graceful-shutdown 10

Restart=always
RestartSec=3
TimeoutStopSec=60

# NOTE: deliberately NOT set NoNewPrivileges / ProtectSystem: the script sandbox
# needs unprivileged user namespaces (bwrap) and the app writes logs/ + generated/.

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable bandshell.service >/dev/null
    ok "bandshell.service enabled (starts on boot)"

    systemctl restart bandshell.service || true
fi

# --------------------------------------------------------------------------- #
# 6b. ngrok tunnel (optional — needs NGROK_URL + NGROK_AUTHTOKEN in the env file)
# --------------------------------------------------------------------------- #
NGROK_ACTIVE=0
if [ "$NGROK_ENABLED" = "1" ]; then
    # Both values live in the SAME env file as everything else; flags override.
    [ -n "$NGROK_URL" ] || NGROK_URL="$(env_value NGROK_URL)"
    [ -n "$NGROK_AUTHTOKEN" ] || NGROK_AUTHTOKEN="$(env_value NGROK_AUTHTOKEN)"
    if [ -z "$NGROK_URL" ] && [ -z "$NGROK_AUTHTOKEN" ]; then
        ok "ngrok: not configured (set NGROK_URL + NGROK_AUTHTOKEN in the env file)"
    elif [ -z "$NGROK_URL" ] || [ -z "$NGROK_AUTHTOKEN" ]; then
        die "ngrok needs BOTH NGROK_URL and NGROK_AUTHTOKEN (found only one)."
    else
        case "$NGROK_URL" in
            https://*) ;;
            *) die "NGROK_URL must be a full https:// address, e.g. https://you.ngrok-free.app" ;;
        esac
        step "ngrok tunnel"

        # --- install the agent (Debian/Ubuntu apt repo) -------------------
        if command -v ngrok >/dev/null 2>&1; then
            ok "ngrok already installed ($(ngrok --version 2>/dev/null | head -1))"
        else
            curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
                | tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
            # Fixed suite (see NGROK_REPO_SUITE): ngrok publishes bookworm/bullseye
            # only, so a Debian 13 "trixie" line here just 404s on apt-get update.
            echo "deb https://ngrok-agent.s3.amazonaws.com ${NGROK_REPO_SUITE} main" \
                >/etc/apt/sources.list.d/ngrok.list
            if apt-get update -qq && apt-get install -y -qq ngrok >/dev/null; then
                ok "ngrok installed (apt suite: ${NGROK_REPO_SUITE})"
            else
                warn "apt could not install ngrok from the '${NGROK_REPO_SUITE}' suite —"
                warn "  falling back to the official static binary."
                install_ngrok_tarball || true
            fi
        fi

        if command -v ngrok >/dev/null 2>&1; then
            NGROK_BIN="$(command -v ngrok)"
            # --- authenticate as the APP USER: the token is stored in that user's
            #     ~/.config/ngrok/ngrok.yml, which is the account the unit runs as.
            if as_user "$NGROK_BIN" config add-authtoken "$NGROK_AUTHTOKEN" >/dev/null 2>&1; then
                ok "authtoken saved for $APP_USER (never echoed; not in the unit)"
            else
                warn "could not save the ngrok authtoken as $APP_USER"
            fi

            NGROK_UNIT="/etc/systemd/system/ngrok.service"
            cat >"$NGROK_UNIT" <<NGROKUNIT
[Unit]
Description=ngrok tunnel to nginx (public URL for Bandshell Calendar)
# After the app so the first request through the tunnel isn't a 502.
After=network-online.target bandshell.service
Wants=network-online.target bandshell.service

[Service]
Type=simple
User=${APP_USER}
# Tunnel to NGINX (auth + WebSocket upgrade live there) — never to uvicorn.
ExecStart=${NGROK_BIN} http ${NGINX_PORT} --url ${NGROK_URL} --log stdout
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
NGROKUNIT
            systemctl daemon-reload
            systemctl enable ngrok.service >/dev/null 2>&1 || true
            systemctl restart ngrok.service || true
            ok "ngrok.service → http://127.0.0.1:${NGINX_PORT} at ${NGROK_URL}"
            NGROK_ACTIVE=1
        fi
    fi
fi

# --------------------------------------------------------------------------- #
# 7. Smoke checks
# --------------------------------------------------------------------------- #
# Idle-shutdown status line for the summary (blank/0 == disabled).
_idle_min="$(awk -F= '/^[[:space:]]*AUTO_SHUTDOWN_MINUTES=/{v=$2; gsub(/[[:space:]]/,"",v)} END{print v}' "$ENV_FILE" 2>/dev/null || true)"
if [ "${NGROK_ACTIVE:-0}" = "1" ]; then
    FIREWALL_HINT="  6. Firewall: only SSH needs to be open now — ngrok reaches nginx from
     this machine's own loopback, so ${NGINX_PORT} can stay closed to the internet."
else
    FIREWALL_HINT="  6. Firewall: allow inbound ${NGINX_PORT} (and 443 if you add TLS), or keep it
     closed and point a tunnel at it. Do NOT expose ${PORT}."
fi
if [ -n "${_idle_min:-}" ] && [ "$(awk -v v="$_idle_min" 'BEGIN{print (v+0>0)?1:0}')" = "1" ]; then
    IDLE_LINE="on — ${_idle_min} min"
else
    IDLE_LINE="disabled — e.g. AUTO_SHUTDOWN_MINUTES=60"
fi

step "Smoke checks"
if as_user "$APP_DIR/.venv/bin/python" -c 'import main' 2>/tmp/bandshell-import.err; then
    ok "app imports (dependencies complete)"
else
    warn "app import failed:"; sed -n '1,15p' /tmp/bandshell-import.err >&2
    rm -f /tmp/bandshell-import.err
    die "the venv is not usable — fix the error above"
fi
rm -f /tmp/bandshell-import.err

if as_user "$APP_DIR/.venv/bin/python" - <<'PY' 2>/dev/null; then
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("<h1>ok</h1>")
        assert await page.inner_text("h1") == "ok"
        await browser.close()

asyncio.run(main())
PY
    ok "headless Chromium launches and renders"
else
    warn "headless Chromium failed to launch — install its system libraries with:"
    warn "  sudo $APP_DIR/.venv/bin/playwright install-deps chromium"
fi

if [ "$RUN_SANDBOX_TEST" = "1" ] && [ -f "$APP_DIR/tools/sandbox_smoke.py" ]; then
    # NOTE: must be captured inside if/else — a bare failing command would trip
    # `set -e` before $? could be read.
    if as_user "$APP_DIR/.venv/bin/python" "$APP_DIR/tools/sandbox_smoke.py" \
            >/tmp/bandshell-sbx.log 2>&1; then
        sbx_rc=0
    else
        sbx_rc=$?
    fi
    if [ "$sbx_rc" = "0" ]; then
        ok "bwrap script sandbox works (generated scripts run isolated)"
    elif [ "$sbx_rc" = "2" ]; then
        warn "sandbox unavailable (bwrap skipped); see /tmp/bandshell-sbx.log"
        warn "with SCRIPT_SANDBOX=auto scripts will run UNSANDBOXED in-process"
    else
        warn "sandbox smoke test FAILED — see /tmp/bandshell-sbx.log"
        warn "with SCRIPT_SANDBOX=1 scripted runs will refuse to run (fails loudly)"
    fi
fi

if [ "$INSTALL_SERVICE" = "1" ] && systemctl is-active --quiet bandshell; then
    healthy=0; code="000"
    for _ in $(seq 1 20); do
        # 2xx = up; 401 = up AND protected by nginx basic auth (expected).
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
                "http://127.0.0.1:${PORT}/api/settings" 2>/dev/null || echo 000)"
        case "$code" in
            2*|401) healthy=1; break ;;
        esac
        sleep 1
    done
    if [ "$healthy" = "1" ]; then
        ok "service is up and answering on 127.0.0.1:${PORT} (HTTP $code)"
        # Ask the process what it can actually see (booleans only — no secrets).
        hjson="$(curl -s --max-time 3 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
        if [ -n "$hjson" ]; then
            llm="$(printf '%s' "$hjson" | (jq -r '.llm_configured' 2>/dev/null || echo unknown))"
            stor="$(printf '%s' "$hjson" | (jq -r '.storage_backend' 2>/dev/null || echo unknown))"
            sbx="$(printf '%s' "$hjson" | (jq -r '.sandbox_mode' 2>/dev/null || echo unknown))"
            if [ "$llm" = "true" ]; then
                ok "the running service sees OPENROUTER_API_KEY (llm_configured=true)"
            elif [ "$llm" = "false" ]; then
                warn "the service does NOT see OPENROUTER_API_KEY — AI actions will fail."
                warn "  add it to $ENV_FILE, then: sudo systemctl restart bandshell"
            fi
            ok "reported: storage=${stor} sandbox=${sbx}"
        fi
    else
        warn "service is running but not answering yet (HTTP $code) — check: journalctl -u bandshell -n 60"
    fi
fi

# The tunnel is only useful if it fronts NGINX (auth intact). Probe the public URL.
if [ "$NGROK_ACTIVE" = "1" ]; then
    step "Public URL check"
    pub_code="000"; pub_err=""
    for _ in $(seq 1 15); do
        pub_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "${NGROK_URL}/" 2>/dev/null || echo 000)"
        [ "$pub_code" = "000" ] || break
        sleep 2
    done
    case "$pub_code" in
        401) ok "verified: ${NGROK_URL} reaches nginx and the login is enforced" ;;
        200) warn "${NGROK_URL} answered WITHOUT a password (HTTP 200) — is the tunnel"
             warn "  pointing at uvicorn (127.0.0.1:${PORT}) instead of nginx (:${NGINX_PORT})?"
             VERIFY_FAILED=1 ;;
        403) warn "${NGROK_URL} answered HTTP 403 — the tunnel is up but ngrok is"
             warn "  refusing the request (free-plan browser interstitial, or the domain"
             warn "  is claimed by another session: ERR_NGROK_334). Check: journalctl -u ngrok -n 40"
             warn "  In a browser you may need to click through ngrok's warning page first." ;;
        000) warn "${NGROK_URL} did not answer — DNS/domain not active yet, or the agent"
             warn "  failed to start. Check: journalctl -u ngrok -n 40" ;;
        *)   warn "${NGROK_URL} answered HTTP ${pub_code} (expected 401) — check:"
             warn "  journalctl -u ngrok -n 40 ; curl -sI http://127.0.0.1:${NGINX_PORT}/" ;;
    esac
fi

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
cat <<SUMMARY

$(printf '%sDone.%s' "$c_green" "$c_reset")

  project      $APP_DIR   (user: $APP_USER)
  venv         $APP_DIR/.venv  ($("$APP_DIR/.venv/bin/python" -V 2>&1))
  nginx        /etc/nginx/sites-available/${SITE_NAME}.conf
               listens on :${NGINX_PORT}  →  127.0.0.1:${PORT}
SUMMARY

if [ "$AUTH_ENABLED" = "1" ]; then
cat <<SUMMARY
  login        HTTP Basic auth — user '$AUTH_USER'  ($HTPASSWD_FILE)
SUMMARY
    if [ "${AUTH_GENERATED:-0}" = "1" ]; then
cat <<SUMMARY

$(printf '%sGENERATED PASSWORD:%s %s' "$c_yellow" "$c_reset" "$AUTH_PASS")
  Copy it now — it is not stored anywhere except as a hash in the htpasswd file.
  Change it later with: sudo htpasswd $HTPASSWD_FILE $AUTH_USER
SUMMARY
    fi
else
cat <<SUMMARY
  login        ${c_red}NONE${c_reset} — anyone who can reach this host has full access.
SUMMARY
fi

if [ "$INSTALL_SERVICE" = "1" ]; then
cat <<SUMMARY
  service      systemctl {status|restart|stop} bandshell
  logs         journalctl -u bandshell -f
SUMMARY
fi

if [ "$NGROK_ACTIVE" = "1" ]; then
cat <<SUMMARY
  public URL   ${NGROK_URL}   (ngrok → nginx :${NGINX_PORT}, login required)
  tunnel       systemctl {status|restart|stop} ngrok   ·  journalctl -u ngrok -f
               Change the address or token: edit NGROK_URL / NGROK_AUTHTOKEN in
               ${ENV_FILE} and re-run this script.
SUMMARY
elif [ "$NGROK_ENABLED" = "0" ]; then
cat <<SUMMARY
  public URL   not installed (--no-ngrok). Remove that flag, or expose ${NGINX_PORT} yourself.
SUMMARY
else
cat <<SUMMARY
  public URL   none configured — set NGROK_URL + NGROK_AUTHTOKEN in ${ENV_FILE}
               (then re-run this script) or expose ${NGINX_PORT} yourself.
SUMMARY
fi

cat <<SUMMARY

Next steps:
  1. Put secrets in ${ENV_FILE} (mode 600, KEY=VALUE lines — NOT a shell script,
     no 'export', no quoting). systemd does not inherit your shell environment, so a
     shell export or a .env file in the repo has no effect on the service.
     Then: sudo systemctl restart bandshell
     Check what the process sees: curl -s http://127.0.0.1:${PORT}/api/health
  2. Network access — nginx is the only public entry point; uvicorn listens on
     loopback only, so a tunnel must point at nginx (http://127.0.0.1:${NGINX_PORT}),
     NEVER at 127.0.0.1:${PORT}: that would skip nginx and the password.
  2b. Change the web password:  sudo bash deploy/install.sh --auth-pass 'new'
      (a re-run without --auth-pass KEEPS the current password; several
      --auth-user values can be added as extra logins: sudo htpasswd ${HTPASSWD_FILE} anotheruser)
  3. On a headless server keep HEADLESS=1 (already set in the unit).
  4. Script sandbox: install bubblewrap (done) and set SCRIPT_SANDBOX=1 so a broken
     sandbox fails loudly instead of silently running scripts unsandboxed.
  5. Idle cost control: set AUTO_SHUTDOWN_MINUTES in ${ENV_FILE} (currently ${IDLE_LINE})
     to power the host off after inactivity — the sudoers rule is already installed.
     Test it first with AUTO_SHUTDOWN_DRY_RUN=1, then check:
       curl -s http://127.0.0.1:${PORT}/api/activity
     A stopped instance must be started again (gcloud compute instances start …).
${FIREWALL_HINT}
SUMMARY

if [ "$VERIFY_FAILED" = "1" ]; then
cat <<SUMMARY

$(printf '%sWARNING:%s the login was NOT verified as enforced.' "$c_red" "$c_reset")
  Diagnose with:
    sudo nginx -T | grep -nE 'auth_basic|server_name|listen|proxy_pass'
    sudo grep -rn "proxy_pass.*:${PORT}" /etc/nginx/
    curl -sI http://127.0.0.1/api/settings        # expect: HTTP/1.1 401
SUMMARY
fi

if [ -n "$DOMAIN" ]; then
cat <<SUMMARY

  TLS: run once certificates are resolvable —
    sudo apt-get install -y certbot python3-certbot-nginx
    sudo certbot --nginx -d ${DOMAIN} --redirect -m ${EMAIL} --agree-tos -n
SUMMARY
fi

if [ -n "$DOMAIN" ] && [ "$NGINX_PORT" != "80" ]; then
    warn "certbot --nginx expects the site on :80 (HTTP-01 challenge + redirect)."
    warn "Re-run with --nginx-port 80, or issue the cert manually (DNS challenge)."
elif [ -n "$DOMAIN" ] && command -v certbot >/dev/null 2>&1; then
    step "Requesting TLS certificate"
    certbot --nginx -d "$DOMAIN" --redirect -m "$EMAIL" --agree-tos -n \
        && ok "https configured for $DOMAIN" \
        || warn "certbot failed; HTTP only for now (re-run manually)"
fi
