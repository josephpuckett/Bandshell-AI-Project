"""Safe execution of generated browser scripts.

A generated script is an `async def run(event, api)` that drives a Playwright
page through a tight, allowlisted `api` interface (the `Driver`). Scripts are
gated by `validate_script()` before execution to defend against prompt/code
injection from malicious page content.

For now scripts run in-process. Later this may move behind a sandbox
(bubblewrap) via the same tight `api` interface, so only this module changes.
"""
import ast
import asyncio
import contextlib
import shutil
import signal
import subprocess
from dataclasses import asdict
from dataclasses import replace as dataclass_replace
from pathlib import Path

from browser import manager, set_live_page
from event import Event
from eventbrite_api import direct_image_url
from state import (BWRAP, SANDBOX_CLIENT, SANDBOX_CPU_SECONDS,
                   SANDBOX_MAX_MEMORY_MB, SANDBOX_TIMEOUT, SCRIPT_SANDBOX)
from website import Script


VIEWPORT = (1280, 850)  # desktop-size viewport for the embedded browser view

# Where the sandbox root lives inside bwrap (nothing else from the repo is bound).
SANDBOX_DIR = '/app'

# Prefix for failures that mean "the sandbox could not run at all" rather than
# "the generated script is wrong". Callers must NOT feed these back to the LLM
# as a script bug (the code never executed), so they abort with a site error.
SANDBOX_ERROR_PREFIX = 'sandbox-unavailable:'


class Driver:
    """The only surface area a generated script can touch.

    Exposes a small allowlisted set of Playwright operations. Keeping this
    interface tight means a future sandbox swap is contained to this module.
    """

    def __init__(self, page):
        self._page = page

    async def navigate(self, url: str):
        await self._page.goto(url)

    async def fill(self, role: str, n: int, value: str):
        # Mirror the manual agent's fill_nth_of_role: auto-detect whether the
        # element is a select, checkbox/radio, or a plain text field.
        locator = self._page.get_by_role(role).nth(n - 1)
        return await self._fill_locator(locator, value)

    async def _fill_locator(self, locator, value):
        """Fill/select/check a single locator, auto-detecting element type."""
        el = await locator.element_handle()
        tag = (await el.get_attribute('tagName') or '').lower()
        type_attr = (await el.get_attribute('type') or '').lower()

        if tag == 'select':
            await locator.select_option(value)
            return
        if type_attr == 'checkbox' or type_attr == 'radio':
            should_check = str(value).strip().lower() in ('true', '1', 'yes', 'on', 'checked')
            if bool(await locator.is_checked()) != should_check:
                await locator.check() if should_check else await locator.uncheck()
            return
        await locator.fill(str(value))

    async def click(self, role: str, n: int):
        locator = self._page.get_by_role(role).nth(n - 1)
        return await self._click_locator(locator)

    async def _click_locator(self, locator):
        try:
            await locator.click(timeout=6000)
        except Exception:
            # A modal / autosuggest overlay is likely intercepting the pointer.
            # Try dismissing it with Escape, then retry once before giving up.
            await self._page.wait_for_timeout(250)
            try:
                await self._page.keyboard.press('Escape')
            except Exception:
                pass
            await self._page.wait_for_timeout(350)
            await locator.click(timeout=15000)

    async def fill_by_role(self, role: str, name: str, value: str):
        """Fill/select/check the element of a role by accessible name."""
        return await self._fill_locator(self._page.get_by_role(role, name=name), value)

    async def click_by_role(self, role: str, name: str):
        """Click the element of a role by accessible name."""
        return await self._click_locator(self._page.get_by_role(role, name=name))

    async def fill_by_label(self, label: str, value: str):
        """Fill the field associated with a visible label."""
        return await self._fill_locator(self._page.get_by_label(label), value)

    async def fill_by_placeholder(self, placeholder: str, value: str):
        """Fill the field whose placeholder matches."""
        return await self._fill_locator(self._page.get_by_placeholder(placeholder), value)

    async def click_by_text(self, text: str):
        """Click the first element containing the given text."""
        return await self._click_locator(self._page.get_by_text(text).first)

    async def press_key(self, key: str):
        await self._page.keyboard.press(key)

    async def aria(self) -> str:
        return await self._page.aria_snapshot()

    async def wait(self, ms: int = 500):
        await self._page.wait_for_timeout(ms)


SAFE_BUILTINS = {
    'str': str, 'int': int, 'float': float, 'bool': bool, 'len': len,
    'range': range, 'list': list, 'dict': dict, 'set': set, 'tuple': tuple,
    'zip': zip, 'enumerate': enumerate, 'min': min, 'max': max, 'sum': sum,
    'abs': abs, 'round': round, 'sorted': sorted, 'any': any, 'all': all,
    'isinstance': isinstance, 'type': type, 'repr': repr,
    'print': lambda *a: None,
    'True': True, 'False': False, 'None': None,
}

ALLOWED_API = {
    'navigate', 'fill', 'click', 'aria', 'wait',
    'fill_by_role', 'click_by_role', 'fill_by_label',
    'fill_by_placeholder', 'click_by_text', 'press_key',
}
# NOTE: 'aria' stays allowed for backward-compat with approved scripts written
# before codegen started stripping api.aria() calls. It only reads a page
# snapshot (no JS exec / network / file access), so it adds no injection
# surface; the codegen prompt forbids it in new scripts and strip_aria removes
# it during generation.
ALLOWED_CALLS = set(SAFE_BUILTINS) - {'True', 'False', 'None'}
ALLOWED_NAMES = ALLOWED_CALLS | {'event', 'api', 'True', 'False', 'None'}
DANGEROUS = [
    '__import__', '__builtins__', 'subprocess', 'os.system', 'popen(',
    'shell=True', '.read(', 'open(', 'eval(', 'exec(', 'compile(', 'base64',
]


def validate_script(code: str):
    """Gate generated scripts before execution (defense against prompt/code injection)."""
    low = code.lower()
    for token in DANGEROUS:
        if token in low:
            return False, f'disallowed pattern: {token!r}'

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f'syntax error: {e}'

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return False, 'imports are not allowed'

    run_nodes = [n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == 'run']
    if not run_nodes:
        return False, 'missing async def run(event, api)'
    run_node = run_nodes[0]

    # Names that may be *called* because they're user-defined helpers or `run`.
    helper_names = set()
    allowed = set(ALLOWED_NAMES)
    for node in ast.walk(run_node):
        # Collect locally-assigned names (assignments, loop/comprehension vars,
        # function params) plus user-defined helper function names.
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            allowed.add(node.id)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            allowed.add(node.name)
            helper_names.add(node.name)
            for a in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                allowed.add(a.arg)
            for p in (node.args.vararg, node.args.kwarg):
                if p is not None:
                    allowed.add(p.arg)

    for node in ast.walk(run_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in allowed:
                return False, f'disallowed name: {node.id}'
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                if fn.id not in ALLOWED_CALLS and fn.id != 'run' and fn.id not in helper_names:
                    return False, f'disallowed call: {fn.id}()'
            elif isinstance(fn, ast.Attribute):
                root = _root_name(fn)
                if root == 'api' and fn.attr not in ALLOWED_API:
                    return False, f'disallowed api call: api.{fn.attr}()'
                elif root is not None and root not in allowed:
                    return False, f'disallowed call on {root}'
    return True, 'ok'


def _root_name(node):
    """Root identifier name of an attribute chain (or None if literal)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def execute_script_in_process(script: Script, event: Event):
    """Load a validated generated script. Returns (run_fn, globals_ns, msg)."""
    ns = dict(SAFE_BUILTINS)
    ns['__builtins__'] = SAFE_BUILTINS
    code = compile(script.code, '<generated-script>', 'exec')
    exec(code, ns, ns)
    run = ns.get('run')
    if run is None:
        return False, None, 'generated script does not define run()'
    return run, ns, 'ok'


async def run_script_against_event(script: Script, event: Event, live_key=None):
    """Open a fresh page, run the script against it, return (ok, msg, page, context).

    Exceptions raised by the script (e.g. a combobox timing out) are caught and
    returned as (False, error_msg, None, None) so the caller can ask the agent to
    rewrite, instead of crashing the whole onboarding.

    The script itself is executed inside a **bubblewrap sandbox** when available
    (`SCRIPT_SANDBOX=auto`), with the host serving only the allowlisted driver
    tools to it over MCP-stdio. The page stays here on the host, so the review
    flow / embedded view / LIVE_PAGES are unchanged. When bwrap is missing (or
    SCRIPT_SANDBOX=0) the script `exec`s in-process exactly as before.

    If `live_key` is given (a website_id), the page is registered in
    browser.LIVE_PAGES as soon as it is created, so the embedded view streams
    the *actual* run as it happens instead of a stale fallback page.
    """
    # Scripts read event.image at run time; hand them a directly-fetchable URL
    # (Drive share links rewritten), matching what the agent saw during onboarding.
    event = dataclass_replace(event, image=direct_image_url(event.image))

    # Defense in depth: `validate_script` runs during codegen, but nothing
    # guaranteed it ran for a script loaded from storage.
    valid, reason = validate_script(script.code)
    if not valid:
        return False, f'script rejected by validate_script: {reason}', None, None

    # Decide how to execute BEFORE opening a browser context. The probe may run
    # bwrap once (cached), so keep it off the event loop.
    argv, why = await asyncio.to_thread(_sandbox_argv)
    if argv is None and why:
        # Sandbox was explicitly required but can't be used: fail loudly rather
        # than silently downgrading to unsandboxed execution.
        return False, SANDBOX_ERROR_PREFIX + ' ' + why, None, None

    context = await manager.browser.new_context()
    try:
        page = await context.new_page()
        await page.set_viewport_size({'width': VIEWPORT[0], 'height': VIEWPORT[1]})
        if live_key is not None:
            set_live_page(live_key, page)

        if argv is not None:
            ok, msg = await _run_sandboxed(page, script, event, argv)
            if not ok:
                await context.close()
                return False, msg, None, None
            return True, 'executed', page, context

        api = Driver(page)
        run, _, err = execute_script_in_process(script, event)
        if run is None:
            await context.close()
            return False, err, None, None
        await run(event, api)
        return True, 'executed', page, context
    except Exception as exc:
        try:
            await context.close()
        except Exception:
            pass
        return False, str(exc), None, None


# ---------------------------------------------------------------------------
# Sandboxed execution (bwrap + MCP-stdio driver bridge)
# ---------------------------------------------------------------------------
# Cached result of the one-time bwrap preflight: None = not yet checked.
_BWRAP_PREFLIGHT: tuple[bool, str] | None = None


def _bwrap_preflight(argv) -> tuple[bool, str]:
    """Check ONCE that this bwrap + these flags + these mounts actually work.

    Runs the real argv with the generated script replaced by `-c 'import sys'`,
    so any unsupported flag, missing mount or namespace permission problem is
    reported here — instead of surfacing as an opaque failure in the middle of
    an onboarding run. Cached: the probe costs one short process per process
    lifetime.
    """
    global _BWRAP_PREFLIGHT
    if _BWRAP_PREFLIGHT is not None:
        return _BWRAP_PREFLIGHT
    probe = list(argv[:-2]) + [argv[-2], '-c', 'import sys']  # keep flags, swap the script
    try:
        proc = subprocess.run(probe, capture_output=True, timeout=30)
    except Exception as exc:
        _BWRAP_PREFLIGHT = (False, f'could not run bwrap: {exc}')
        return _BWRAP_PREFLIGHT
    if proc.returncode == 0:
        _BWRAP_PREFLIGHT = (True, '')
        return _BWRAP_PREFLIGHT
    err = (proc.stderr or b'').decode('utf-8', 'replace').strip().splitlines()
    reason = err[-1] if err else f'bwrap exited with {proc.returncode}'
    _BWRAP_PREFLIGHT = (False, f'bwrap preflight failed: {reason}')
    return _BWRAP_PREFLIGHT


def _is_sigkill(rc) -> bool:
    """True if the process died from SIGKILL (bwrap reports it as 137)."""
    if rc is None:
        return False
    sig = -rc if rc < 0 else (rc - 128 if rc > 128 else None)
    return sig == int(signal.SIGKILL)


def _describe_exit(rc) -> str:
    """Human-readable process outcome (exit code / killing signal).

    bwrap (like a shell) reports a signalled child as 128+N, so SIGKILL shows up
    as 137 rather than -9. Handle both conventions.
    """
    if rc is None:
        return 'still running'
    sig = -rc if rc < 0 else (rc - 128 if rc > 128 else None)
    if sig is not None and sig > 0:
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = f'signal {sig}'
        return f'killed by {name} ({rc})'
    return f'exit code {rc}'


def _sandbox_argv():
    """Return (argv, why) for launching the sandboxed runner.

    `argv is None` means "don't sandbox": `why` is empty for the normal
    auto-fallback case (no bwrap installed → in-process), and non-empty when the
    operator explicitly required a sandbox that can't be provided.
    """
    if SCRIPT_SANDBOX in ('0', 'false', 'off', 'no'):
        return None, ''
    required = SCRIPT_SANDBOX in ('1', 'true', 'on', 'yes', 'require')
    bwrap = shutil.which(BWRAP)
    if not bwrap:
        if required:
            return None, (f'SCRIPT_SANDBOX={SCRIPT_SANDBOX} but {BWRAP!r} was not found on PATH')
        return None, ''

    root = Path(__file__).resolve().parent
    venv = root / '.venv'
    python = venv / 'bin' / 'python3'
    if not python.exists():
        if required:
            return None, f'SCRIPT_SANDBOX={SCRIPT_SANDBOX} but {python} is missing'
        return None, ''

    # Only the files the runner actually needs are bound read-only: the venv,
    # the runner, the framing helper and the Event dataclass. The rest of the
    # repo (logs/, generated/, sheets creds, etc.) is invisible.
    argv = [
        bwrap,
        # Fresh, empty environment: no API keys, no tokens, no PYTHON* knobs.
        '--clearenv',
        '--setenv', 'PATH', '/usr/bin:/bin',
        '--setenv', 'HOME', '/tmp',
        # Resource ceilings, read by sandbox_runner.py (bwrap has no --rlimit-*).
        '--setenv', 'SANDBOX_CPU_SECONDS', str(SANDBOX_CPU_SECONDS),
        '--setenv', 'SANDBOX_MAX_MEMORY_MB', str(SANDBOX_MAX_MEMORY_MB),
        '--setenv', 'SANDBOX_CLIENT', SANDBOX_CLIENT,
        '--die-with-parent',
        # Isolation: no net, no shared pid/ipc/uts namespaces, own session.
        '--unshare-net', '--unshare-pid', '--unshare-ipc', '--unshare-uts',
        '--unshare-cgroup-try', '--unshare-user-try', '--new-session',
        # Empty root first, then the read-only system view on top of it.
        # (Order matters: a later --tmpfs / would shadow the binds.)
        '--tmpfs', '/',
        '--ro-bind', '/usr', '/usr',
        '--ro-bind-try', '/lib', '/lib',
        '--ro-bind-try', '/lib64', '/lib64',
        '--ro-bind-try', '/bin', '/bin',
        '--ro-bind-try', '/sbin', '/sbin',
        '--proc', '/proc',
        '--dev', '/dev',
        '--tmpfs', '/tmp',
        # The whitelisted project files.
        '--ro-bind', str(venv), f'{SANDBOX_DIR}/.venv',
        '--ro-bind', str(root / 'sandbox_runner.py'), f'{SANDBOX_DIR}/sandbox_runner.py',
        '--ro-bind', str(root / 'sandbox_framing.py'), f'{SANDBOX_DIR}/sandbox_framing.py',
        '--ro-bind', str(root / 'event.py'), f'{SANDBOX_DIR}/event.py',
        '--chdir', SANDBOX_DIR,
        # NOTE: no resource limits here — bubblewrap has no --rlimit-* options.
        # CPU/memory/fd/process ceilings are applied *inside* the sandbox by
        # sandbox_runner.py via resource.setrlimit() (soft == hard, so the
        # untrusted script cannot raise them), and the host additionally enforces
        # a wall-clock timeout (SANDBOX_TIMEOUT) and kills the child.
        # The interpreter is invoked at its BOUND location (/app/.venv/...), not
        # the host path — the venv dir is mounted read-only at SANDBOX_DIR.
        f'{SANDBOX_DIR}/.venv/bin/python3', f'{SANDBOX_DIR}/sandbox_runner.py',
    ]

    usable, reason = _bwrap_preflight(argv)
    if not usable:
        if required:
            return None, reason
        # auto mode: fall back to in-process, but say so loudly — a silent
        # downgrade would leave scripts running unsandboxed without anyone
        # noticing.
        print(f'[sandbox] {reason} — running scripts in-process (unsandboxed). '
              'Set SCRIPT_SANDBOX=1 to make this fatal.')
        return None, ''
    return argv, ''


def _event_payload(event: Event) -> dict:
    """JSON-safe copy of the Event for the sandbox (datetimes become ISO)."""
    data = asdict(event)
    for key in ('start_date', 'end_date'):
        value = data.get(key)
        if hasattr(value, 'isoformat'):
            data[key] = value.isoformat()
    return data


async def _run_sandboxed(page, script: Script, event: Event, argv) -> tuple[bool, str]:
    """Run the script inside bwrap, serving it the driver tools over stdio."""
    # Imported lazily: sandbox_host imports Driver from this module.
    from sandbox_host import SandboxHost

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        return False, f'could not start the sandbox: {exc}'

    host = SandboxHost(page, _event_payload(event), script.code)
    stderr_lines: list[str] = []

    async def drain_stderr():
        # Must be drained concurrently: a full stderr pipe would block the child.
        while True:
            try:
                line = await proc.stderr.readline()
            except Exception:
                break
            if not line:
                break
            text = line.decode('utf-8', 'replace').rstrip()
            if text:
                print(f'[sandbox] {text}')
                stderr_lines.append(text)
                del stderr_lines[:-40]  # keep a bounded tail

    drain = asyncio.create_task(drain_stderr())
    timed_out = False
    transport_error = ''
    killed_by_us = False
    try:
        await asyncio.wait_for(host.serve_child(proc), timeout=SANDBOX_TIMEOUT)
    except asyncio.TimeoutError:
        timed_out = True
    except Exception as exc:
        transport_error = f'{type(exc).__name__}: {exc}'
    finally:
        with contextlib.suppress(Exception):
            if proc.returncode is None:
                # The bridge finished while the child was still alive: we are
                # about to SIGKILL it, so the exit status below (137) is OUR
                # doing — do not report it as the sandbox dying on its own.
                killed_by_us = True
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 10)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(drain, 5)

    tail = '\n'.join(stderr_lines[-12:])
    # Anything that went wrong before the client fetched its job is an infra
    # failure (bwrap/env), not a bug in the generated script — the code never
    # ran. The marker MUST be at the very start of the message: callers test
    # `msg.startswith(SANDBOX_ERROR_PREFIX)` and otherwise hand the failure to
    # the LLM as a script bug (burning rewrite attempts on a broken sandbox).
    infra = not host.started
    marker = SANDBOX_ERROR_PREFIX + ' ' if infra else ''
    exit_desc = _describe_exit(proc.returncode)

    if timed_out:
        body = f'sandboxed script timed out after {SANDBOX_TIMEOUT:g}s'
    elif transport_error:
        body = f'sandbox bridge error: {transport_error}'
    else:
        result = host.result
        if result is not None and result.get('ok'):
            return True, 'executed'
        if result is not None:
            return False, (str(result.get('error') or 'script failed')
                           + (f'\n{tail}' if tail else ''))
        if killed_by_us:
            body = ('the sandbox bridge stopped before the script finished '
                    '(the runner was still alive and had to be killed)')
        else:
            body = (f'the sandboxed runner exited without reporting a result '
                    f'({exit_desc})')
        if host.bridge_error:
            body += f'; bridge error: {host.bridge_error}'
        if infra:
            if _is_sigkill(proc.returncode) and not killed_by_us:
                body += ('; SIGKILL with no traceback usually means the kernel killed '
                         'the sandbox (out of memory). Try SANDBOX_CLIENT=mcp, lower '
                         'SANDBOX_MAX_MEMORY_MB, or SCRIPT_SANDBOX=0 to compare with '
                         'the in-process path')
            body += ('; check `bwrap`, SCRIPT_SANDBOX and SANDBOX_TIMEOUT, or run '
                     '`python3 tools/sandbox_smoke.py` on this machine')

    if infra:
        # Surface the command + whatever the sandbox said, so the failure is
        # diagnosable without re-running with extra logging.
        print(f'[sandbox] run failed ({exit_desc}) — command was:')
        print('  ' + ' '.join(argv))
        if tail:
            print(f'[sandbox] stderr tail:\n{tail}')
    return False, marker + body + (f'\n{tail}' if tail else '')
