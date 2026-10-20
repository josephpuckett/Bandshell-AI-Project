"""Host side of the sandboxed script runner: the driver **MCP server**.

Direction (deliberate inversion — kept here so nobody "fixes" it):

    host process (this file)                  bwrap child (sandbox_runner.py)
    ─────────────────────────────────  ────────────────────────────────────
    mcp low-level Server               fastmcp Client over its own stdin/out
      tools = the Driver allowlist  ◀── stdio ──▶   api.* → tools/call
      owns the review page +                       runs `run(event, api)`
      LIVE_PAGES (human watches it)

Normally the *client* spawns the *server* (`mcp.client.stdio.stdio_client`). Here
the host is the parent (it spawns `bwrap`) and wants to be the server, so it
binds the low-level `Server` to the child's pipes via `sandbox_framing`.

Bootstrap goes over MCP itself (no fd tricks): the runner's first call is
`get_job`, which returns the event + generated script; its last call is
`report`, which returns the outcome. If the sandbox dies or hangs, the host
sees an EOF on the child's stdout (or its wall-clock timeout fires) and never
gets a `report` — which the caller treats as a failed run.

Security note: the tool surface here is the ONLY thing a sandboxed script can
do. It is hardcoded to the same 11 ops as `scripting.ALLOWED_API` (kept in
lockstep below) and dispatches into the very same `Driver` implementation the
in-process path uses, so a script behaves identically — while having no network,
no filesystem and no host environment of its own.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions

from sandbox_framing import framed_streams
from scripting import Driver

# Tool surface exposed to the sandbox: exact mirror of scripting.ALLOWED_API.
# Deliberately absent: `take_screenshot` (scripts are deterministic/vision-free)
# and anything that could read the filesystem or reach another page.
SANDBOX_TOOLS: dict[str, dict[str, Any]] = {
    "navigate": {
        "description": "Navigate the page to a URL.",
        "schema": {"url": "string"},
    },
    "fill": {
        "description": "Fill/select/check the nth (1-based) element of a role.",
        "schema": {"role": "string", "n": "integer", "value": "string"},
    },
    "click": {
        "description": "Click the nth (1-based) element of a role.",
        "schema": {"role": "string", "n": "integer"},
    },
    "fill_by_role": {
        "description": "Fill by accessible role + name.",
        "schema": {"role": "string", "name": "string", "value": "string"},
    },
    "click_by_role": {
        "description": "Click by accessible role + name.",
        "schema": {"role": "string", "name": "string"},
    },
    "fill_by_label": {
        "description": "Fill the field of a visible label.",
        "schema": {"label": "string", "value": "string"},
    },
    "fill_by_placeholder": {
        "description": "Fill the field of a placeholder.",
        "schema": {"placeholder": "string", "value": "string"},
    },
    "click_by_text": {
        "description": "Click the first element containing text.",
        "schema": {"text": "string"},
    },
    "press_key": {
        "description": "Press a keyboard key (Tab/Enter/Escape/Arrow*).",
        "schema": {"key": "string"},
    },
    "aria": {
        "description": "Read the accessible tree (read-only).",
        "schema": {},
    },
    "wait": {
        "description": "Pause for ms milliseconds.",
        "schema": {"ms": "integer"},
    },
}

# JSON-Schema "type" per python type name used in SANDBOX_TOOLS.
_JSON_TYPES = {"string": "string", "integer": "integer"}

# Hard caps so a runaway script can't abuse the host channel.
MAX_CALLS = 2000
MAX_STRING = 100_000


def _json_schema(props: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {k: {"type": _JSON_TYPES.get(v, "string")} for k, v in props.items()},
        "required": list(props),
        "additionalProperties": False,
    }


def _check_args(name: str, args: dict) -> dict:
    """Coerce + bound tool arguments before they reach Playwright."""
    schema = SANDBOX_TOOLS[name]["schema"]
    out: dict[str, Any] = {}
    for key, kind in schema.items():
        if key not in args:
            raise ValueError(f"{name}: missing argument {key!r}")
        value = args[key]
        if kind == "integer":
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name}: {key!r} must be an integer")
        else:
            text = str(value)
            if len(text) > MAX_STRING:
                raise ValueError(f"{name}: {key!r} is too long")
            out[key] = text
    return out


class SandboxHost:
    """Serves the driver tools to one sandboxed run.

    One instance per run: it holds that run's review `page` (the same object the
    embedded view streams), the event/script payload handed to the client, and
    the outcome the client reports back.
    """

    def __init__(self, page, event_payload: dict, code: str, dispatch=None):
        self.page = page
        self.event_payload = event_payload
        self.code = code
        self.calls = 0
        self.result: dict | None = None  # {'ok': bool, 'error': str}
        self.error: str = ""
        # True once the sandboxed client fetched its job. If the run fails
        # before this, the problem is the sandbox itself (bwrap/env), NOT the
        # generated script — callers must not "fix" the script in that case.
        self.started = False
        # Last transport-level failure (was previously swallowed, which made
        # broken sandboxes indistinguishable from broken scripts).
        self.bridge_error = ""
        # Optional stub dispatcher used by tools/sandbox_smoke.py to exercise the
        # transport without a browser. In production this is None and every call
        # goes to the real Driver against `page`.
        self._dispatch_fn = dispatch

    # -- tool dispatch ------------------------------------------------------
    async def _dispatch(self, name: str, args: dict) -> str:
        """Run one allowlisted op against the review page via the real Driver."""
        if name not in SANDBOX_TOOLS:
            raise ValueError(f"tool {name!r} is not allowed in the sandbox")
        self.calls += 1
        if self.calls > MAX_CALLS:
            raise ValueError(f"too many api calls ({MAX_CALLS} max)")
        kwargs = _check_args(name, args)
        if self._dispatch_fn is not None:
            result = await self._dispatch_fn(name, kwargs)
            return "" if result is None else str(result)
        driver = Driver(self.page)
        method = getattr(driver, name, None)
        if method is None:  # pragma: no cover - ALLOWED_API/Driver drift guard
            raise ValueError(f"driver does not implement {name!r}")
        result = await method(**kwargs)
        return "" if result is None else str(result)

    # -- MCP server ---------------------------------------------------------
    def _build_server(self) -> Server:
        server = Server("bandshell-sandbox")

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            tools = [
                types.Tool(
                    name=name,
                    description=spec["description"],
                    inputSchema=_json_schema(spec["schema"]),
                )
                for name, spec in SANDBOX_TOOLS.items()
            ]
            # Bootstrap/control tools (not part of the script's `api`).
            tools.append(types.Tool(
                name="get_job",
                description="Fetch the event + script for this sandboxed run.",
                inputSchema={"type": "object", "properties": {},
                             "additionalProperties": False},
            ))
            tools.append(types.Tool(
                name="report",
                description="Report the run's outcome.",
                inputSchema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"},
                                   "error": {"type": "string"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            ))
            return tools

        # NOTE: mcp's low-level `call_tool` decorator invokes the handler as
        # `func(tool_name, arguments)` — NOT with a CallToolRequest. (Passing a
        # request object raises TypeError inside the decorator, which it turns
        # into an error response, so the handler silently never runs.)
        @server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict | None = None):
            args = dict(arguments or {})
            try:
                if name == "get_job":
                    self.started = True
                    return [types.TextContent(type="text", text=json.dumps({
                        "event": self.event_payload,
                        "code": self.code,
                    }))]
                if name == "report":
                    self.result = {"ok": bool(args.get("ok")),
                                   "error": str(args.get("error") or "")[:4000]}
                    return [types.TextContent(type="text", text="ok")]
                out = await self._dispatch(name, args)
                return [types.TextContent(type="text", text=out)]
            except Exception as exc:
                # Returned (not raised) so the script can see and react to the
                # failure — matching the in-process Driver's exception surface.
                self.error = f"{type(exc).__name__}: {exc}"
                return types.CallToolResult(
                    isError=True,
                    content=[types.TextContent(type="text", text=self.error)],
                )

        return server

    # -- serving ------------------------------------------------------------
    async def serve_child(self, proc) -> None:
        """Attach the driver server to a spawned bwrap child until it exits.

        Returns when the child closes stdout/ exits (EOF), meaning the run is
        over. Any `report` the child sent is left in `self.result`.
        """
        server = self._build_server()
        options = InitializationOptions(
            server_name="bandshell-sandbox",
            server_version="1.0.0",
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(listChanged=False)),
        )
        async with framed_streams(proc.stdout, proc.stdin) as (read_stream, write_stream):
            try:
                await server.run(read_stream, write_stream, options)
            except Exception as exc:
                # Never swallow: without this, a protocol/setup failure looked
                # exactly like "the sandbox exited" and was undiagnosable.
                self.bridge_error = f'{type(exc).__name__}: {exc}'
                print(f'[sandbox] bridge error: {self.bridge_error}')
