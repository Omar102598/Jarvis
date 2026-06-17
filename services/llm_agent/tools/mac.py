"""macOS laptop control tools for JARVIS.

Calls the mac_bridge service running natively on the host
(reachable at host.docker.internal:MAC_BRIDGE_PORT).

Available tools
---------------
mac_screenshot      — capture the screen and describe it with vision AI
mac_clipboard       — read or write the clipboard
mac_system_info     — CPU, memory, battery, disk usage
mac_shell           — run an allowlisted shell command on the Mac
mac_applescript     — run arbitrary AppleScript
mac_open            — open an app, file, or URL
mac_spotlight       — search for files with Spotlight
mac_type            — type text into the focused app
mac_notify          — send a macOS native notification
"""

import asyncio
import base64
import os
from typing import Literal

import aiohttp
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

_HOST = os.environ.get("MAC_BRIDGE_HOST", "host.docker.internal")
_PORT = int(os.environ.get("MAC_BRIDGE_PORT", "7777"))
_BASE = f"http://{_HOST}:{_PORT}"
_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def _get(path: str, **params) -> dict:
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        async with s.get(f"{_BASE}{path}", params=params) as r:
            r.raise_for_status()
            return await r.json()


async def _post(path: str, **body) -> dict:
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        async with s.post(f"{_BASE}{path}", json=body) as r:
            r.raise_for_status()
            return await r.json()


def _bridge_unavailable(e: Exception) -> str:
    return (
        f"macOS bridge unavailable ({e}). "
        "Make sure scripts/run_mac_bridge.sh is running on your Mac."
    )


@tool
async def mac_screenshot(question: str = "Describe what you see on the screen.") -> str:
    """Capture the Mac screen and answer a question about it using vision AI.

    Args:
        question: What to look for or describe, e.g. 'What windows are open?'
    """
    try:
        data = await _get("/screenshot")
    except Exception as e:
        return _bridge_unavailable(e)

    b64 = data.get("image_base64", "")
    if not b64:
        return "Screenshot captured but no image data returned."

    # Use the LLM's vision capability to analyse the screenshot
    from llm_factory import build_llm
    llm = build_llm(temperature=0)
    response = llm.invoke([
        SystemMessage(content="You are analysing a screenshot for the user."),
        HumanMessage(content=[
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ])
    return response.content


@tool
async def mac_clipboard(action: Literal["read", "write"] = "read", text: str = "") -> str:
    """Read from or write to the Mac clipboard.

    Args:
        action: "read" to get the current clipboard text, "write" to replace it.
        text: Text to write (only used when action="write").
    """
    try:
        if action == "read":
            data = await _get("/clipboard")
            return data.get("text", "")
        else:
            await _post("/clipboard", text=text)
            return f"Clipboard updated with: {text[:100]}{'...' if len(text) > 100 else ''}"
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_system_info() -> str:
    """Get the Mac's current CPU load, memory, battery, and disk usage."""
    try:
        d = await _get("/system")
        mem = d.get("memory", {})
        bat = d.get("battery") or {}
        disk = d.get("disk", {})
        parts = [
            f"CPU: {d.get('cpu_percent', '?')}%",
            f"Memory: {mem.get('used_gb', '?')} / {mem.get('total_gb', '?')} GB ({mem.get('percent', '?')}%)",
            f"Disk: {disk.get('free_gb', '?')} GB free of {disk.get('total_gb', '?')} GB",
        ]
        if bat:
            charging = "charging" if bat.get("charging") else "on battery"
            parts.append(f"Battery: {bat.get('percent', '?')}% ({charging})")
        if d.get("platform"):
            parts.append(f"macOS {d['platform']}")
        return "\n".join(parts)
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_shell(command: str) -> str:
    """Run a shell command directly on your Mac (allowlisted commands only).

    Use this for Mac-native operations: check processes, query system state,
    run scripts, etc. The allowlist is set via MAC_SHELL_ALLOWLIST in .env.

    Args:
        command: Shell command to run, e.g. 'df -h' or 'ps aux | grep python'
    """
    try:
        data = await _post("/shell", cmd=command, timeout=20)
        return data.get("output", "").strip() or "(no output)"
    except aiohttp.ClientResponseError as e:
        if e.status == 403:
            return f"Command not allowed: {e.message}"
        return f"Shell error: {e}"
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_applescript(script: str) -> str:
    """Run an AppleScript on the Mac and return the result.

    Use this to automate macOS apps, interact with Finder, control music,
    read/write app data, and anything else AppleScript can do.

    Args:
        script: AppleScript code, e.g. 'tell application "Finder" to get name of desktop'
    """
    try:
        data = await _post("/applescript", script=script)
        if data.get("error"):
            return f"AppleScript error: {data['error']}"
        return data.get("result", "(no result)")
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_open(what: str) -> str:
    """Open a native macOS application or a local file. NOT for web automation.

    Use this ONLY to launch apps (e.g. 'Spotify', 'Finder', 'Terminal') or open
    local files. To visit a website and interact with it, use mac_browser_navigate
    instead — it opens the page inside Playwright where you can click and fill forms.

    Args:
        what: App name (e.g. 'Spotify') or file path ('/Users/omar/doc.pdf').
    """
    try:
        await _post("/open", what=what)
        return f"Opened: {what}"
    except aiohttp.ClientResponseError as e:
        return f"Could not open '{what}': {e.message}"
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_spotlight(query: str, limit: int = 10) -> str:
    """Search for files on the Mac using Spotlight.

    Args:
        query: Search term, e.g. 'budget 2025', 'kind:image vacation'
        limit: Maximum number of results to return (default 10)
    """
    try:
        data = await _get("/spotlight", q=query, limit=limit)
        results = data.get("results", [])
        if not results:
            return f"No files found for: {query}"
        return "\n".join(results)
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_type(text: str, app: str = "") -> str:
    """Type text into the currently focused macOS application.

    Args:
        text: Text to type.
        app: Optional app to bring to front first (e.g. 'Notes', 'Terminal').
    """
    try:
        body = {"text": text}
        if app:
            body["app"] = app
        await _post("/type", **body)
        return f"Typed into {'`' + app + '`' if app else 'focused app'}: {text[:80]}"
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_notify(title: str = "JARVIS", body: str = "") -> str:
    """Send a native macOS notification (appears in Notification Center).

    Args:
        title: Notification title (default "JARVIS")
        body: Notification message
    """
    try:
        await _post("/notify", title=title, body=body)
        return f"Notification sent: {title} — {body}"
    except Exception as e:
        return _bridge_unavailable(e)


# ---------------------------------------------------------------------------
# Chrome control via AppleScript (uses user's existing signed-in Chrome)
# ---------------------------------------------------------------------------

async def _chrome_osa(script: str) -> str:
    """Route a Chrome AppleScript through mac_bridge (osascript only works on the host)."""
    data = await _post("/applescript", script=script)
    if data.get("error"):
        return data["error"]
    return data.get("result", "")


@tool
async def mac_chrome_navigate(url: str) -> str:
    """Open a URL in the user's existing Chrome browser (keeps your login session).

    Use this instead of mac_browser_navigate when you need to interact with a
    site the user is already signed into (Amazon Fresh, Instacart, banking, etc.).
    After navigating, use mac_chrome_js or mac_chrome_read to interact.

    Args:
        url: Full URL, e.g. 'https://www.amazon.com/alm/storefront'
    """
    try:
        script = f"""
tell application "Google Chrome"
    activate
    if (count of windows) = 0 then
        make new window
    else
        tell front window to make new tab
    end if
    set URL of active tab of front window to "{url}"
end tell
        """
        await _chrome_osa(script)
        await asyncio.sleep(2)  # give page a moment to load
        return f"Chrome opened new tab and navigated to: {url}"
    except Exception as e:
        return f"Chrome navigate failed: {e}"


@tool
async def mac_chrome_read() -> str:
    """Read the visible text content of the current Chrome tab.

    Use after mac_chrome_navigate to understand what's on the page before
    deciding what to click or fill.
    """
    try:
        script = """
tell application "Google Chrome"
    set pageText to execute front window's active tab javascript "document.body.innerText"
    return pageText
end tell
        """
        text = await _chrome_osa(script)
        url_script = 'tell application "Google Chrome" to get URL of active tab of front window'
        url = await _chrome_osa(url_script)
        return f"[{url}]\n\n{text[:4000]}"
    except Exception as e:
        return f"Chrome read failed: {e}"


@tool
async def mac_chrome_js(script: str) -> str:
    """Execute JavaScript in the current Chrome tab and return the result.

    Use for precise interactions: clicking buttons, filling forms, reading
    specific elements. The JS runs in the page context with full DOM access.

    Examples:
      - Click: "document.querySelector('.add-to-cart button').click()"
      - Fill:  "document.querySelector('#search').value = 'milk'; document.querySelector('#search').dispatchEvent(new Event('input'))"
      - Read:  "document.querySelector('.price').innerText"

    Args:
        script: JavaScript to execute. Must return a value if you want output.
    """
    try:
        safe_script = script.replace('"', '\\"').replace('\n', ' ')
        osa = f"""
tell application "Google Chrome"
    execute front window's active tab javascript "{safe_script}"
end tell
        """
        result = await _chrome_osa(osa)
        return result or "(script executed, no return value)"
    except Exception as e:
        return f"Chrome JS failed: {e}"


# ---------------------------------------------------------------------------
# Browser automation (Playwright via mac_bridge)
# ---------------------------------------------------------------------------

@tool
async def mac_browser_navigate(url: str) -> str:
    """Navigate to a website for web automation — ordering, forms, searching.

    Use this (NOT mac_open) whenever the goal involves interacting with a website:
    grocery ordering, web search, filling forms, reading content. Opens a visible
    Chromium window the user can watch; follow up with mac_browser_read,
    mac_browser_click, mac_browser_fill to interact with the page.

    Args:
        url: Full URL, e.g. 'https://www.amazon.com/alm/storefront' for Amazon Fresh
    """
    try:
        data = await _post("/browser/navigate", url=url)
        return data.get("status", f"Navigated to {url}")
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_browser_read() -> str:
    """Read the visible text content of the current browser page.

    Use after mac_browser_navigate to understand what's on the page before
    deciding which element to click or fill.
    """
    try:
        data = await _get("/browser/read")
        text = data.get("text", "")
        url  = data.get("url", "")
        return f"[{url}]\n\n{text[:4000]}"
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_browser_click(text: str) -> str:
    """Click an element on the current page that contains the given text.

    Matches buttons, links, and labels by visible text (case-insensitive).
    If multiple matches exist, clicks the first one.

    Args:
        text: Visible text of the element to click, e.g. 'Add to cart' or 'Sign in'
    """
    try:
        data = await _post("/browser/click", text=text)
        return data.get("status", f"Clicked: {text}")
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_browser_fill(label: str, value: str) -> str:
    """Fill a form field on the current page.

    Finds an input by its placeholder, label, or aria-label and types the value.

    Args:
        label: The field label or placeholder text, e.g. 'Search', 'Email', 'Password'
        value: The value to type into the field
    """
    try:
        data = await _post("/browser/fill", label=label, value=value)
        return data.get("status", f"Filled '{label}' with: {value}")
    except Exception as e:
        return _bridge_unavailable(e)


@tool
async def mac_browser_screenshot(question: str = "Describe what's on the page.") -> str:
    """Take a screenshot of the current browser page and describe it with vision AI.

    Use when you need to visually confirm the state of the page, e.g. checking
    if a cart was updated or a search returned results.

    Args:
        question: What to look for, e.g. 'Is the item in the cart?'
    """
    try:
        data = await _get("/browser/screenshot")
    except Exception as e:
        return _bridge_unavailable(e)

    b64 = data.get("image_base64", "")
    if not b64:
        return "Browser screenshot captured but no image returned."

    from llm_factory import build_llm
    llm = build_llm(temperature=0)
    response = llm.invoke([
        SystemMessage(content="You are analysing a browser screenshot to help the user."),
        HumanMessage(content=[
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ])
    return response.content
