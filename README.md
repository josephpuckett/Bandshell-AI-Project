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

Requires Python 3.12 with the deps in `pyproject.toml` (existing venv: `.venv`), plus:

- `OPENROUTER_API_KEY` — LLM calls go to OpenRouter (model configured in `state.py`)
- Firefox (runs `headless=False`, so a visible window appears on the server)

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000`, create an Event, add a Website, and run through the
guided setup.

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

### Serving externally

The embedded live browser view uses a WebSocket (`/api/browser/{id}/ws`), so a reverse
proxy must pass WebSocket upgrade headers. The frontend derives the WS URL from the page
host, so any tunnel (ngrok, Cloudflare, Tailscale) works without code changes.

Recommended setup: `nginx` on the server reverse-proxying to uvicorn, then an HTTPS
tunnel (ngrok / Cloudflare tunnel) in front:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Upgrade $http_upgrade;          # WebSocket pass-through
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

Note: ngrok tunnels WebSockets transparently. On ngrok's free tier the URL changes
per restart — consider a static domain for anything persistent.

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

See `AGENTS.md` for the in-depth agent-oriented guide to the codebase.