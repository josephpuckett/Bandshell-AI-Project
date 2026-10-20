"""Smoke test for the bubblewrap + MCP-stdio sandbox bridge.

Run this on the deployment VM (it needs `bwrap`, and the venv's python3.12):

    .venv/bin/python3 tools/sandbox_smoke.py

It deliberately does NOT touch Playwright: it injects a stub dispatcher into
`SandboxHost`, so it exercises exactly the risky plumbing of the sandbox design:

  1. the bwrap argv (clearenv / unshare / tmpfs-root ordering / ro-binds)
  2. pipe + fd inheritance through bwrap
  3. MCP framing in BOTH directions (host Server <-> fastmcp Client in bwrap)
  4. event delivery over the `get_job` bootstrap (attribute access in the script)
  5. stdout hygiene: a script that `print()`s must not corrupt the JSON-RPC wire
  6. error propagation: a raising script reports ok=False with its message
  7. the wall-clock timeout killing a runaway (`while True`) script

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox_host import SandboxHost          # noqa: E402
from scripting import _event_payload, _sandbox_argv  # noqa: E402
from event import Event                      # noqa: E402
from datetime import datetime                # noqa: E402

FAILURES: list[str] = []


def describe_exit(rc) -> str:
    """Decode an exit status; bwrap reports signalled children as 128+N."""
    if rc is None:
        return "still running"
    sig = -rc if rc < 0 else (rc - 128 if rc > 128 else None)
    if sig is not None and sig > 0:
        try:
            return f"killed by {signal.Signals(sig).name} ({rc})"
        except ValueError:
            return f"killed by signal {sig} ({rc})"
    return f"exit code {rc}"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def _sample_event() -> Event:
    return Event(
        title="Summer Concert",
        short_summary="short",
        long_summary="long",
        categories=["Music"],
        image="https://example.com/a.jpg",
        start_date=datetime(2026, 8, 20, 19, 0),
        end_date=datetime(2026, 8, 20, 21, 0),
        venue="Bandshell",
        location="Long Beach",
        open_to_public=True,
        event_cost=0.0,
        website_url="https://example.com",
        organizer="Live at The Shell",
        organizer_first_name="Steve",
        organizer_last_name="Pruitt",
        organizer_phone_number="555",
        organizer_email_address="a@b.c",
    )


async def run_case(code: str, timeout: float, dispatch=None):
    """Spawn one sandboxed run; return (host, exit_code, timed_out)."""
    argv, why = _sandbox_argv()
    if argv is None:
        raise RuntimeError(f"sandbox unavailable: {why or 'bwrap not installed'}")

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    host = SandboxHost(None, _event_payload(_sample_event()), code, dispatch=dispatch)
    stderr_lines: list[str] = []

    async def drain():
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                stderr_lines.append(text)
                print(f"       stderr| {text}")

    d = asyncio.create_task(drain())
    timed_out = False
    try:
        await asyncio.wait_for(host.serve_child(proc), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        with contextlib.suppress(Exception):
            if proc.returncode is None:
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 10)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(d, 5)
    return host, proc.returncode, timed_out


async def main() -> int:
    argv, why = _sandbox_argv()
    if argv is None:
        print(f"SKIP: bubblewrap is not usable here ({why or 'bwrap not found'})")
        return 2

    print("bwrap argv:")
    print("  " + " \\\n  ".join(argv) + "\n")

    calls: list[tuple[str, dict]] = []

    async def stub(name, args):
        calls.append((name, args))
        return f"stub:{name}"

    # --- 1/2/3/4/5: happy path, includes a stray print() -------------------
    # NOTE: uses only operations that work under the scrubbed builtins (the same
    # restrictions the in-process path has — e.g. no strftime, no __import__).
    code_ok = (
        "async def run(event, api):\n"
        "    print('this must not corrupt the wire')\n"
        "    await api.wait(5)\n"
        "    await api.click_by_text(str(event.title))\n"
        "    await api.fill_by_label('Title', str(event.start_date))\n"
    )
    host, rc, timed_out = await run_case(code_ok, timeout=30, dispatch=stub)
    check("sandboxed runner started (get_job served)", host.started,
          f"rc={describe_exit(rc)} — if this fails before the banner appears, the "
          "runner died while importing (try SANDBOX_CLIENT=mcp)")
    check("happy path reported ok", bool(host.result and host.result.get("ok")),
          f"result={host.result} rc={describe_exit(rc)}")
    check("exit code 0", rc == 0, f"rc={describe_exit(rc)}")
    check("api calls reached the host", len(calls) == 3, f"calls={calls}")
    check("event attributes crossed get_job",
          any(a.get("text") == "Summer Concert" for _, a in calls),
          f"calls={calls}")
    check("datetime stayed a datetime in the sandbox",
          any("2026-08-20" in str(a.get("value")) for _, a in calls),
          f"calls={calls}")

    # --- 6: error propagation --------------------------------------------
    # An AttributeError on the api proxy: raised by Python itself, so it needs no
    # name from the scrubbed builtins (the in-process path behaves the same).
    code_err = (
        "async def run(event, api):\n"
        "    await api.wait(1)\n"
        "    await api.boom_from_script()\n"
    )
    host, rc, _ = await run_case(code_err, timeout=30, dispatch=stub)
    check("failing script reports ok=False",
          bool(host.result and host.result.get("ok") is False),
          f"result={host.result}")
    check("failure message propagates",
          "boom_from_script" in (host.result or {}).get("error", ""),
          f"result={host.result}")
    check("failing script exits non-zero", rc not in (0, None), f"rc={describe_exit(rc)}")

    # --- 7: runaway script is killed by the wall clock --------------------
    code_hang = (
        "async def run(event, api):\n"
        "    while True:\n"
        "        pass\n"
    )
    t0 = asyncio.get_running_loop().time()
    host, rc, timed_out = await run_case(code_hang, timeout=5, dispatch=stub)
    elapsed = asyncio.get_running_loop().time() - t0
    check("runaway script times out", timed_out, f"timed_out={timed_out}")
    check("timeout returns promptly (<15s)", elapsed < 15, f"elapsed={elapsed:.1f}s")
    check("no result reported on timeout", host.result is None,
          f"result={host.result}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: " + ", ".join(FAILURES))
        return 1
    print("all sandbox checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
