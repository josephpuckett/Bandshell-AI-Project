# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project Overview

**Bandshell Calendar** is a FastAPI web app that partially automates publishing events
to other organizations' online calendars. The core workflow:

1. An **AI agent** (LLM) uses Playwright browser tools to manually navigate to a website and
   fill in an event form.
2. A **human approves** the manual attempt in the UI.
3. The agent generates a **reusable Python script** parameterized by the `Event` class that
   populates the same fields directly (no LLM needed on subsequent runs).
4. The script is run against the browser for the human to **review and submit manually**.
5. Future events for that same website just run the saved script; the agent is not involved.

This is a **demo/prototype** — all state is in-memory. No database. No persistence beyond
process memory (restart loses events, websites, scripts, settings).

## Layout

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app + route handlers (app wiring, lifespan, all `/api/*` endpoints incl. `/api/health` and the browser WebSocket). Entry point. |
| `state.py` | Shared global state/config: LLM provider, mounted browser MCP (`parent`), frontend path, and all in-memory registries + settings. |
| `agents.py` | Long-running agent orchestration: `run_onboarding` and `run_task` state machines. |
| `scripting.py` | Safe script execution: `Driver` (the `api` surface), `validate_script`, `execute_script_in_process`, `run_script_against_event`. Pages opened for scripted runs get a pinned `VIEWPORT=(1280, 850)` (desktop size for the embedded view). `run_script_against_event` executes the script inside the bwrap sandbox when available, else in-process. |
| `sandbox_framing.py` | MCP stdio framing bound to arbitrary pipes (`framed_streams` for the host's asyncio pipe pair, `framed_stdio` for the child's own stdin/stdout). Shared by both sandbox halves; ~50 lines on top of the SDK's `JSONRPCMessage`/`SessionMessage`. |
| `sandbox_host.py` | Host side of the sandbox: a low-level `mcp` `Server` bound to the bwrap child's pipes, exposing exactly the 11 `ALLOWED_API` ops plus the `get_job`/`report` bootstrap tools, all dispatching into the real `Driver` against the review page. Per-run instance; caps calls and argument sizes. |
| `sandbox_runner.py` | Sandbox side: the only code executed inside bwrap. A `fastmcp.Client` over its **own** stdin/stdout (inverted stdio — it cannot spawn), fetches `get_job`, `exec`s the script in a scrubbed namespace, forwards each `api.*` as an MCP `tools/call`, reports the outcome, exits. **stdout is the wire** — script output is redirected to stderr. |
| `tools/sandbox_smoke.py` | VM smoke test for the sandbox bridge (bwrap flags, fd/pipe inheritance, framing both ways, event delivery, stdout hygiene, error propagation, runaway timeout). Uses a stub dispatcher, so it needs no browser. |
| `deploy/install.sh` | Idempotent Debian/Ubuntu provisioner for a real host: apt deps (incl. Chromium libs + bubblewrap), `uv` **as the app user**, `uv sync --frozen`, `playwright install chromium`, nginx site + snippet + basic auth + rate limit, systemd unit, `/etc/bandshell/bandshell.env`, then smoke checks (app imports, Chromium renders, sandbox smoke test, `/api/health`, auth probe must return 401). Re-runnable; aborts at the first failing step. |
| `docs/sandbox-plan.md` | Sandbox design + threat model + the bugs found during E2E verification. |
| `scripter.py` | Prompt builders (`codegen_prompt`, `initial_*_message`) + code extraction helpers. |
| `serialization.py` | Dataclass→API-dict serialization (`event_out`, `website_out`, ...) + `asset_url`, `current_settings_version`, `_parse_event_id`. |
| `bot.py` | `Bot` class: HTTP client for the LLM (httpx), manages the tool-call loop. Every completion sends `max_tokens=8192` (runaway-generation guard). Timeout is `httpx.Timeout(connect=10, read=600, write=30, pool=10)` — the read budget is generous because a max-size generation at a slow provider can take minutes; a too-short read was silently dropping large-but-valid responses. 4-attempt retry: `_TransientError` (429 rate limit / 5xx, also HTTP-200-with-error bodies) backs off honoring `Retry-After` (capped at 30s); other failures retry with backoff; every attempt logs (elapsed + error) to `logs/llm_usage.jsonl` and success logs `finish=... in=... out=... elapsed=...`; total failure raises a descriptive `RuntimeError` (URL/status/body). Optional OpenRouter fallback models via `state.FALLBACK_MODELS` (env `OPENROUTER_FALLBACK_MODELS`, sent as the `models` array). Cancellation (`asyncio.CancelledError`) propagates. |
| `browser.py` | Playwright browser manager + the MCP tools the agent calls. Tools: `navigate_to`, `fill_nth_of_role`/`click_nth_of_role` (1-based index, fallback), `fill_by_role`/`click_by_role`/`fill_by_label`/`fill_by_placeholder`/`click_by_text` (accessible-name/label/text targeting — preferred), `press_key`, `wait`, `take_screenshot` (viewport JPEG by default, `full=True` for whole page — the manual agent's eyes when aria is ambiguous), `aria_snapshot` (full or diff). Plus `LIVE_PAGES` registry + `Manager`/`Session`. |
| `event.py` | `Event` dataclass — the generic input every script consumes. |
| `settings.py` | `Settings` dataclass — global defaults: venue/location/cost/publicity/categories stay baked into every event; website_url + organizer/names/phone/email are *per-event prefills* (overridable on each event, see events/edit below). |
| `website.py` | `Website` and `Script` dataclasses — a website + its generated reusable script. |
| `task.py` | `Task` dataclass — a one-off manual agent fill tied to an event (legacy/less-used path). |
| `eventbrite_api.py` | Eventbrite API client (no LLM/browser): draft event creation, image upload, free ticket class. |
| `storage.py` | Storage facade: `mark_dirty()`/`flush()` + debounced background flusher; `make_storage()` picks backend by env. |
| `drive.py` | Google Sheets persistence (gspread) — Events/Websites/Settings worksheets; enabled by env vars. |
| `memory_storage.py` | Default no-op in-memory storage backend. |
| `frontend/` | Static UI served by FastAPI (`index.html`, `app.js`, `style.css`). |
| `generated/` | Debug artifacts from script-codegen validation (raw/extracted attempts). Scratch — ignore. |
| `logs/` | Runtime agent logs (auto-created, gitignored): `llm_usage.jsonl` (one JSON line per LLM call incl. failed attempts) + one JSONL transcript per onboarding/task run. |
| `misc/` | Unused / scratch files (eventbrite API code, field notes, test JSON). Ignore. |

## How to Run

- Requires a Python 3.12 venv with the deps in `pyproject.toml`. Existing interpreter lives
  in `.venv` (symlinked to a system Python on the user's machine).
- The browser uses **Chromium** by default (`BROWSER=firefox` to switch back) — Chromium
  is required for the live view's CDP screencast streaming. Visible window by default so
  the human can watch/approve; set `HEADLESS=1` for headless servers.
- LLM calls go to **OpenRouter** (`https://openrouter.ai/api`) with the model set in
  `state.py` (`MODEL`; currently `deepseek/deepseek-v4.1-flash` — swap to a vision-capable
  model e.g. gpt-4o/claude to make `take_screenshot()` useful). An `OPENROUTER_API_KEY`
  env var is required. To point at a local OpenAI-compatible server instead, change
  `MODEL`/`URL`/`TOKEN` in `state.py` — nothing else needs to move. `OPENROUTER_FALLBACK_MODELS`
  (comma-separated) enables OpenRouter's `models`-array failover when the primary is
  rate-limited (429s in-band or as HTTP status are retried with `Retry-After` backoff).
- Optional features (off by default, enabled by env vars):
  - **Eventbrite** websites: `EVENTBRITE_TOKEN`
  - **Google Sheets persistence**: `GOOGLE_SERVICE_ACCOUNT_JSON` (JSON content or path to the JSON file) + `EVENTS_SPREADSHEET_ID`
  - **Script sandbox**: `SCRIPT_SANDBOX=auto` (default — bwrap when installed, else in-process),
    `1`/`require` (fail the run if bwrap is missing), `0` (always in-process). Also
    `BWRAP` (binary path), `SANDBOX_TIMEOUT` (wall-clock seconds per run, default 120),
    `SANDBOX_CLIENT` (`auto`|`fastmcp`|`mcp` — the MCP client used inside the sandbox),
    `SANDBOX_CPU_SECONDS` (30) and `SANDBOX_MAX_MEMORY_MB` (1024).
    Install with `apt-get install bubblewrap`; verify with `python3 tools/sandbox_smoke.py`.
    The runner prints `[sandbox-runner] start ...` / `client=... ready` breadcrumbs to
    stderr (mirrored by the host as `[sandbox] ...`) so a startup failure is pinpointed.
    Verified end-to-end against bwrap 0.13.1 + CPython 3.12.15 + fastmcp 3.4.4 + mcp
    1.28.1: the smoke test passes on both `SANDBOX_CLIENT` paths, and a generated script
    drives a real Chromium page through the bridge. Measured cost: the sandboxed client
    peaks at ~54 MB RSS (~76 MB VA), so it is NOT a memory hog — a 137/SIGKILL is not
    explained by the MCP client's imports.
    `SANDBOX_ERROR_PREFIX` (`sandbox-unavailable:`) marks failures where the script never
    ran; `agents.py` aborts on that prefix instead of asking the LLM to rewrite.

```
uvicorn main:app --host 127.0.0.1 --port 8000
```

The app binds itself and mounts `/static`.

**Deployment (`deploy/install.sh`).** The supported server layout is nginx → uvicorn on
loopback, provisioned by the script (idempotent — re-run after a `git pull`):

```
tunnel/Internet → nginx :8080 (basic auth, WS upgrade, rate limit) → 127.0.0.1:8000 (uvicorn, 1 worker)
```

Facts that matter when touching either side:

- **Auth lives in nginx, not the app** (the app has no login). It's HTTP Basic on every
  route via a shared snippet `/etc/nginx/snippets/<site>-proxy.conf`; browsers replay the
  cached credentials on the WebSocket handshake, so the live view works unchanged.
- **A tunnel must point at nginx**, never at `127.0.0.1:8000` — the latter skips the password.
- **The proxy config lives in the snippet**, not inline in the site file, so both locations
  (and any `certbot --nginx` rewrite) carry the same auth/WS/timeouts.
- **`PORT` (8000) and `NGINX_PORT` (8080) are separate** on purpose; the installer detects a
  competing site (same listen port, or a second `proxy_pass` to the app port) *before*
  reloading nginx, because a failed reload leaves the old, unauthenticated site serving.
- **The systemd unit must not set `NoNewPrivileges`/`ProtectSystem`** — bwrap needs user
  namespaces. Uvicorn runs with `--workers 1` (all state is process-local) and
  `--timeout-graceful-shutdown 10` (so SIGTERM still flushes storage to Sheets).
- **Env comes from `/etc/bandshell/bandshell.env`** — see Config & secrets.

The manual equivalent of what the installer generates (abbreviated — the real file also
has auth, the `map` for `Connection`, `proxy_buffering off`, and the rate limit):

**nginx + tunnel + WebSockets (important):** the embedded browser view
(`/api/browser/{id}/ws`) needs WebSocket upgrade headers in the reverse proxy —
a common config that serves the normal pages fine but silently drops the live
view with `404` / WS close `1006`:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    # WebSocket pass-through (the embedded browser view).
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

(Also note: if nginx rate-limits per IP, remember the live view's frames travel
inside one WebSocket — they are not separate requests — so `limit_req` only sees
 the handshake + the 1s state polls.)

**Tunneling (ngrok / Cloudflare / anywhere):** the frontend builds the WS URL from
`location.host` (`app.js`) with `ws://` or `wss://` chosen from the page protocol, so
page WSS pass-through is enough — no tunnel-specific WS config is needed beyond the
nginx upgrade headers above. ngrok free-tier notes: the public URL changes on every
restart (bookmarks/tunnels break; static domains need paid/legacy free), and the
connection/concurrency caps are low, so a busy run (long-lived WS + 1s polls) may hit
ngrok's free limits — if the embedded view silently freezes after some time, that's
the likely cause (pages load, WS closes).

## Module dependency map (no circular imports)

```
state.py      <- shared config + registries (leaf, imports nothing project-specific)
bot.py        <- LLM client (leaf)
browser.py    <- Playwright MCP tools (leaf)
event/settings/task/website <- dataclasses (leaves)
scripting.py  <- depends on browser.manager + dataclasses
scripter.py   <- depends on dataclasses
serialization.py <- depends on state
agents.py     <- depends on state, bot, scripting, scripter, serialization
main.py       <- depends on everything; imports agents/serialization/state
```

## Two Agent Paths (important)

1. **Website onboarding** (`run_onboarding` in `agents.py`) — the primary path. A website goes
   through a state machine:
   ```
   manual_fill → manual_approval → writing → running (script exec) → script_approval → done
   ```
   Each step loops back on failure/rejection. Ends with `Website.status = "scripted"` and an
   approved `Script`. Onboarding is keyed to a `website_id` + `event_id`.

   - Agent fills the form manually (works in the browser).
   - Human clicks approve/reject in the Website UI.
   - Agent writes a script from its tool calls; script is AST-validated; human approves the
     scripted run.

2. **One-off task** (`run_task`) — a legacy/less-used path where the agent fills a calendar
   form for a single event with no script. Kept around but not the focus.

3. **Eventbrite website** (new, no agent) — `Website.kind = "eventbrite"` (only one allowed).
   Uses the Eventbrite API directly with `EVENTBRITE_TOKEN`: `POST /api/websites/{id}/add-event`
   creates a **draft** (never auto-publishes; the human publishes in Eventbrite). Venue/organizer
   ids are configured once per eventbrite site via `POST /api/websites/{id}/configure-eventbrite`,
   called from the frontend popup dialog; timezone + category-id mapping come from Settings. Image
   upload is best-effort (Drive links converted to direct download URLs and downloaded server-side;
   the agentic path only rewrites the URL — see Script generation & execution).

## Script generation & execution (the core feature)

- The agent's successful manual sequence is captured from its tool-call transcript and fed to
  a codegen prompt (`codegen_prompt`), asking it to emit an `async def run(event, api)`.
- **Interaction model:** the agent prefers accessible-name/label targeting (`fill_by_role`,
  `click_by_role`, `fill_by_label`, `fill_by_placeholder`, `click_by_text`) over fragile
  1-based index counting (`fill_nth_of_role`/`click_nth_of_role`, kept as fallback).
  `press_key` + `wait` drive comboboxes/overlays. **Vision:** the agent may also call
  `take_screenshot()` (viewport JPEG; `full=True` for the whole page) and the image is
  sent to the LLM as `data:image/jpeg;base64,...` — so a vision-capable model can look
  at the page when the aria snapshot is ambiguous. Screenshots are NOT in the `Driver`
  API, so generated scripts can't call them (deterministic runs don't need vision). The
  `Driver` mirrors all manual tools so generated scripts reproduce the manual steps;
  failed tool calls return the real error reason to the agent (bot.py `call_tool`) so it can self-correct.
- **Image URLs:** Google Drive share links (e.g. `/file/d/{id}/view` or `open?id=`) are
  rewritten to a direct-download URL (`https://drive.google.com/uc?export=download&id={id}`)
  via `eventbrite_api.direct_image_url()` before the agent sees them — applied in
  `run_onboarding` and `run_task` (so the manual fill uses the direct URL) and again in
  `scripting.run_script_against_event` (so future scripted runs on a scripted site also fill
  the direct URL). Non-Drive URLs pass through unchanged; the stored event keeps the raw
  link the human entered. **`usp=` query params (sharing/drive_link) are ignored** — only the
  `/file/d/{id}` path segment matters. At onboarding, `check_direct_image_url()` preflights
  the link: an unshared Drive file serves an HTML sign-in wall instead of image bytes, so a
  failed check logs a clear warning ("share the file as Anyone with the link") before the
  agent/calendar site ever hits the opaque "got HTML" failure.
- The script runs **in-process** against a Playwright page via the `Driver` class (`scripting.py`).
  `Driver`
  exposes ONLY a tight allowlisted `api`: `navigate`, `fill`, `click`, `aria`, `wait`,
  `fill_by_role`, `click_by_role`, `fill_by_label`, `fill_by_placeholder`, `click_by_text`,
  `press_key`.
- **Security:** scripts are gated by `validate_script()` (`scripting.py`) before execution — an AST walk that
  rejects imports, disallowed names/calls/dangerous patterns, and requires the `run()` shape.
  This is the defense against **prompt/code injection** (a malicious calendar site trying to
  steer the agent into generating malicious code). The prompt also instructs the agent that
  page text is untrusted DATA. `run_script_against_event` re-runs `validate_script` too, so a
  script loaded from storage is never executed unvalidated.
- **Sandbox (defense in depth):** the validated script then runs inside **bubblewrap** with
  `--clearenv` (no API keys/tokens), `--unshare-net/pid/ipc/uts` (no network, no fork),
  an empty `--tmpfs /` root with only `/usr`, `/lib*`, `/bin`, `/proc`, `/dev` and a
  **whitelist of project files** (`.venv`, `sandbox_runner.py`, `sandbox_framing.py`,
  `event.py`) bound read-only. CPU/memory/fd/process ceilings are **not** bwrap flags —
  bubblewrap has no `--rlimit-*` options — they are set from inside by `sandbox_runner.py`
  via `resource.setrlimit()` (soft == hard, so the untrusted script can't raise them), on top
  of the host's wall-clock `SANDBOX_TIMEOUT` kill. A one-time **preflight** (`_bwrap_preflight`
  in `scripting.py`) runs the real argv with the script swapped for `-c 'import sys'`, so a
  bad bwrap version/flag/mount is reported up front instead of failing mid-onboarding;
  in `auto` mode it logs and falls back to in-process, in `SCRIPT_SANDBOX=1` mode it aborts.
  Everything the script can *do* is
  the 11-op driver surface — served by the **host** over MCP-stdio, against the same review
  page the human sees. Enabled/disabled with `SCRIPT_SANDBOX` (auto|1|0); when bwrap is
  missing it falls back to in-process `exec`. Design + threat model: `docs/sandbox-plan.md`.
- **Transport note (don't re-litigate):** the sandbox uses MCP **stdio with the direction
  inverted** — the host is the *server* on the child's pipes and the child is the *client*
  on its own stdin/stdout. HTTP was rejected because `--unshare-net` leaves no working
  loopback (it would force either dropping net isolation or bringing `lo` up with
  CAP_NET_ADMIN), a listener adds an OS-level surface reachable by any local process, and
  stdio gives free death detection (pipe EOF). Since MCP's stdio framing is symmetric
  newline-delimited JSON-RPC, only the byte-stream *source* is custom (`sandbox_framing.py`).

## Settings versioning / staleness

- `current_settings_version()` = content hash of the **global** settings only:
  `SETTINGS_GLOBAL_FIELDS` (venue, location, open_to_public, event_cost, categories,
  timezone, eventbrite_*). The website_url/organizer fields are **not** in the hash —
  they're per-event prefills now, and scripts read them off the `Event` object, so
  changing their Settings defaults doesn't invalidate scripts.
- Generated scripts record the hash they were built against.
- `website_out()` sets `stale=True` when a scripted website's stored version != current
  settings. Stale websites cannot be run (returns 409) and must be re-onboarded.
- Rationale: scripts take default values (venue, location, cost, categories) for granted, so
  changing those settings invalidates the scripts; organizer/website defaults don't because
  each event carries its own copy.

## Frontend

- Header actions (left to right): **Events / Websites** view switch, then
  **↻ Refresh**, **+ New Event**, **+ Website**, **⚙️ Settings**, and a large round **?** help button.
- **Refresh button:** `POST /api/refresh` re-hydrates in-memory state from the storage
  backend (Google Sheets) so sheet edits show up without restarting. Returns 409 while an
  AI task/onboarding is running (reloading would clobber objects the runner holds). After a
  refresh the frontend re-renders lists + open detail panels.
- **Help modal:** the `?` button opens a "How to use this app" dialog with the basic steps
  (create event → add website → one-time setup with AI + approve → review & submit).
- **Field tooltips:** a small **?** marker sits to the right of many field labels in the
  New Event, New Website, Settings, and Eventbrite-config dialogs. They show on hover only
  (no focus trigger — that caused them to auto-open when a dialog opened). Tip boxes are
  sized to content (`width: fit-content`, capped at 320px) and float via JS so short text
  doesn't leave empty space.
- **Websites view:** lists websites with a colored badge (kind + status). A small legend
  under the header explains the colors; badge text is friendly (Not set up / Setting up /
  Ready / Needs attention / Stale) instead of raw status values. Badge classes:
  `unscripted` (muted), `scripting` (amber), `scripted`/`ready` (green), `error` (red),
  `stale` (amber).
- **Eventbrite site panel:** venue/organizer IDs are configured in a popup dialog
  (`ebConfigModal`, opened by a "Configure venue/organizer" button) with Cancel + Save —
  no inline inputs on the panel. '+ Add event (draft)' sends the selected event as a draft.
- **Embedded browser view:** while a website is being set up or a scripted run is being
  reviewed, a "Live page" panel streams **JPEG screenshots** of the server's real
  Playwright page over a WebSocket (`/api/browser/{id}/ws`). With the default
  **chromium** engine it uses CDP `Page.screencast` — a continuous pipeline where the
  browser streams JPEG frames it's already compositing, so the fps ceiling is far higher
  than per-frame screenshots (Firefox has no CDP screencast, so it falls back to
  `page.screenshot()`). The target rate is configured by `BROWSER_FPS` (default 4) — a
  *ceiling*. `BROWSER_JPEG_QUALITY` (default 80) trades encode speed/frame size against
  sharpness — drop it on constrained hardware. The event poll is kept a fraction of the frame
  budget so it never gates frame production (event batches are capped so a scroll/typing
  flood can't starve frames — the view keeps streaming while you scroll, and clicks flush
  a frame immediately). Transient page/CDP errors degrade to `empty` and the stream stays
  alive (they don't kill the WS, which would freeze the view and let wheels hit the app
  page); Playwright ops + WS sends are timeout-bounded (3–5s) so a hung page or slow
  tunnel can't wedge the handler. The client also keeps the wheel captured while a frame
  was recently shown (reconnect blips never scroll the app) and runs a staleness watchdog:
  open-but-silent sockets (no frame/ping for 6s) are **force-reopened directly** (a
  wedged server never completes the close handshake, so relying on close()/onclose alone
  froze the view until a manual refresh). Sockets are generation-tagged: stale handlers
  are ignored, deliberate closes can't reopen, and reconnects use exponential backoff
  (2s→15s, reset on a successful message). Unchanged frames are deduped (a quiet page just
  gets a lightweight `ping` heartbeat, so bandwidth stays low).
  The stream logs achieved fps every ~2s (`[browser-ws] ... ~X.Xfps sent`). It is
  **view-only** while the AI is working (manual_fill/writing/running) and becomes
  **interactive** during approval phases and scripted-run review: clicks/types/scroll are
  proxied back into the real page (`page.mouse`/`keyboard`) so the human can fix fields,
  solve captchas, and submit the form right from the embedded view. The scripted run's page
  is registered in `LIVE_PAGES` **as soon as it is created** (not after it finishes), so the
  view shows the real run rather than a stale fallback. Scroll events include the cursor
  coordinates and the server moves the pointer there first, so wheeling scrolls where the
  user is looking. Scroll works even while the AI is working (view-only panning):
  the client coalesces wheel deltas (~50ms flush, so continuous scrolling sends one
  accumulated scroll per tick instead of a flood), and
  the server dispatches a *native* `mouse.wheel` at the pointer (falling back to a
  DOM scrollable-ancestor walk) — native wheel scrolls whatever is under the cursor,
  including content inside iframes, which a JS elementFromPoint walk can't reach.
  Native browser popups (date pickers, select dropdowns) are browser-chrome and CANNOT
  appear in the captured stream — after a click lands on one, the server pushes a `hint`
  message telling the human to type/use the keyboard instead.
  Click coordinates are remapped from the image's *intrinsic* pixel
  size (`naturalWidth/Height`), so they stay exact even if the reported viewport
  disagrees (DPI/zoom). **Paste:** Ctrl/Cmd+V (or Edit ▸ Paste) while interactive reads the
  human's OS clipboard via `navigator.clipboard.readText()` and sends the text as a
  `paste` WS event; the server inserts it via Playwright `keyboard.insert_text()` into the
  focused field (fires `input`, so autosuggest works). Copy FROM the page isn't possible —
  the view is a JPEG, so text can't be selected in it.
  A "✓ Done" (primary-styled, like Approve) button closes
  the review page
  (`POST /api/websites/{id}/done`). The image scales to fit; the client remaps clicks and
  scroll using the viewport size sent with each frame, so coordinates stay correct. Live
  pages are tracked in `browser.LIVE_PAGES` (keyed by website_id) with a fallback to the
  agent's own session page during onboarding; the WS endpoint logs hits and close codes so
  proxy/upstream failures are diagnosable (see the nginx note in How to Run).
- **Browser lifecycle:** the agent's browser window closes when its interaction finishes —
  `run_onboarding` shuts the manual-fill session as soon as the human approves it (the script
  runs in its own fresh context afterwards), and both agent paths tear down the session in
  their `finally` (incl. `run_task`, which previously left a window open). A site persisted
  mid-onboarding (crashed while waiting to approve) is reset to *unscripted at startup*, so
  it never comes back "stuck waiting to approve" with dead buttons.
- **Events view:** list events, create event (per-event fields: title, summaries,
  image, dates + website_url/organizer/names/phone/email which **prefill from
  Settings** but are overridable per event), view event detail, start one-off tasks.
- **Edit (events & websites):** `PUT /api/events/{id}` edits the per-event fields
  (title, summaries, image, dates) plus the six organizer/website fields
  (website_url, organizer, *_first/last_name, *_phone_number, *_email_address —
  blank reverts to the Settings default); settings-derived fields (venue, location,
  cost, categories) are untouched; block 409 while a task uses the event; validates
  dates). `PUT /api/websites/{id}` edits name/url (kind is
  immutable; 409 while onboarding runs; changing the URL of a *scripted* site resets it to
  unscripted and clears its routine). The shared New/Edit dialogs switch title/submit label
  via `reset*ModalMode()` / `open*Edit()` helpers; `editingEventId`/`editingWebsiteId`
  select POST vs PUT.
- Cache-busting: `main.py` `index()` route substitutes `{{style_url}}` / `{{app_url}}` with
  `?v=<mtime>` query strings pointing at `/static/...` assets, so frontend edits appear
  without cache issues.
- Known UI quirk fixed: the 1s polls must not rebuild action dropdowns, the Activity log,
  or the sidebar lists while the user is reading/hovering — `app.js` signatures
  (`actionsSig` / `scriptsSig` / `sitesSig` / `eventsListSig`) skip re-rendering when
  nothing about status/phase/scripts/lists changed, and the log is rendered
  append-only (`lastLogLen`/`logWid`): existing message nodes are never touched, so text
  selection and scroll position survive the poll; new messages append below and the
  container only pins to the bottom when the user is already near it.

## Current known issues / to verify

- **Sandbox: implemented and verified in-process, not yet on a deployed host.** The smoke
  test passes on both `SANDBOX_CLIENT` paths and a generated script drove a real Chromium
  page through bwrap (see How to Run + `docs/sandbox-plan.md`). Still unproven on the real
  server: running under systemd as the app user, and the timeout seatbelt killing a hung
  run. Server runs should set `SCRIPT_SANDBOX=1` so isolation can't silently vanish.
- **`deploy/install.sh` has never completed against a real root shell.** Every step was
  rehearsed with stubbed `sudo`/`apt-get`/`systemctl`/`nginx`/`htpasswd` over a real
  `uv sync` venv, which caught several real bugs (clobbered `PORT` default, a backtick in a
  summary heredoc executing a command, `chown` aborting under `set -e`). The apt/systemd
  paths are the remaining unknown; the script stops at the first failure with a message.
- **No automated tests.** Verification is `py_compile`, `node --check`,
  `tools/sandbox_smoke.py`, and the stubbed installer run. A pytest over `validate_script`,
  `sandbox_framing`, and `serialization` would be cheap insurance.
- **Unbounded runtime files.** `logs/llm_usage.jsonl` appends forever and `generated/` grows
  by two files per codegen attempt; there is no rotation or cleanup yet (harmless on a
  rare-use VM, worth adding before a long-lived one).
- **Idle shutdown is designed but not implemented** (see the hosting discussion):
  `AUTO_SHUTDOWN_MINUTES` (0 = off) + `/api/activity` + a guard that refuses to power off
  while a browser stream is open or a runner is active + one sudoers line. Until then a VM
  runs 24/7.
- **TLS is untested** — the installer's `--domain` path runs certbot (and is skipped by
  design when nginx isn't on `:80`), but no real Let's Encrypt issuance has been exercised.
- **Script writing/execution was debugged and fixed** (double-generate in codegen, helper-fn
  validation, aria-strip, click-overlay retry, execution-failure rewrite loop). Verify end-to-end.
- **Agent logs**: `logs/llm_usage.jsonl` records every LLM call (finish_reason, token counts,
  elapsed) incl. failed attempts, and `logs/*.jsonl` captures full onboarding/task transcripts
  (JSONL, one message per line — tool calls and results included). This is a local source of
  truth that survives OpenRouter dashboard cleanup.
- **Eventbrite + Sheets are new and untested** — nothing here has been integration-tested.
- `Driver.fill()` already auto-detects select/checkbox/fill (mirrors the manual agent);
  remaining edge cases are dates/images may need special handling.
- **LLM runaway-generation guard is in place** (`bot.py` `max_tokens=8192` + generous read
  timeout, in response to OpenRouter rows showing multi-tens-of-thousands-token outputs that
  were dropped client-side and later refunded by the provider). Watch `logs/llm_usage.jsonl`
  for `"finish": "length"` — that means max_tokens is cutting a response short.
- **Embedded browser view:** scroll is proxied with pointer-move (native `mouse.wheel`,
  works in all phases incl. view-only panning; iframes handled). If viewing through a
  tunnel ever fails with WS 404/1006, the nginx upgrade headers are missing (see How to
  Run) — or, with ngrok free tier, the connection cap was hit; a possible future fallback
  is HTTP polling of a `/frame` endpoint.
- Frontend-guard notes: the 1s polls rebuild actions/scripts via signatures
  (`actionsSig`/`scriptsSig`) — keep that working. Tooltips open on hover only (a focus
  trigger made them flash open when a dialog opened). The help `?`, legend, and dialog
  markup must stay balanced (quick HTML tag check catches regressions).

## Storage (Google Sheets, optional)

- Storage is **in-memory by default**; `main.py`'s lifespan calls `storage.load()` at boot and
  `storage.stop()` at shutdown, and a 5s watchdog flushes when `mark_dirty()` was called.
  `agents.log_note()` and mutation endpoints call `mark_dirty()`, so agent state changes (status,
  phase, scripts, logs) persist too.
- The **↻ Refresh** button / `POST /api/refresh` re-runs `storage.load()` while the app runs
  (see Frontend section) — this is how sheet-side edits propagate to the running app.
- Enable by setting `GOOGLE_SERVICE_ACCOUNT_JSON` (JSON string **or** path to the JSON file) +
  `EVENTS_SPREADSHEET_ID`. `drive.py` wants worksheets `Events`, `Websites`, `Settings` (created
  automatically on first save).
- **Tasks and website/task logs are memory-only** for now — websites/tasks/events/settings persist.
- `current_settings_version()` hashes only the global settings (venue/location/cost/
  categories/publicity + Eventbrite timezone/category ids) — the six per-event
  organizer/website fields are excluded because they live on each Event. But note:
  sheet-side edits to an Event's per-event fields propagate via ↻ Refresh.
- The Settings sheet holds defaults only; per-event values travel with each Event row.
  Per-event organizer/website columns are in `EVENT_COLS`, so past rows written before
  the split still round-trip (their values are just immutable for old rows that were
  saved with copies of the defaults).

## Config & secrets

- `.venv` is gitignored. The venv symlinks point at `/usr/bin/python3.12` — that interpreter
  must be where dev runs.
- **On a server, env vars come from `/etc/bandshell/bandshell.env`**, loaded by the
  systemd unit's `EnvironmentFile=`. systemd does not inherit a shell, and a `.env`
  file in the repo is *not* read (everything goes through `os.getenv()` at import
  time). `GET /api/health` reports booleans only (`llm_configured`,
  `eventbrite_configured`, `storage_backend`, `sandbox_mode`, ...) — use it to confirm
  the running process picked the values up without ever echoing secrets.
- `OPENROUTER_API_KEY` is read from the env at import time in `state.py`:
  `TOKEN = os.getenv("OPENROUTER_API_KEY")`. The token should NOT be committed.
- `EVENTBRITE_TOKEN` is read in `state.py` too, used only server-side by `eventbrite_api.py`.
- If adding `.gitignore` entries, keep `.venv`, `__pycache__`, and any API keys out.

## Conventions & notes

- All REST endpoints return plain dicts (no Pydantic response models). Body params are
  sometimes cast manually (see `_parse_event_id`).
- IDs are `UUID`. Path params are auto-converted to UUID by FastAPI annotations; body fields
  are strings and need explicit conversion.
- Log messages are appended directly to lists that the frontend render as chat bubbles.
- The project prefers Google Drive / Sheets for eventual persistence (org already uses it).
  The current in-memory registries are a placeholder — a future storage layer should be
  swappable for the drive without touching the rest of the code.