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
#   7. runs smoke checks (app imports, headless Chromium launches, bwrap sandbox)
#
# It does NOT touch your environment/secrets. Provide them in:
#     /etc/bandshell/bandshell.env      (KEY=VALUE lines, chmod 600)
# which the systemd unit loads (OPENROUTER_API_KEY, EVENTBRITE_TOKEN,
# GOOGLE_SERVICE_ACCOUNT_JSON, EVENTS_SPREADSHEET_ID, ...).
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
PORT="${PORT:-8080}"
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
LIMIT_ZONE="${SITE_NAME//-/_}_cost"
PROXY_SNIPPET="/etc/nginx/snippets/${SITE_NAME}-proxy.conf"

as_user() { sudo -u "$APP_USER" -H -- "$@"; }

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

    if [ ! -f /etc/bandshell/bandshell.env ]; then
        warn "no /etc/bandshell/bandshell.env yet — create it (chmod 600) before starting:"
        warn "    OPENROUTER_API_KEY=sk-or-..."
        warn "    # optional: EVENTBRITE_TOKEN=..., GOOGLE_SERVICE_ACCOUNT_JSON=...,"
        warn "    #           EVENTS_SPREADSHEET_ID=..., SCRIPT_SANDBOX=1"
    fi
    systemctl restart bandshell.service || true
fi

# --------------------------------------------------------------------------- #
# 7. Smoke checks
# --------------------------------------------------------------------------- #
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
    else
        warn "service is running but not answering yet (HTTP $code) — check: journalctl -u bandshell -n 60"
    fi
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

cat <<SUMMARY

Next steps:
  1. Create /etc/bandshell/bandshell.env (chmod 600) with at least
     OPENROUTER_API_KEY=... . Then: sudo systemctl restart bandshell
  2. Open port ${NGINX_PORT} (and 443 if you use TLS) in the cloud firewall — nginx is the
     only public entry point; uvicorn itself listens on loopback only.
     Point any tunnel (ngrok/cloudflared) at nginx (http://127.0.0.1:${NGINX_PORT}), NEVER
     at 127.0.0.1:${PORT}: a tunnel straight to uvicorn skips nginx and the password.
  2b. Rotate the password any time: sudo htpasswd ${HTPASSWD_FILE} ${AUTH_USER}
  3. On a headless server keep HEADLESS=1 (already set in the unit).
  4. Script sandbox: install bubblewrap (done) and set SCRIPT_SANDBOX=1 so a broken
     sandbox fails loudly instead of silently running scripts unsandboxed.
  5. Idle cost control: implement the inactivity watchdog (main.py) + a sudoers line
     for \`shutdown\`, or wire a Cloud Scheduler job to stop the instance.
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
