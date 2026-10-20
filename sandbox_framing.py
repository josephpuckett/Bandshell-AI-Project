"""MCP stdio framing over arbitrary pipes — shared by BOTH sandbox halves.

The MCP stdio wire format is symmetric: newline-delimited JSON-RPC 2.0
(`JSONRPCMessage`), one message per line, UTF-8. The SDK ships this framing
twice — once inside `mcp.server.stdio.stdio_server()` (for a *server* bound to
its own process stdin/stdout) and once inline in `mcp.client.stdio.stdio_client()`
(for a *client* that spawns a server subprocess).

We need neither of those convenience shapes: the host owns the bwrap child and
acts as the MCP **server** on the child's pipes, while the child acts as the MCP
**client** on its own stdin/stdout. So both sides need "attach the SDK framing to
a pipe pair I already have" — that's all this module is (~50 lines).

Everything above this layer (tool definitions, `ClientSession`, fastmcp's
`Client`, `Server.run`) is stock SDK; only the byte-stream *source* is custom.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

import anyio
from mcp import types
from mcp.shared.message import SessionMessage


def _encode(message: types.JSONRPCMessage) -> str:
    """Serialize a JSON-RPC message to one wire line (no trailing newline)."""
    return message.model_dump_json(by_alias=True, exclude_none=True)


def _decode(line: bytes | str) -> types.JSONRPCMessage:
    """Parse one wire line into a JSON-RPC message (raises on bad JSON)."""
    return types.JSONRPCMessage.model_validate_json(line)


@contextlib.asynccontextmanager
async def framed_streams(reader, writer):
    """Bind MCP framing to an asyncio (StreamReader, StreamWriter) pipe pair.

    Used by the HOST over the bwrap child's pipes: requests arrive on the
    child's stdout (we read), responses leave on the child's stdin (we write).
    Yields the (read_stream, write_stream) pair that `Server.run()` expects.
    """
    read_send, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_recv = anyio.create_memory_object_stream(0)

    async def pump_in():
        # child stdout -> JSONRPCMessage/SessionMessage stream
        async with read_send:
            while True:
                try:
                    line = await reader.readline()
                except Exception:
                    break
                if not line:
                    break  # EOF: the sandbox exited (this IS our death signal)
                if not line.strip():
                    continue
                try:
                    msg = _decode(line)
                except Exception as exc:  # surface parse errors like the SDK does
                    await read_send.send(exc)
                    continue
                await read_send.send(SessionMessage(msg))

    async def pump_out():
        # JSONRPCMessage/SessionMessage stream -> child stdin
        async with write_recv:
            async for session_message in write_recv:
                try:
                    writer.write((_encode(session_message.message) + "\n").encode())
                    await writer.drain()
                except Exception:
                    break

    async with anyio.create_task_group() as tg:
        tg.start_soon(pump_in)
        tg.start_soon(pump_out)
        try:
            yield read_stream, write_stream
        finally:
            tg.cancel_scope.cancel()


@contextlib.asynccontextmanager
async def framed_stdio():
    """Bind MCP framing to THIS process's stdin/stdout.

    Used by the SANDBOX runner, which is the MCP client on its own inherited
    pipes (the host spawned bwrap and holds the other ends).

    This deliberately uses *asyncio* pipe transports rather than
    `anyio.wrap_file` + blocking `readline()` on a worker thread: a thread
    blocked in `readline()` cannot be cancelled, so any error/exit path hung
    forever waiting for that thread instead of unwinding (a silent deadlock the
    sandbox could never recover from). Non-blocking reads cancel cleanly.

    NOTE: the transports are captured here, *before* the generated script runs.
    `sandbox_runner` redirects `sys.stdout` to stderr while the script executes
    so a stray `print()` can never corrupt the JSON-RPC framing.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    write_transport, write_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer)
    writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)
    try:
        async with framed_streams(reader, writer) as streams:
            yield streams
    finally:
        with contextlib.suppress(Exception):
            read_transport.close()
        with contextlib.suppress(Exception):
            write_transport.close()
