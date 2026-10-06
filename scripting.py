"""Safe execution of generated browser scripts.

A generated script is an `async def run(event, api)` that drives a Playwright
page through a tight, allowlisted `api` interface (the `Driver`). Scripts are
gated by `validate_script()` before execution to defend against prompt/code
injection from malicious page content.

For now scripts run in-process. Later this may move behind a sandbox
(bubblewrap) via the same tight `api` interface, so only this module changes.
"""
import ast
from dataclasses import replace as dataclass_replace

from browser import manager, set_live_page
from event import Event
from eventbrite_api import direct_image_url
from website import Script


VIEWPORT = (1280, 850)  # desktop-size viewport for the embedded browser view


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

    If `live_key` is given (a website_id), the page is registered in
    browser.LIVE_PAGES as soon as it is created, so the embedded view streams
    the *actual* run as it happens instead of a stale fallback page.
    """
    # Scripts read event.image at run time; hand them a directly-fetchable URL
    # (Drive share links rewritten), matching what the agent saw during onboarding.
    event = dataclass_replace(event, image=direct_image_url(event.image))
    context = await manager.browser.new_context()
    try:
        page = await context.new_page()
        await page.set_viewport_size({'width': VIEWPORT[0], 'height': VIEWPORT[1]})
        if live_key is not None:
            set_live_page(live_key, page)
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
