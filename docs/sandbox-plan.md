# Sandboxed script execution — implementation plan

Status: **implemented** (`sandbox_framing.py`, `sandbox_host.py`, `sandbox_runner.py`;
wired into `scripting.run_script_against_event`). Verification on the VM is pending —
run `python3 tools/sandbox_smoke.py` there. In-process `exec` remains the fallback and is
used whenever `bwrap` is unavailable (or `SCRIPT_SANDBOX=0`).
Owner: bandshell/bandshell-calendar.

---

## 0. Why and what

Generated agent scripts (the `async def run(event, api)` routines) currently `exec` in
the **same host process** that holds the Eventbrite token, the Google-SA credentials and
the Playwright browser. A malicious calendar page could steer the agent's manual fill, and
from there the generated code, into anything `Driver` exposes — and `Driver` runs
in-process with full host privileges.

The defense we already have is the AST **allowlist** in `scripting.py`/`scripting.py`
(`ALLOWED_API`, injectable tool surface). That stays as the first gate. This plan adds
real **isolation on top**: the generated script executes in a **bubblewrap sandbox** with
no network, no filesystem writes, no host env, no host test-running process — using the
**MCP protocol over the sandbox's own stdio** as the only channel back to the host
driver.

Threat model this defends: prompt-injection-malicious-site → steers agent → agent hands
the human a poisoned script → human approves → **script runs with full host powers**.
After this change the poisoned script runs with: bwrap `--unshare-all` (no net, no pid,
no ipc, no uts, no cgroup), a fresh tmpfs root, a crafted read-only view of system
libs, a scrubbed environment (no secrets), and **only** the `api` MCP tool surface to
touch anything.

Deliberately **not** sandboxed (unchanged): the *manual* agent fill, the review flow,
`LIVE_PAGES` streaming, and the final human submit. The scripted run's *page* stays
host-side and in `LIVE_PAGES`; the review embedding, `review_contexts`, and manual
Done flow are untouched by design.

## 1. Direction / topology (the part that took the research)

**Decision: stdio, with the HOST as the MCP *server* and the SANDBOX as the MCP
*client* — the "reversed" direction — over the child's inherited pipes.**

Why this (not HTTP, not in-memory):

- **bwrap `--unshare-net`.** The headline isolation flag we actually want (`--unshare-all`
  implies net too). If the child connects over TCP to `127.0.0.1`, we have to either drop
  `--unshare-net` (a real regression, and defeats the netns the whole design leans on) or
  bring `lo` up inside the fresh netns (`ip link set lo up` + `CAP_NET_ADMIN` + netlink
  on the now-`root` netns — all of which need root/pivots the VM may not grant an
  unprivileged bwrap). stdio needs **no network at all** — it rides the inherited
  pipes (`bwrap` passes fds by default), so `--unshare-net` works unchanged.
- **Death detection for free.** stdio over the child's pipes = when bwrap dies the host's
  read pipe hits EOF instantly. Combine with `--die-with-parent` and the host loop gets
  natural "the sandbox is gone" with zero polling, no port lifecycle, no half-open
  sockets to time out. The sibling HTTP option needs heartbeat timeouts and port
  cleanup; stdio gives EOF.
- **No listener, no port.** Nothing to bind, no loopback race, no firewall rules, no
  exposure via the nginx/ngrok tunnel (there's no TCP surface at all), no conflicting
  with uvicorn on :8000.
- **Where HTTP *would* win** (and why we reject it): if we *had* to keep the child's
  connection fully stateless/curl-able, or if we genuinely needed the child to be able to
  *reach* the parent by URL. We don't. HTTP's one genuine advantage — "anybody can point
  a stock client at a URL" — is exactly the wrong property for an allowed surface we want
  to keep host-only.

The only cost of stdio here: **fastmcp's stock `StdioTransport`/`stdio_client` bundles
spawn + stream creation and assumes the client spawns the server.** We invert that. The
MCP stdio wire format is symmetric (newline-delimited JSON-RPC over a byte pipe); the
transport is just "attach framing to a pair of streams". So we:
- keep fastmcp's `Client` machinery in the sandbox (it can connect to a session built
  over any streams — `fastmcp.Client` is constructed over a `ClientTransport`, and
  `ClientSession` takes arbitrary read/write streams);
- provide a small custom transport on each side that binds the **already-inherited**
  child pipes (host spawns the child; host's `Driver` server runs IN-PROCESS bound to
  the child's pipes via the low-level `mcp` SDK's `Server.run(read_stream, write_stream)`
  — which accepts any anyio byte streams, confirmed in the installed copy).

So both sides use the SDK's own framing; only the byte-stream *source* is custom
(~30 lines/side). This is "using fastmcp/stdio", just with the spawn direction flipped —
which is the honest reason stdio is the right call for a host-owned sandbox.

## 2. Component layout

```
host (bandshell-calendar)                      child (bwrap'd)
┌─────────────────────────────┐    stdio      ┌─────────────────────────────┐
│ sandbox_host.py              │◀────────────▶│ sandbox_runner.py           │
│  mcp low-level Server        │  JSON-RPC    │  fastmcp Client over its    │
│  tools = the 11 Driver ops   │   lines      │  own stdin/stdout           │
│  owns page + LIVE_PAGES      │              │  runs `run(event, api)`     │
└─────────────────────────────┘              └─────────────────────────────┘
sandbox_framing.py  ← the ~50-line newline-JSON-RPC framer shared by both sides
```

Bootstrap rides the MCP channel itself (no fd 3): the runner's first call is
`get_job` (returns event + script) and its last is `report` (returns ok/error). If the
sandbox dies or hangs, the host sees EOF on the child's stdout (or its wall-clock
timeout) and never gets a `report` — treated as a failed run.

- **`sandbox_host.py`** (host, new): the host-side MCP *server*. Runs the low-level
  `mcp.Server` bound to the child's pipes (via anyio streams over `asyncio` pipes from
  `asyncio.create_subprocess_exec(... bwrap ...)`). Implements ONLY the driver tool
  surface: `navigate`, `fill`, `click`, `fill_by_role`, `click_by_role`,
  `fill_by_label`, `fill_by_placeholder`, `click_by_text`, `press_key`, `wait`, `aria`
  — the exact `ALLOWED_API` set, each backed by the existing `Driver`/`manager` internals
  that already drive the live page. **No screenshot tool, no `take_screenshot`** — the
  scripted path is deterministic and vision-free by design (matching the existing
  "no vision in scripts" stance). It mounts onto the child pipes in-process, so the
  review page + `LIVE_PAGES` + embedded WS streaming keep working untouched.
- **`sandbox_runner.py`** (replaces the `exec` inside `scripting.run_script_against_event`):
  runs inside bwrap. Creates a fastmcp `Client` over its own stdin/stdout (custom
  transport over its inherited pipes), fetches the event + script with `get_job`,
  `exec`s the script with a scrubbed namespace (a local copy of `SAFE_BUILTINS`) + an
  `api` that forwards each call as an MCP `tools/call`, then sends `report` and exits. All stdout is reserved for MCP framing; all
  `print()`/logs go to stderr.
- **`state.py`**: `SCRIPT_SANDBOX=0` env switch; bwrap auto-detection.
- **`scripting.py`**: the execution path swaps in-process `exec` → spawn
  `bwrap ... python3 sandbox_runner.py` + `sandbox_host.py` wiring, keeping the
  existing `run_script_against_event(event, script)` return contract and the review page
  in `LIVE_PAGES`.
- **`browser.py`**: untouched (still owns the page + LIVE_PAGES registry; host-side
  runner reuses `manager`'s existing `get_page`/`get_session`).

## 3. bwrap command (in the plan, exact)

```bash
bwrap \
  --clearenv --setenv PATH /usr/bin:/bin --setenv HOME /tmp \
  --die-with-parent \
  --unshare-net --unshare-pid --unshare-ipc --unshare-uts \
  --unshare-cgroup-try --unshare-user-try --new-session \
  --tmpfs / \
  --ro-bind /usr /usr \
  --ro-bind-try /lib /lib --ro-bind-try /lib64 /lib64 \
  --ro-bind-try /bin /bin --ro-bind-try /sbin /sbin \
  --proc /proc --dev /dev --tmpfs /tmp \
  --ro-bind <repo>/.venv /app/.venv \
  --ro-bind <repo>/sandbox_runner.py /app/sandbox_runner.py \
  --ro-bind <repo>/sandbox_framing.py /app/sandbox_framing.py \
  --ro-bind <repo>/event.py /app/event.py \
  --chdir /app \
  /app/.venv/bin/python3 /app/sandbox_runner.py
```

(No `--rlimit-*` — bubblewrap has none; see the limits note below. The interpreter is
invoked at its *bound* path `/app/.venv/bin/python3`, not the host path.)

Ordering correctness (the user's original snippet had the ordering backwards):
`--tmpfs /` must come FIRST so the per-path ro-binds land on top of the empty root, not
under a tmpfs that shadows them. (Their draft mounted `--tmpfs /` last and would have
hidden `/usr`/`/lib`.) Then ro-bind `/usr` (Debian's python3.12 stdlib + the dynamic-loader
`.so`s) plus `/proc`, `/dev` and a tmpfs `/tmp`; only the whitelisted project files are
bound on top. Everything else in the repo — `logs/`, `generated/`, `misc/`, frontend, the
host's site-packages outside the venv — is invisible. `/etc` is absent entirely.

Bootstrap is **not** an fd: the runner's first MCP call is `get_job`, which returns the
event + script (and `report` sends the outcome back), so there is no fd-3 pipe to inherit
and nothing to pre-write before exec.

Env scrub (belt + suspenders on top of --clearenv): also strip `OPENROUTER_*`,
`EVENTBRITE_*`, `GOOGLE_*`, `*_API_KEY` from the inherited environment by launching
with a whitelist.

**Resource limits are NOT bwrap flags** — bubblewrap (0.13.1 and earlier) has no
`--rlimit-*` options, and passing them fails with `Unknown option --rlimit-cpu`. The
ceilings are applied from *inside* the sandbox by `sandbox_runner.py` via
`resource.setrlimit()` before any untrusted code runs (soft == hard so the script
cannot raise them): `RLIMIT_CPU` 30s, `RLIMIT_AS` 2GiB, `RLIMIT_NOFILE` 256,
`RLIMIT_NPROC` 64. The host additionally enforces a wall-clock `SANDBOX_TIMEOUT` and
kills the child.

A one-time **preflight** (`_bwrap_preflight()` in `scripting.py`) executes the real
argv with the script swapped for `-c 'import sys'`, so an unsupported flag, a missing
mount or a namespace-permission problem is reported immediately rather than as an
opaque mid-onboarding failure. In `auto` mode it logs and falls back to in-process; in
`SCRIPT_SANDBOX=1` mode it aborts the run.

## 4. Wire framing (both sides, one small helper)

Both sides share the stdio framing the SDK already defines. We reuse it rather than
reinventing: read side = newline-delimited JSON-RPC; write side = newline-delimited
JSON-RPC. Implementation uses the mcp SDK's public stream machinery:
- host: `mcp.Server(...).run(read_stream, write_stream)` where `read_stream`/`write_stream`
  are anyio byte streams over the child's stdout/stdin pipes.
- sandbox: fastmcp `Client` over `anyio` streams wrapping its own stdin/stdout
  (the inherited child pipes).

## 5. Security review (what the sandbox can / cannot do)

| Capability | Allowed in sandbox | Notes |
|---|---|---|
| Navigate / fill / click / aria on the review page | ✅ via `api.*` | host-side Driver running against the real Playwright page — the ONLY touch point |
| Read host env (secrets) | ❌ | `--clearenv` + explicit whitelist + unshare-env; `os.environ` inside the sandbox is empty except PATH/HOME/PYTHONUTF8 |
| Read host filesystem | ❌ | `--tmpfs /` root; only /usr /proc /dev bound ro |
| Write to disk | ❌ | everything is ro-bind + tmpfs (nothing persists; /tmp is tmpfs) |
| Network | ❌ | `--unshare-net` (we can even go `--unshare-all`) |
| Spawn processes / fork | ❌ | `--unshare-pid` (PID 1) |
| Stay alive past parent | ❌ | `--die-with-parent` |
| Do crypto (CPU burn) | bounded | `RLIMIT_CPU` 30s (set in the runner) |
| Memory | bounded | `RLIMIT_AS` 2GiB (set in the runner) |
| Read a file via Playwright | ❌ | `api` exposes no file/fs tool |
| Exfiltrate event/page data | ❌ | no net; output = MCP tool results only, which the host renders in its log |

**The generated script itself** is the untrusted principal. It can call `api.navigate`,
`api.fill`, etc. — but only against the review page, only through the allowlisted tools,
and with no network/fs. An injected "delete my DB" script has no DB; "mint a token" has
no token; "curl exfil" has no net. The *bootstrap* (`get_job`: event + script) is the only
input; everything else is host-enforced.

**Limits / stub-outs (deliberate, keep):**
- `take_screenshot` excluded from sandbox tools (deterministic scripts don't need vision;
  matches `ALLOWED_API` + the no-aria-`strip_aria` rule).
- The `Driver`/`ALLOWED_API` surface is unchanged so any script written before this landed
  still passes `validate_script` and runs identically (now sandboxed).

## 5b. Implementation notes (bugs found while verifying)

1. **`Server.call_tool` handler shape.** In mcp 1.28 the low-level `call_tool`
   decorator invokes the handler as `func(tool_name, arguments)` — *not* with a
   `CallToolRequest`. Taking `req` raises inside the decorator, which converts it to an
   error response, so the handler silently never runs (the client then hangs/fails with
   no useful message). Handlers must be `async def handler(name, arguments)`.
2. **Don't block a worker thread on stdin.** The first client-side framer used
   `anyio.wrap_file(...)` + `async for line in stdin`, whose blocking `readline` runs on a
   thread that *cannot be cancelled* — so any error/exit path deadlocked instead of
   unwinding (the sandbox never exited; the host eventually killed it and reported a
   confusing SIGKILL/137). `framed_stdio()` now builds asyncio pipe transports
   (`connect_read_pipe`/`connect_write_pipe`), which cancel cleanly.
3. **`RLIMIT_NPROC` is a footgun** and is not set: it counts threads per *uid on the whole
   machine*, so on a busy host it breaks thread creation. `--unshare-pid` already covers
   process escape.

## 6. Verification status

Rehearsed in a real environment (bwrap 0.13.1 + uv-managed CPython 3.12.15 + fastmcp 3.4.4
+ mcp 1.28.1 + Chromium): items 1–3 **pass**, on both `SANDBOX_CLIENT` paths, with all 12
`tools/sandbox_smoke.py` checks green. Items 4–5 need the real deployment (systemd, the
review flow against a live site), hence still open.

| # | Check | Status |
|---|-------|--------|
| 1 | bwrap flags, `--unshare-net`, `--die-with-parent`, `--tmpfs /` ordering | ✅ verified |
| 2 | Bootstrap over MCP (`get_job` / `report`, no fd tricks) | ✅ verified |
| 3 | MCP stdio framing both ways; script fills a real page field | ✅ verified |
| 4 | Timeout seatbelt kills a `while True` script, no hung context | ⬜ smoke-tested with a stub dispatcher only |
| 5 | Reviewer flow identical via sandbox vs in-process (LIVE_PAGES, embed, Done) | ⬜ needs a real onboarding |

## 7. Docs (updated when it shipped)

Done, keep in sync:

- `README.md` — sandboxing paragraph (bwrap + stdio, env vars, `SCRIPT_SANDBOX`), the
  installer section, and the troubleshooting table (`exit 137` → `SANDBOX_CLIENT=mcp`).
- `AGENTS.md` — the real component names (`sandbox_framing` / `sandbox_host` /
  `sandbox_runner`), the stderr-only rule for the runner's non-MCP output, and the framing
  inversion (**host = MCP server**, child = client) so nobody re-litigates stdio vs HTTP.

### Bugs this plan did not anticipate (found during E2E)

1. **bubblewrap has no `--rlimit-*` flags** — limits are applied inside the runner via
   `resource.setrlimit()` (soft == hard).
2. **mcp 1.28's `@server.call_tool` decorator calls `func(name, arguments)`**, not
   `func(request)`. The wrong signature raised `TypeError` inside the SDK, the body never
   ran, and the resulting error response deadlocked the client's framer
   (`anyio.wrap_file` + blocking `readline` on an uncancellable thread) → the run hung and
   was killed as `137` with no traceback. Two fixes: correct handler signature, and
   `loop.connect_read_pipe` / `connect_write_pipe` instead of wrapping `sys.stdin`.
3. **A uv-managed interpreter lives outside `/usr`**, and the venv symlink chain hops
   through a version-less directory (`cpython-3.12-…` → `cpython-3.12.15-…`). `_sandbox_argv`
   walks the chain and ro-binds every non-`/usr` prefix at its literal path.
4. **`SANDBOX_ERROR_PREFIX` has to be the first token** of the message, or `agents.py`'s
   `startswith()` check misses it and infra failures get handed to the LLM as script bugs
   (8 pointless rewrites).
