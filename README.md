# Bandshell Calendar

A FastAPI web app that partially automates publishing nonprofit events to other
organizations' online calendars. An AI agent acts as a "browser operator" through a
one-time guided setup, then a reusable script fills the same form for every future event.

**Status: demo / prototype.** All state is in-memory (a restart loses events, websites,
scripts, and settings). Optional Google Sheets persistence and an Eventbrite API path
exist but are lightly tested.

## How it works

1. **An AI agent** (LLM) uses Playwright to manually navigate to the calendar website and
   fill in the event form — you can watch it live in the embedded browser view.
2. **A human approves** the manual attempt in the UI (and can fix fields, solve captchas,
   and submit from the embedded view).
3. The agent writes a **reusable Python script** from the steps it took, parameterized by
   the `Event` class — no LLM needed for subsequent runs.
4. The script runs against the browser; you **review and submit manually**.
5. Future events for that site just run the saved script.

The workflow is *scripted, not automated*: nothing is ever published without a human
looking at it.

## Quick start

Requirements: Python 3.12 with the deps in `pyproject.toml`, plus:

- `OPENROUTER_API_KEY` — LLM calls go to OpenRouter (model configured in `state.py`)
- Chromium — Playwright's browser (`BROWSER=firefox` switches engines; Firefox loses the
  CDP screencast, so the live view falls back to slower per-frame screenshots)

```bash
apt-get install bubblewrap          # optional: script sandbox (recommended)
uv sync                             # creates .venv from uv.lock (CPython 3.12)
.venv/bin/playwright install --with-deps chromium
HEADLESS=1 uvicorn main:app --host 127.0.0.1 --port 8000
```

A visible browser window opens on the machine running the app unless `HEADLESS=1`. Then
open `http://127.0.0.1:8000`, create an Event, add a Website, and run through the guided
setup.

For an actual server, skip the steps above and use `deploy/install.sh` (next section) —
it does all of this, plus nginx, auth, and systemd.

### Optional configuration (env vars)

| Variable | Effect |
|----------|--------|
| `OPENROUTER_FALLBACK_MODELS` | Comma-separated fallback models used when the primary is rate-limited |
| `BROWSER` | Playwright engine: `chromium` (default) or `firefox`. Chromium enables the live view's CDP screencast streaming (see below); Firefox falls back to per-frame screenshots |
| `HEADLESS` | `1` runs the Playwright browser without a visible window (needed on headless servers) |
| `BROWSER_FPS` | Live-view target frame rate (default `4`, max `60`). A ceiling — the actual rate is bounded by the CDP/screenshot pipeline and the VM's CPU; unchanged frames are deduped anyway |
| `BROWSER_JPEG_QUALITY` | Live-view JPEG quality (default `80`, range 30–95). Lower = faster encode + smaller frames (the encode is the CPU floor on small VMs); a small visual cost in the watching view |
| `EVENTBRITE_TOKEN` | Enables Eventbrite API websites (draft events, no publishing) |
| `SCRIPT_SANDBOX` | Script isolation: `auto` (default — bubblewrap when installed, else in-process), `1`/`require` (require bwrap), `0` (always in-process). Install with `apt-get install bubblewrap` |
| `BWRAP` / `SANDBOX_TIMEOUT` | bwrap binary path (default `bwrap`) / per-run wall-clock limit in seconds (default 120) |
| `SANDBOX_CLIENT` | MCP client used *inside* the sandbox: `auto` (default — fastmcp if it imports, else the `mcp` SDK), `fastmcp`, or `mcp` (lightest; use if sandboxed runs die with `exit 137`) |
| `SANDBOX_CPU_SECONDS` / `SANDBOX_MAX_MEMORY_MB` | Per-run ceilings applied inside the sandbox (default 30 s / 1024 MB) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` + `EVENTS_SPREADSHEET_ID` | Google Sheets persistence (Events / Websites / Settings worksheets) |

(The browser engine is set via the `BROWSER` env var, default `chromium`.)

### Deploying on a server (Debian/Ubuntu)

`deploy/install.sh` provisions a host end to end: system packages, `uv`, the
Python deps (from `uv.lock`), Playwright's Chromium, an nginx site with
**HTTP Basic auth on by default**, and a systemd unit running uvicorn on
loopback. Put secrets in `/etc/bandshell/bandshell.env`.

```bash
sudo bash deploy/install.sh --auth-user you --nginx-port 8080
```

**Environment variables go in `/etc/bandshell/bandshell.env`** (created by the
installer, mode 600). It is read by the systemd unit — systemd does *not* inherit
your shell, and a `.env` file in the repo is not read, because the app uses
`os.getenv()` at import time. Format is literal `KEY=VALUE` lines:

```ini
OPENROUTER_API_KEY=sk-or-v1-...
HEADLESS=1
SCRIPT_SANDBOX=1
```

Then `sudo systemctl restart bandshell`. To check what the running process
actually sees:

```bash
curl -s http://127.0.0.1:8000/api/health
# {"ok":true,"llm_configured":true,"storage_backend":"MemoryStorage",...}
```

`/api/health` reports booleans only (never values), so it is safe to expose.

The installer is idempotent — re-run it after a `git pull` to refresh deps and configs.
It refuses to leave a competing nginx site enabled: if another config listens on the same
port or proxies to the same uvicorn port, it names the file to remove before reloading
(a failed reload would leave the old, *unauthenticated* site serving).

#### What it sets up

| Piece | Result |
|---|---|
| nginx | listens on `--nginx-port` (default **8080**), proxies to `127.0.0.1:8000`, WebSocket upgrade + 3600s timeouts for the live view |
| auth | HTTP Basic on **every** route (the app has no login of its own); password in `/etc/nginx/bandshell.htpasswd` |
| rate limit | `6r/m` + burst 3 on the two endpoints that cost money (`…/onboard`, `…/run`) |
| systemd | `bandshell.service`, `--workers 1`, restarts always, reads `/etc/bandshell/bandshell.env` |
| sandbox | installs `bubblewrap`; set `SCRIPT_SANDBOX=1` in the env file so a broken sandbox fails loudly instead of silently running scripts unsandboxed |

### Serving externally

The embedded live browser view uses a WebSocket (`/api/browser/{id}/ws`). The frontend
derives the WS URL from the page host, so any tunnel (ngrok, Cloudflare, Tailscale) works
without code changes — **but it must point at nginx, not at uvicorn**:

```bash
tunnel → http://127.0.0.1:8080   # nginx: auth + WebSocket upgrade
             ↓
        127.0.0.1:8000         # uvicorn: loopback only, no auth
```

A tunnel aimed straight at `127.0.0.1:8000` bypasses the password entirely. Everything the
proxy needs is in `/etc/nginx/snippets/bandshell-proxy.conf` (shared by every location, so
a `certbot --nginx` rewrite can't drop it); don't hand-edit the site file — re-run the
installer instead.

On ngrok's free tier the URL changes per restart — use a static domain for anything
persistent, and note the free tier's connection cap: a busy session (long-lived WS + 1s
polls) can exceed it, which shows up as the live view freezing while pages still load.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/api/health` says `llm_configured: false` | the key isn't in `/etc/bandshell/bandshell.env` (a shell `export` or a repo `.env` has no effect on a systemd service). Add `OPENROUTER_API_KEY=…`, then `sudo systemctl restart bandshell` |
| every page returns **401** | basic auth is on — that's the intended state; the browser prompts once and caches it |
| a page is served but **200 without a prompt** | another nginx site is answering: `sudo nginx -T \| grep -nE 'listen\|server_name\|proxy_pass'` |
| live view is blank, WS closes with 1006/404 | missing `Upgrade`/`Connection` headers (see snippet above), or the tunnel points at uvicorn |
| scripted runs die with `exit 137` | sandbox `/proc` or memory limits; try `SANDBOX_CLIENT=mcp`, raise `SANDBOX_MAX_MEMORY_MB`, and check `[sandbox]` lines in `journalctl -u bandshell` |
## Repository layout

| Path | Purpose |
|------|---------|
| `main.py` | FastAPI app, all `/api/*` routes, WebSocket live view |
| `agents.py` | Agent orchestration state machines (`run_onboarding`, `run_task`) |
| `bot.py` | LLM client (OpenRouter/OpenAI-compatible) with tool-call loop + retry/fallback handling |
| `browser.py` | Playwright manager + the browser tools the agent calls |
| `scripting.py` | Script validation (AST allowlist) and execution (`Driver`; runs in a bubblewrap sandbox when available) |
| `sandbox_framing.py` `sandbox_host.py` `sandbox_runner.py` | Script sandbox: MCP-stdio framing, host-side driver server, sandbox-side runner |
| `scripter.py` | Prompt builders + code-extraction helpers |
| `event.py` `settings.py` `website.py` `task.py` | Dataclasses |
| `eventbrite_api.py` | Eventbrite API client (drafts, image upload) |
| `storage.py` `drive.py` `memory_storage.py` | Persistence facade (Google Sheets optional) |
| `serialization.py` | Dataclass ↔ API-dict conversion, settings-version hashing |
| `state.py` | Shared globals: LLM config, in-memory registries, sandbox/deploy settings |
| `deploy/install.sh` | Idempotent Debian/Ubuntu provisioner: apt deps, `uv`, venv, Chromium, nginx site + auth + rate limit, systemd unit, env file, smoke checks |
| `docs/sandbox-plan.md` | Sandbox design + threat model + what was verified |
| `frontend/` | Static UI (`index.html`, `app.js`, `style.css`) |
| `logs/` | Runtime LLM usage + per-run transcripts (JSONL, gitignored) |
| `tools/sandbox_smoke.py` | Smoke test for the bwrap + MCP-stdio sandbox bridge (no browser needed) |

## Key behavior notes

- **Scripts are gated by `validate_script()`** — an AST walk that rejects imports,
  disallowed calls, and dangerous patterns before any script executes. This is the defense
  against prompt/code injection (a malicious calendar site steering the agent toward
  malicious code).
- **Scripts then run inside a bubblewrap sandbox** (when `bwrap` is installed): no network,
  no host filesystem (an empty tmpfs root plus a read-only whitelist of `/usr`, the venv and
  three project files), no API keys in the environment, and CPU/memory/fd ceilings applied by
  the runner itself (`resource.setrlimit`, soft == hard). The only thing the script can touch
  is the 11 allowlisted `api.*` operations, which are served by the host over MCP-stdio and
  drive the same review page you are watching. `SCRIPT_SANDBOX` controls it (`auto` = use
  bwrap if it passes a one-time preflight, `0` = in-process fallback, `1` = require it).
  See `docs/sandbox-plan.md`.
- **Settings versioning:** generated scripts record a hash of the *global* settings they
  were built against (venue, location, cost, categories, timezone, Eventbrite IDs). Changing
  those marks a scripted website **stale** and it must be re-onboarded — scripts take those
  defaults for granted. Per-event fields (website_url, organizer, contact info) don't
  invalidate scripts; each event carries its own copy.
- **Image URLs:** Google Drive share links are rewritten to direct-download URLs before the
  agent or script uses them. A link that isn't shared ("Anyone with the link") is caught by a
  preflight check and logs a clear warning instead of failing opaquely.
- **The embedded view is a stream of JPEG frames** (rate = `BROWSER_FPS`, default 4fps
  over a WebSocket), not a real iframe. With the default **chromium** engine it uses CDP
  `Page.screencast` (a continuous optimized pipeline — the browser streams frames it's
  already compositing; no per-frame re-capture), falling back to `page.screenshot()`
  per-frame on Firefox. It's view-only while the AI works, interactive during
  approval/review phases. Note: native browser popups (date pickers, select dropdowns)
  are browser-chrome and don't appear in the captured stream — the UI hints you to type
  or use the keyboard instead.
  Because it's imagery, text in the page can't be copied from it — but you can paste *into*
  it with Ctrl/Cmd+V (reads your OS clipboard, inserts into the focused field).
- **The agent's browser window closes** when its interaction finishes; a website left
  mid-onboarding by a crash/restart is reset to *Not set up* at startup rather than stuck
  waiting to approve.

## Monitoring

- `logs/llm_usage.jsonl` — one line per LLM call: model, finish reason, token counts,
  elapsed, failed attempts. Watch for `"finish": "length"` (the 8192-token runaway guard
  cut a response short).
- `logs/onboarding_*.jsonl` / `logs/task_*.jsonl` — full per-run transcripts (tool calls +
  results included).

Both files append forever and `generated/` grows by two dumps per codegen attempt — on a
long-lived host, prune them (`find logs generated -type f -mtime +30 -delete`).

For a deployed instance, `journalctl -u bandshell -f` shows the app's stdout/stderr,
including the `[sandbox-runner]` / `[sandbox]` breadcrumbs and the `[browser-ws] ~X.Xfps`
line that tells you whether the live view is actually producing frames.

See `AGENTS.md` for the in-depth agent-oriented guide to the codebase.