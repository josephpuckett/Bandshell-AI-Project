# Running Bandshell Calendar on a server

Everything an operator needs: install, update, settings, logs, cost control.

The short version: **Debian/Ubuntu VM → run `bootstrap.sh` → open the URL it prints.**
All detail below is for when that isn't enough.

```
tunnel / internet → nginx :8080 (basic auth, WebSocket upgrade, rate limit)
                        ↓
                    127.0.0.1:8000 (uvicorn, one worker) → Chromium
```

Two facts worth internalising before you touch anything:

- **nginx is the only public entry point.** uvicorn listens on loopback only. Point any
  tunnel at `http://127.0.0.1:8080`, never at `:8000` — the latter skips the password.
- **All state lives in one process** (`--workers 1`). Anything that restarts the app
  cancels in-flight agent work; see *Restarts* below.

---

## 1. Install

`bootstrap.sh` prepares the machine and hands over to `deploy/install.sh`:

```bash
sudo bash bootstrap.sh
```

It checks Debian/Ubuntu + root, runs `apt-get update` (+ upgrade), installs `git`, clones
or fast-forwards the checkout, `chown`s it to the app user, then runs `deploy/install.sh`.
The settings live at the top of the file (`REPO`, `DIR`, `APP_USER`, `USER_HOME`) and the
installer flags at the bottom — so set those once and the command stays the same forever.
`USER_HOME` is what makes `--env-file`/`--google-key` point at *your* downloads rather
than root's home — it's simply hardcoded to your home directory; see *Why `USER_HOME`*
below.

> **Keep `DIR` absolute.** A relative `DIR` clones into whatever directory you ran the
> script from, which is how people end up with two checkouts and a mysterious
> `.venv: Permission denied` later.
>
> **`~` in the installer flags means root's home** when the script runs under `sudo`.
> Use absolute paths (`/home/you/bandshell.env`), or run `deploy/install.sh` yourself with
> `sudo` — then `~` is expanded by *your* shell.

A minimal bootstrap that does the same thing by hand:

```bash
REPO=https://github.com/<you>/Bandshell-AI-Project.git
DIR=/opt/bandshell            # absolute: a relative DIR clones into $PWD
APP_USER=info
USER_HOME=/home/info          # where you downloaded bandshell.env + the Google key
apt-get update -qq && apt-get install -y -qq git
git -C "$DIR" rev-parse --show-toplevel >/dev/null 2>&1 || git clone -q --depth 1 "$REPO" "$DIR"
git -C "$DIR" fetch -q --depth 1 origin main && git -C "$DIR" reset -q --hard origin/main
chown -R "$APP_USER:$APP_USER" "$DIR"
exec bash "$DIR/deploy/install.sh" --user "$APP_USER" --auth-user bandshell --nginx-port 8080 \
  --env-file "$USER_HOME/bandshell.env" --google-key "$USER_HOME/google-key.json"
```

### Why `USER_HOME` and not `~` or `$HOME`

Inside a script run with `sudo`, both `~` and `$HOME` are **root's** home (`/root`), while
your `bandshell.env` and Google key are wherever *you* downloaded them. That's the
`cannot read --env-file /root/bandshell.env` error.

So the wrapper sets it explicitly, next to its other settings:

```bash
USER_HOME=/home/info                      # where the downloads live
echo "secrets: $USER_HOME/bandshell.env"   # makes a wrong path obvious immediately
```

and uses it for every path handed to the installer:

```bash
  --env-file "$USER_HOME/bandshell.env" \
  --google-key "$USER_HOME/google-key.json"
```

Worth knowing:

- **Hardcoding is fine for a single-admin VM** — that's the normal case, and a wrong value
  fails immediately and loudly (`cannot read --env-file /home/…`). The cost is that the
  wrapper is machine-specific: if a different account ever installs, edit that one line.
- Prefer it to follow whoever ran `sudo`? One line:

  ```bash
  USER_HOME="$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)"
  ```

  `$SUDO_USER` is the account that typed `sudo`; it's empty from a real root shell (console
  login, cloud run-command agent), so `:-$(id -un)` then resolves to root's home and the
  installer's error names `/root/…` — which is the diagnosis. `getent` rather than `~user`
  so LDAP/SSSD accounts work too.
- **Quote it.** `--env-file "$USER_HOME/bandshell.env"` — an unquoted path with a space
  silently splits the `exec` arguments.
- The variable is unnecessary when you run `deploy/install.sh` *yourself* with `sudo`:
  there `~` is expanded by your interactive shell before sudo sees it.

Gotchas that have actually bitten people:

| Symptom | Cause / fix |
|---|---|
| `cannot read --env-file /root/bandshell.env` | `~`/`$HOME` expanded by **root's** shell. Set `USER_HOME` in the wrapper (see *Why `USER_HOME`*), pass an absolute path, or run the installer from your own shell with `sudo` (then `~` is yours) |
| `failed to create directory …/.venv: Permission denied` | the checkout belongs to root but the app runs as another user. `sudo chown -R info:info /path/to/checkout` (and the same for `~info/.cache`, `~info/.local`) |
| `E: … 'trixie Release' does not have a Release file` | stale ngrok apt source; re-running the installer repairs it (`sudo sed -i 's/trixie/bookworm/' /etc/apt/sources.list.d/ngrok.list`) |
| clone lands somewhere unexpected | the bootstrap's `DIR` was relative; use an absolute path or you get a second checkout wherever you happened to be |
| `bootstrap.sh --auth-user …` is ignored | a minimal wrapper often hardcodes the installer flags instead of forwarding `"$@"`; edit the last line, or append `"$@"` to it |

### `deploy/install.sh` directly

Idempotent — re-run it after a `git pull` to refresh code, deps and config. It aborts at
the first genuinely broken step and otherwise reports warnings in a summary at the end.

| Flag | Meaning |
|---|---|
| `--app-dir PATH` | the checkout (default: this script's repo) |
| `--user NAME` | account that runs the app (default: owner of the checkout) |
| `--port N` / `--nginx-port N` | uvicorn port (8000, loopback) / nginx port (8080, public) |
| `--server-name NAME` | nginx `server_name` (default `_` = catch-all) |
| `--auth-user NAME` / `--auth-pass PASS` / `--no-auth` | the web login (basic auth is **on** by default) |
| `--env-file SRC` | install a downloaded env file (see below) |
| `--google-key SRC` | install a Google service-account JSON |
| `--auto-shutdown MINUTES` | power the host off after inactivity |
| `--ngrok-url URL` / `--ngrok-authtoken TOKEN` / `--no-ngrok` | public URL |
| `--no-service` / `--no-sandbox-test` | skip the systemd unit / the sandbox smoke test |

What it writes:

| Path | What |
|---|---|
| `/etc/nginx/sites-available/bandshell.conf` + `sites-enabled` symlink | the site (8080 → uvicorn) |
| `/etc/nginx/snippets/bandshell-proxy.conf` | shared proxy settings: headers, WebSocket upgrade, `proxy_buffering off`, 3600s timeouts, `auth_basic` |
| `/etc/nginx/bandshell.htpasswd` | credentials (`root:www-data`, 640) |
| `/etc/systemd/system/bandshell.service` | uvicorn, `--workers 1`, restarts always |
| `/etc/systemd/system/ngrok.service` | only if the tunnel is configured |
| `/etc/bandshell/bandshell.env` | secrets + settings (600, root) |
| `/etc/bandshell/google-service-account.json` | Sheets key (640, readable by the app user) |
| `/etc/sudoers.d/bandshell-shutdown` | lets the app power the host off (idle shutdown only) |

### Secrets

Both files are downloadable; hand the installer the path:

```bash
sudo bash deploy/install.sh --env-file ~/bandshell.env --google-key ~/sheets-key.json
```

- Format is literal `KEY=VALUE`, one per line. **Not** a shell script: no `export`, no
  quotes, and no inline comments (systemd keeps `# …` as part of the value).
- A UTF-8 BOM and CRLF line endings are normalised automatically; lines that aren't
  `KEY=VALUE` are reported and dropped rather than shipped to systemd.
- The previous env file is kept as `bandshell.env.bak`.
- The Google key is installed world-unreadable but group-readable by the app's group,
  because the app (not systemd) reads it at runtime.

---

## 2. Settings

Environment variables come from `/etc/bandshell/bandshell.env`, loaded by the systemd
unit. **systemd does not inherit your shell**, and a `.env` file in the repo is not read.
After editing: `sudo systemctl restart bandshell`.

`GET /api/health` reports booleans only — the quickest way to confirm the running process
picked values up without ever echoing a secret.

| Variable | Effect |
|---|---|
| `OPENROUTER_API_KEY` | **required** for any AI action (onboarding) |
| `OPENROUTER_FALLBACK_MODELS` | comma-separated fallback models when the primary is rate-limited |
| `EVENTBRITE_TOKEN` | enables Eventbrite websites (creates *drafts*; you publish them) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | path to the service-account JSON (or inline JSON — a path is less fragile) |
| `EVENTS_SPREADSHEET_ID` | the Sheets document for persistence; needs the key above too |
| `SCRIPT_SANDBOX` | `auto` (default — bwrap when installed), `1` (require it; fails loudly), `0` (in-process) |
| `BWRAP` / `SANDBOX_TIMEOUT` / `SANDBOX_CLIENT` | bwrap binary / per-run seconds (120) / MCP client (`auto`\|`fastmcp`\|`mcp`) |
| `SANDBOX_CPU_SECONDS` / `SANDBOX_MAX_MEMORY_MB` | per-run ceilings (30 s / 1024 MB) |
| `HEADLESS` | `1` = no visible browser window (set by the unit) |
| `BROWSER` | `chromium` (default, enables CDP screencast) or `firefox` |
| `BROWSER_FPS` / `BROWSER_JPEG_QUALITY` | live-view ceiling (4) / JPEG quality (80) |
| `AUTO_SHUTDOWN_MINUTES` | power off after N minutes of real inactivity (`0` = off) |
| `AUTO_SHUTDOWN_MAX_HOURS` / `AUTO_SHUTDOWN_DRY_RUN` / `AUTO_SHUTDOWN_CHECK_SECONDS` / `AUTO_SHUTDOWN_COMMAND` | force off after N h uptime / log-only mode / poll interval / command (default `sudo -n /usr/sbin/shutdown -h now`) |
| `NGROK_URL` + `NGROK_AUTHTOKEN` | read by the installer (not the app) to set up the tunnel |

### Persistence (Google Sheets)

Without it, **a restart loses events, websites and scripts**. With it, state round-trips
through worksheets named `Events`, `Websites`, `Settings` (created on first save), and the
**↻ Refresh** button re-hydrates in-memory state so sheet-side edits appear without a
restart. Refresh returns 409 while an AI task is running.

---

## 3. Operating it

```bash
sudo systemctl status  bandshell      # is it up?
sudo systemctl restart bandshell      # after editing the env file
sudo journalctl -u bandshell -f       # live logs (sandbox + browser breadcrumbs)
curl -s http://127.0.0.1:8000/api/health     # what it can see (booleans only)
curl -s http://127.0.0.1:8000/api/activity   # idle timer / busy state
```

**Update:** re-run the bootstrap (`sudo bash bootstrap.sh`) — it fast-forwards the
checkout and re-runs the installer. `install.sh` alone also works after a manual `git pull`.

**Change the web password:**

```bash
sudo bash deploy/install.sh --auth-pass 'a-new-one'      # keeps everything else
sudo htpasswd /etc/nginx/bandshell.htpasswd anotheruser  # extra logins; instant, no reload
```

A re-run *without* `--auth-pass` deliberately keeps the current password.

**Restarts** cancel in-flight agent work and close review pages. A website caught
mid-onboarding is reset to *Not set up* at startup rather than left stuck — you'll need to
run its one-time setup again.

**Disk:** `logs/` (LLM usage + full transcripts) and `generated/` (codegen dumps) grow
without limit. Prune occasionally:

```bash
find logs generated -type f -mtime +30 -delete
```

---

## 4. Public URL (ngrok)

Put both values in the env file:

```ini
NGROK_URL=https://your-name.ngrok-free.app
NGROK_AUTHTOKEN=2abc...
```

Re-run the installer. It adds ngrok's apt repo, saves the token **as the app user**
(that's where `ngrok config` writes), and installs `ngrok.service` running
`ngrok http 8080 --url <NGROK_URL>` **after** the app. It then probes the public URL and
expects **401** — proving the tunnel reaches nginx with the password intact.

- The apt suite is hardcoded to `bookworm` because that's what ngrok publishes
  (`dists/trixie/Release` is a 404, which breaks `apt-get update` on Debian 13). Override
  with `--ngrok-repo-suite`. If apt can't serve it, the installer falls back to ngrok's
  official static tarball in `/usr/local/bin`.
- `--url` needs a **reserved** domain; free plans include one static domain.
- `ERR_NGROK_334` in `journalctl -u ngrok` = that domain is claimed by another agent.
- With the tunnel up, only **SSH** needs to be open in the cloud firewall.

---

## 5. Cost control (idle shutdown)

An idle VM bills by the hour. Set:

```ini
AUTO_SHUTDOWN_MINUTES=60
```

and re-run the installer (it writes the matching sudoers rule). What counts as activity:
loading the app, any create/edit/approve, and interaction with the live view. **Not** the
1-second polls or `/api/health` — a forgotten browser tab can't keep the box awake.

It refuses to power off while work is in flight (an agent or script running, a site
awaiting approval, an unsubmitted review page). `AUTO_SHUTDOWN_MAX_HOURS` forces the issue
after N hours regardless.

Test it in log-only mode first:

```bash
# AUTO_SHUTDOWN_DRY_RUN=1   then restart the service
journalctl -u bandshell | grep 'idle shutdown'
```

A stopped instance must be started again from your cloud console. With Sheets persistence
nothing is lost; without it, in-memory state goes with the power-off.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/api/health` says `llm_configured: false` | key missing from `/etc/bandshell/bandshell.env` (a shell export does nothing); add it and restart |
| every page returns **401** | correct — basic auth is on; the browser prompts once and caches it |
| a page is served **without** a prompt | another nginx site is answering: `sudo nginx -T \| grep -nE 'listen\|server_name\|proxy_pass'` |
| live view blank, WS closes 1006/404 | missing `Upgrade`/`Connection` headers, or the tunnel points at uvicorn |
| `journalctl` shows `[browser-ws] no messages…` | stream reconnected by the client watchdog; a persistent loop means the tunnel is dropping |
| scripted runs die with `exit 137` | sandbox limits: try `SANDBOX_CLIENT=mcp`, raise `SANDBOX_MAX_MEMORY_MB`, read `[sandbox]` log lines |
| the VM powered off by itself | that's `AUTO_SHUTDOWN_MINUTES`: `journalctl -u bandshell \| grep 'idle shutdown'` |
| idle shutdown never fires | something is in flight (`busy: true` in `/api/activity`), or the sudoers rule is wrong — check the journal |
| website badge says **Stale** | global settings changed (venue/cost/categories); re-run that website's setup |
