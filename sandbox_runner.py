"""Sandbox-side runner — the only code that executes INSIDE bwrap.

    bwrap ... /app/.venv/bin/python3 /app/sandbox_runner.py

This process is the MCP **client** for the host's driver server. It connects by
binding fastmcp's `Client` to its **own** stdin/stdout (the host spawned us and
holds the other ends of those pipes). That is the inverse of fastmcp's stock
`stdio_client()`, which spawns a server subprocess — we have no spawn rights
inside the sandbox, so we attach to the pipes we were given instead.

Flow:
  1. `get_job`  -> {"event": {...}, "code": "<generated script>"}
  2. exec the script in a scrubbed namespace; its `api` proxy forwards each call
     to the host as an MCP `tools/call`
  3. `report`   -> {"ok": bool, "error": str}

**stdout is reserved for JSON-RPC.** Everything the generated script prints is
redirected to stderr (which the host captures for the log). A stray `print()`
must never be able to desync the framing.

This file deliberately does not import the host's browser/agent stack
(`browser`, `scripting`, `agents`): all it needs is an MCP client + the `Event`
dataclass, so the sandbox ro-binds exactly those files and nothing else.
"""

from __future__ import annotations

import os
import sys

# Banner FIRST, before any heavy import: if the process is killed while
# importing (e.g. the host OOM-kills us on a small VM) this line is the only
# evidence of how far we got. stderr only — stdout is the MCP wire.
print(f'[sandbox-runner] start pid={os.getpid()} py={sys.version.split()[0]}', file=sys.stderr)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Resource ceilings. bubblewrap has no --rlimit-* flags, so the limits are set
# here, inside the sandbox, *before* any untrusted code runs. soft == hard means
# an unprivileged process cannot raise them again — including the script itself.
# Values are passed in by the host via --setenv (see scripting._sandbox_argv).
#
# NOTE: RLIMIT_NPROC is deliberately NOT set. It counts processes/threads per
# *uid across the whole machine*, so on a box that already has many processes it
# makes thread creation fail (and does nothing useful here — `--unshare-pid`
# already prevents leftover processes).
LIMIT_CPU_SECONDS = _env_int('SANDBOX_CPU_SECONDS', 30)
LIMIT_ADDRESS_SPACE = _env_int('SANDBOX_MAX_MEMORY_MB', 1024) * 1024 * 1024
LIMIT_NOFILE = 256


def _apply_limits() -> None:
    """Impose CPU/memory/fd ceilings on this process (best-effort)."""
    import resource

    for which, value in (
        (resource.RLIMIT_CPU, LIMIT_CPU_SECONDS),
        (resource.RLIMIT_AS, LIMIT_ADDRESS_SPACE),
        (resource.RLIMIT_NOFILE, LIMIT_NOFILE),
    ):
        try:
            _soft, hard = resource.getrlimit(which)
            # Never *raise* the inherited hard limit (unprivileged processes
            # can't anyway); just pin soft and hard to our ceiling.
            ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(which, (ceiling, ceiling))
        except (ValueError, OSError):
            pass  # a missing/unsupported limit must not stop the run


import contextlib  # noqa: E402
import json  # noqa: E402
import traceback  # noqa: E402
from datetime import datetime  # noqa: E402

import anyio  # noqa: E402
from mcp import ClientSession  # noqa: E402

from event import Event  # noqa: E402
from sandbox_framing import framed_stdio  # noqa: E402

# Which MCP client to use inside the sandbox:
#   auto (default) = fastmcp when it imports cleanly, else mcp.ClientSession
#   fastmcp        = require fastmcp
#   mcp            = skip fastmcp entirely (lighter; use if `auto` gets OOM-killed)
SANDBOX_CLIENT = (os.environ.get('SANDBOX_CLIENT') or 'auto').strip().lower()

# fastmcp pulls a large dependency tree (pydantic, rich, ...). On a small VM
# importing it can be the peak-memory moment of the whole run, so it is opt-out.
_FastMCPClient = None
_ClientTransport = object
if SANDBOX_CLIENT != 'mcp':
    try:
        from fastmcp import Client as _FastMCPClient
        from fastmcp.client.transports.base import ClientTransport as _ClientTransport
    except Exception as _exc:  # pragma: no cover - environment dependent
        _FastMCPClient = None
        _ClientTransport = object
        print(f'[sandbox-runner] fastmcp unavailable ({type(_exc).__name__}: {_exc});'
              ' using mcp.ClientSession directly', file=sys.stderr)

_client_name = 'fastmcp' if _FastMCPClient is not None else 'mcp'
if SANDBOX_CLIENT == 'fastmcp' and _FastMCPClient is None:
    # Explicitly required but unusable: fail loudly instead of
    # silently downgrading the client.
    raise SystemExit('[sandbox-runner] SANDBOX_CLIENT=fastmcp but import failed')
print(f'[sandbox-runner] client={_client_name} ready', file=sys.stderr)

# Scrubed builtins the generated script may use. Mirrors
# scripting.SAFE_BUILTINS (kept in sync deliberately: the sandbox must not
# import the host's scripting/browser modules just to reuse a dict).
SAFE_BUILTINS = {
    "str": str, "int": int, "float": float, "bool": bool, "len": len,
    "range": range, "list": list, "dict": dict, "set": set, "tuple": tuple,
    "zip": zip, "enumerate": enumerate, "min": min, "max": max, "sum": sum,
    "abs": abs, "round": round, "sorted": sorted, "any": any, "all": all,
    "isinstance": isinstance, "type": type, "repr": repr,
    "print": print,  # goes to stderr (stdout is the MCP wire)
    "True": True, "False": False, "None": None,
}


def _text_of(result) -> str:
    """Extract the text payload from a fastmcp/mcp tool result."""
    content = getattr(result, "content", None)
    if isinstance(content, (list, tuple)) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if text is not None:
            return text
        return str(first)
    data = getattr(result, "data", None)
    if data is not None:
        return data if isinstance(data, str) else json.dumps(data)
    return str(result)


def _make_event(raw: dict) -> Event:
    """Rebuild a real `Event` (datetime fields parsed) from its JSON payload."""
    data = dict(raw)
    for key in ("start_date", "end_date"):
        value = data.get(key)
        if isinstance(value, str):
            try:
                data[key] = datetime.fromisoformat(value)
            except ValueError:
                pass
    known = set(Event.__dataclass_fields__)  # ignore any extra keys
    return Event(**{k: v for k, v in data.items() if k in known})


class _SessionClient:
    """Minimal fastmcp-like client over a raw `ClientSession` (fallback path)."""

    def __init__(self, session):
        self._session = session

    async def call_tool(self, name: str, arguments: dict):
        result = await self._session.call_tool(name, arguments)
        if getattr(result, "isError", False):
            raise RuntimeError(f'{name} failed: {_text_of(result)}')
        return result


class _InvertedStdioTransport(_ClientTransport):  # type: ignore[misc]
    """fastmcp transport over our inherited stdin/stdout (no subprocess spawn)."""

    @contextlib.asynccontextmanager
    async def connect_session(self, **session_kwargs):
        async with framed_stdio() as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     **session_kwargs) as session:
                yield session


def _build_api(client):
    """The `api` object handed to `run(event, api)` — 1:1 with scripting.Driver."""

    async def call(name: str, arguments: dict):
        return await client.call_tool(name, arguments)

    class API:
        async def navigate(self, url: str):
            await call("navigate", {"url": url})

        async def fill(self, role: str, n: int, value):
            await call("fill", {"role": role, "n": n, "value": str(value)})

        async def click(self, role: str, n: int):
            await call("click", {"role": role, "n": n})

        async def fill_by_role(self, role: str, name: str, value):
            await call("fill_by_role",
                       {"role": role, "name": name, "value": str(value)})

        async def click_by_role(self, role: str, name: str):
            await call("click_by_role", {"role": role, "name": name})

        async def fill_by_label(self, label: str, value):
            await call("fill_by_label", {"label": label, "value": str(value)})

        async def fill_by_placeholder(self, placeholder: str, value):
            await call("fill_by_placeholder",
                       {"placeholder": placeholder, "value": str(value)})

        async def click_by_text(self, text: str):
            await call("click_by_text", {"text": text})

        async def press_key(self, key: str):
            await call("press_key", {"key": key})

        async def aria(self) -> str:
            return _text_of(await call("aria", {}))

        async def wait(self, ms: int = 500):
            await call("wait", {"ms": int(ms)})

    return API()


async def _execute(client) -> int:
    """Fetch the job, run the script, report the outcome. Returns the exit code."""
    ok = False
    error = ""
    job = json.loads(_text_of(await client.call_tool("get_job", {})))
    code = job.get("code") or ""
    event = _make_event(job.get("event") or {})
    api = _build_api(client)
    ns: dict = dict(SAFE_BUILTINS)
    ns["__builtins__"] = SAFE_BUILTINS
    # stdout belongs to the MCP wire for the WHOLE script (including any
    # module-level code), so script output goes to stderr instead.
    try:
        with contextlib.redirect_stdout(sys.stderr):
            exec(compile(code, "<generated-script>", "exec"), ns, ns)
            run = ns.get("run")
            if run is None:
                raise RuntimeError("generated script does not define run()")
            await run(event, api)
        ok = True
    except BaseException as exc:  # noqa: BLE001 - reported, not raised
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc(file=sys.stderr)
    with contextlib.suppress(Exception):
        await client.call_tool("report", {"ok": ok, "error": error})
    return 0 if ok else 1


async def amain() -> int:
    _apply_limits()
    if _FastMCPClient is not None:
        async with _FastMCPClient(_InvertedStdioTransport()) as client:
            return await _execute(client)
    async with framed_stdio() as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await _execute(_SessionClient(session))


def main() -> int:
    return anyio.run(amain)


if __name__ == "__main__":
    sys.exit(main())
