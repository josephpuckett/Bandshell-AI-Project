import asyncio
import difflib
import os
from fastmcp import FastMCP
from playwright.async_api import async_playwright
from playwright.async_api import Page, Playwright, Browser
from fastmcp.utilities.types import Image
from fastmcp.server.dependencies import get_context

# Initialize FastMCP - This makes creating tools very easy
mcp = FastMCP("BrowserAgent")

# Live pages exposed to the embedded browser view, keyed by website_id (str).
# A page is registered while it is the page the user should see/ed:
#  - after a scripted run (the review page, kept open for the human),
#  - during onboarding (the agent's own session page is resolved as a fallback,
#    see `resolve_live_page` in main.py; it is not registered here explicitly).
LIVE_PAGES: dict[str, Page] = {}


def set_live_page(website_id, page: Page):
    LIVE_PAGES[str(website_id)] = page


def get_live_page(website_id):
    return LIVE_PAGES.get(str(website_id))


def clear_live_page(website_id):
    LIVE_PAGES.pop(str(website_id), None)


def clear_all_live_pages():
    LIVE_PAGES.clear()


class Manager:
    def __init__(self):
        self.latest_page: Page | None = None

    async def start(self):
        self.manager: Playwright = await async_playwright().start()
        # Browser engine: chromium (default — enables the embedded view's CDP
        # screencast streaming, far more efficient than per-frame screenshots)
        # or firefox (no CDP → the view falls back to page.screenshot()).
        # BROWSER=chromium|firefox overrides.
        engine = os.getenv("BROWSER", "chromium").strip().lower()
        # Visible window by default (the human watches); HEADLESS=1 for servers.
        headless = os.getenv("HEADLESS") == "1"
        if engine == "firefox":
            self.browser: Browser = await self.manager.firefox.launch(headless=headless)
        else:
            self.browser: Browser = await self.manager.chromium.launch(headless=headless)
        self.sessions: dict[str, Session] = {}
    async def get_session(self):
        id = get_context().session_id
        if id in self.sessions:
            session = self.sessions[id]
        else:
            session = Session()
            await session.start(self.browser)
            self.sessions[id] = session
        self.latest_page = session.page
        return session
    async def get_page(self):
        return (await self.get_session()).page
    async def close_session(self, session_id: str | None = None):
        """Close one browser session by id, or every open session if none given.

        Closes the context (and its tab/window). Used to tear down an agent's
        browser when its onboarding is cancelled or finishes, so windows are not
        left open.
        """
        if session_id is not None:
            session = self.sessions.pop(session_id, None)
            items = [session] if session else []
        else:
            items = list(self.sessions.values())
            self.sessions.clear()
        if not self.sessions:
            self.latest_page = None
        for s in items:
            try:
                if s and s.context is not None:
                    await s.context.close()
            except Exception:
                pass

class Session:
    def __init__(self):
        self.last_aria = ""  # previous aria snapshot, used to compute diffs
        self.context = None
        self.page = None

    async def start(self, browser: Browser):
        self.context = await browser.new_context()
        self.page = await self.context.new_page()

manager = Manager()

@mcp.tool()
async def navigate_to(url: str) -> str:
    """Navigates the browser to a specific URL."""
    p = await manager.get_page()
    await p.goto(url)
    return f"Successfully navigated to {url}"

@mcp.tool()
async def fill_nth_of_role(role: str, n: int, value: str) -> str:
    """Fill/select/check the nth element of a given role. Auto-detects the element type. Uses 1-based indexing."""
    p = await manager.get_page()
    locator = p.get_by_role(role).nth(n - 1)
    return await _fill_locator(locator, value)


async def _fill_locator(locator, value: str) -> str:
    """Fill/select/check a single locator, auto-detecting element type."""
    el = await locator.element_handle()
    tag = (await el.get_attribute('tagName') or '').lower()
    type_attr = (await el.get_attribute('type') or '').lower()

    if tag == 'select':
        await locator.select_option(value)
        return f"Selected {value!r} in select"
    if type_attr == 'checkbox' or type_attr == 'radio':
        # Interpret the value as boolean-ish: set checked/unchecked.
        should_check = str(value).strip().lower() in ('true', '1', 'yes', 'on', 'checked')
        if bool(await locator.is_checked()) != should_check:
            await locator.check() if should_check else await locator.uncheck()
        return f"Set checkbox/radio checked={should_check}"
    await locator.fill(value)
    return f"Filled {value!r}"


async def _click_locator(locator) -> str:
    """Click a single locator, retrying once after dismissing a possible overlay."""
    try:
        await locator.click(timeout=6000)
        return "Clicked"
    except Exception:
        # A modal / autosuggest overlay is likely intercepting the pointer.
        # Try dismissing it with Escape, then retry once before giving up.
        await locator.page.wait_for_timeout(250)
        try:
            await locator.page.keyboard.press('Escape')
        except Exception:
            pass
        await locator.page.wait_for_timeout(350)
        await locator.click(timeout=15000)
        return "Clicked (after dismissing overlay)"


@mcp.tool()
async def click_nth_of_role(role: str, n: int) -> str:
    """Click nth-of-role. Uses 1-based indexing."""
    p = await manager.get_page()
    locator = p.get_by_role(role).nth(n - 1)
    return await _click_locator(locator)


@mcp.tool()
async def fill_by_role(role: str, name: str, value: str) -> str:
    """Fill/select/check the element of a role whose accessible name matches `name`
    (case-insensitive substring, e.g. role='textbox' name='Event Title').

    PREFER this over fill_nth_of_role: names come straight from the aria snapshot
    and stay stable when the page re-renders, so the agent doesn't have to count.
    """
    p = await manager.get_page()
    locator = p.get_by_role(role, name=name)
    return await _fill_locator(locator, value)


@mcp.tool()
async def click_by_role(role: str, name: str) -> str:
    """Click the element of a role whose accessible name matches `name`
    (e.g. role='button' name='Submit'). Prefer over click_nth_of_role."""
    p = await manager.get_page()
    locator = p.get_by_role(role, name=name)
    return await _click_locator(locator)


@mcp.tool()
async def fill_by_label(label: str, value: str) -> str:
    """Fill the field associated with a visible label, e.g. fill_by_label('Event Title', ...)."""
    p = await manager.get_page()
    locator = p.get_by_label(label)
    return await _fill_locator(locator, value)


@mcp.tool()
async def fill_by_placeholder(placeholder: str, value: str) -> str:
    """Fill the field whose placeholder matches, e.g. fill_by_placeholder('Search events', ...)."""
    p = await manager.get_page()
    locator = p.get_by_placeholder(placeholder)
    return await _fill_locator(locator, value)


@mcp.tool()
async def click_by_text(text: str) -> str:
    """Click the first element containing the given text (menu items, dropdown options, links)."""
    p = await manager.get_page()
    locator = p.get_by_text(text).first
    return await _click_locator(locator)


@mcp.tool()
async def press_key(key: str) -> str:
    """Press a keyboard key: Tab, Enter, Escape, ArrowDown/ArrowUp/ArrowLeft/ArrowRight, Backspace.
    Useful for comboboxes/autosuggest (type + ArrowDown + Enter), closing overlays (Escape),
    and moving focus between fields (Tab)."""
    p = await manager.get_page()
    await p.keyboard.press(key)
    return f"Pressed {key}"


@mcp.tool()
async def wait(ms: int = 500) -> str:
    """Pause so animations/autosuggest options render before the next snapshot."""
    p = await manager.get_page()
    await p.wait_for_timeout(ms)
    return f"Waited {ms}ms"

@mcp.tool()
async def take_screenshot(full: bool = False) -> Image:
    """Takes a screenshot and returns the image (for vision-enabled models).

    Defaults to a JPEG of the current viewport (small, cheap for the model).
    Pass full=True to capture the entire page. Use when the aria snapshot is
    ambiguous or you need to SEE the layout (colors, spacing, custom widgets,
    whether a combobox opened). Prefer aria_snapshot for reading values.
    """
    p = await manager.get_page()
    if full:
        data = await p.screenshot(full_page=True, type='jpeg', quality=60)
    else:
        data = await p.screenshot(type='jpeg', quality=70)
    return Image(data=data, format="jpeg")

@mcp.tool()
async def aria_snapshot(full: bool = True) -> str:
    """Takes an aria snapshot of the page. Use to figure out which elements to interact with.

    If `full=True` (default) the entire page snapshot is returned. If `full=False` only
    a diff of the lines that changed since the last aria_snapshot call is returned, which
    is much cheaper to read for small changed regions (e.g. after opening a dropdown).
    """
    session = await manager.get_session()
    data = await session.page.aria_snapshot()
    if not full:
        prev = session.last_aria
        session.last_aria = data
        if prev:
            diff = difflib.unified_diff(
                prev.splitlines(),
                data.splitlines(),
                fromfile='before', tofile='after', lineterm='',
            )
            diff_text = '\n'.join(line for line in diff if line[:1] in '+-')
            print(diff_text)
            if diff_text:
                return ('[aria diff] lines prefixed + were added, - were removed; '
                        'unchanged lines omitted.\n' + diff_text)
            return '[aria diff] no changes since last snapshot.'
        # No previous baseline, so fall back to returning the full page.
        print(data)
        return '[aria full (no earlier baseline to diff against)]\n' + data
    session.last_aria = data
    print(data)
    return data

async def main():
    await manager.start()
    await mcp.run_async(transport="sse")
if __name__ == "__main__":
    asyncio.run(main())
