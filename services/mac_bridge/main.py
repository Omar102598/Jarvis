"""JARVIS macOS Bridge.

A lightweight HTTP API that runs natively on the Mac and exposes laptop
capabilities to the llm_agent container via host.docker.internal:7777.

Endpoints
---------
GET  /health
GET  /screenshot?analyze=false        — capture screen, return PNG base64
GET  /clipboard                       — read clipboard text
POST /clipboard  {"text": "..."}      — write clipboard text
GET  /system                          — CPU, memory, battery, disk
POST /shell      {"cmd": "..."}       — run allowlisted shell command on Mac
POST /applescript {"script": "..."}  — run AppleScript, return result
POST /open       {"what": "..."}      — open app, file, or URL
GET  /spotlight  ?q=...&limit=10      — mdfind Spotlight search
POST /type       {"text": "..."}      — type text into focused app
POST /notify     {"title":"","body":""} — macOS native notification

Headless Playwright scraper (for grocery agent & price comparison):
POST /scraper/navigate   {"url": "...", "wait_until": "networkidle", "timeout_ms": 30000}
GET  /scraper/read                    — get visible text of current scraper page
POST /scraper/js         {"script": "..."} — run JS in scraper page
GET  /scraper/screenshot              — PNG screenshot of scraper page
POST /scraper/close                   — close scraper context
"""

import base64
import json
import os
import queue
import shlex
import subprocess
import tempfile
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

PORT = int(os.environ.get("MAC_BRIDGE_PORT", "7777"))

# Shell commands Jarvis is allowed to run on the Mac host.
# "*" means unrestricted (only use on a trusted machine).
_SHELL_ALLOW_ENV = os.environ.get("MAC_SHELL_ALLOWLIST", "")
_DEFAULT_ALLOW = {
    "echo", "ls", "cat", "pwd", "whoami", "date", "uptime", "df", "du",
    "ps", "uname", "hostname", "open", "say", "osascript", "afplay",
    "mdfind", "mdls", "pbcopy", "pbpaste", "system_profiler",
    "networksetup", "scutil", "defaults", "launchctl", "brew",
    "git", "python3", "node", "npm", "curl", "ping",
    # Search / text tools (needed for self-modification and dev work)
    "find", "grep", "rg", "wc", "head", "tail", "sort", "uniq",
    "xargs", "sed", "awk", "cut", "tr", "diff", "which", "type",
    "touch", "mkdir", "cp", "mv", "rm",
    # Build tools
    "make", "cargo", "go", "rustc", "java", "mvn", "gradle",
    "pip", "pip3", "poetry", "yarn", "pnpm",
    "docker", "docker-compose",
}
if _SHELL_ALLOW_ENV == "*":
    SHELL_ALLOWLIST = None  # unrestricted
elif _SHELL_ALLOW_ENV:
    SHELL_ALLOWLIST = set(_SHELL_ALLOW_ENV.split(","))
else:
    SHELL_ALLOWLIST = _DEFAULT_ALLOW

# ---------------------------------------------------------------------------
# Visible browser singleton (Playwright — started lazily on first use)
# ---------------------------------------------------------------------------
_pw               = None
_browser          = None
_browser_context  = None
_page             = None

_BROWSER_STATE_PATH = os.path.expanduser("~/.jarvis/browser_state.json")

# ---------------------------------------------------------------------------
# Headless scraper singleton (separate context — for grocery/price scraping)
# ---------------------------------------------------------------------------
_scraper_pw      = None
_scraper_browser = None
_scraper_page    = None
_scraper_lock    = threading.Lock()


async def _get_page():
    """Return the current visible Playwright page, launching browser if needed.

    Loads a saved storage state (cookies/localStorage) from disk if it exists,
    so the user stays logged in to stores between grocery agent runs.
    """
    global _pw, _browser, _browser_context, _page
    if _pw is None:
        from playwright.async_api import async_playwright
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(headless=False, args=["--start-maximized"])
    if _browser_context is None:
        state_file = _BROWSER_STATE_PATH if os.path.exists(_BROWSER_STATE_PATH) else None
        _browser_context = await _browser.new_context(
            storage_state=state_file,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        if state_file:
            print(f"[MacBridge] Browser session loaded from {state_file}")
    if _page is None or _page.is_closed():
        _page = await _browser_context.new_page()
    return _page


async def _get_scraper_page():
    """Return the headless scraper page, launching a separate browser if needed."""
    global _scraper_pw, _scraper_browser, _scraper_page
    if _scraper_pw is None:
        from playwright.async_api import async_playwright
        _scraper_pw = await async_playwright().start()
        _scraper_browser = await _scraper_pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1280,900",
            ],
        )
    if _scraper_page is None or _scraper_page.is_closed():
        context = await _scraper_browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        # Stealth: hide webdriver flag
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        _scraper_page = await context.new_page()
    return _scraper_page


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    global _pw, _browser, _browser_context, _page, _scraper_pw, _scraper_browser, _scraper_page
    for p in [_page, _scraper_page]:
        if p and not p.is_closed():
            await p.close()
    if _browser_context:
        await _browser_context.close()
    for b in [_browser, _scraper_browser]:
        if b:
            await b.close()
    for pw in [_pw, _scraper_pw]:
        if pw:
            await pw.stop()


app = FastAPI(title="JARVIS macOS Bridge", version="1.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list, timeout: int = 15, input_text: Optional[str] = None) -> str:
    """Run a subprocess and return stdout+stderr as text."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )
    return (result.stdout + result.stderr).strip()


def _osascript(script: str) -> str:
    return _run(["osascript", "-e", script])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ShellRequest(BaseModel):
    cmd: str
    timeout: int = 15

class ClipboardWrite(BaseModel):
    text: str

class AppleScriptRequest(BaseModel):
    script: str
    timeout: int = 15

class OpenRequest(BaseModel):
    what: str  # app name, file path, or URL

class TypeRequest(BaseModel):
    text: str
    app: Optional[str] = None  # bring this app to front first

class NotifyRequest(BaseModel):
    title: str = "JARVIS"
    body: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "service": "jarvis-mac-bridge"}


@app.get("/screenshot")
def screenshot(analyze: bool = False):
    """Capture the entire screen and return it as a base64-encoded PNG."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(["screencapture", "-x", tmp_path], check=True, timeout=10)
        with open(tmp_path, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode()
        return {"image_base64": b64, "format": "png", "size_bytes": len(data)}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.get("/clipboard")
def clipboard_read():
    """Read the current clipboard text."""
    text = _run(["pbpaste"])
    return {"text": text}


@app.post("/clipboard")
def clipboard_write(req: ClipboardWrite):
    """Write text to the clipboard."""
    subprocess.run(["pbcopy"], input=req.text.encode(), check=True, timeout=5)
    return {"ok": True, "text": req.text}


@app.get("/system")
def system_info():
    """Return CPU load, memory, battery, and disk usage."""
    import platform

    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        vm = psutil.virtual_memory()
        battery = psutil.sensors_battery()
        disk = psutil.disk_usage("/")
        return {
            "cpu_percent": cpu,
            "memory": {
                "total_gb": round(vm.total / 1e9, 1),
                "used_gb": round(vm.used / 1e9, 1),
                "percent": vm.percent,
            },
            "battery": {
                "percent": battery.percent if battery else None,
                "charging": battery.power_plugged if battery else None,
            } if battery else None,
            "disk": {
                "total_gb": round(disk.total / 1e9, 1),
                "used_gb": round(disk.used / 1e9, 1),
                "free_gb": round(disk.free / 1e9, 1),
                "percent": disk.percent,
            },
            "platform": platform.mac_ver()[0],
        }
    except ImportError:
        uptime = _run(["uptime"])
        df = _run(["df", "-h", "/"])
        return {"uptime": uptime, "disk": df, "note": "pip install psutil for richer stats"}


@app.post("/shell")
def shell(req: ShellRequest):
    """Run a shell command on the Mac host (allowlisted)."""
    parts = shlex.split(req.cmd)
    if not parts:
        raise HTTPException(400, "Empty command")

    if SHELL_ALLOWLIST is not None and parts[0] not in SHELL_ALLOWLIST:
        raise HTTPException(
            403,
            f"Command '{parts[0]}' is not in the allowlist. "
            f"Set MAC_SHELL_ALLOWLIST=* in .env to allow all commands."
        )

    try:
        output = _run(parts, timeout=req.timeout)
        return {"output": output, "command": req.cmd}
    except subprocess.TimeoutExpired:
        raise HTTPException(408, f"Command timed out after {req.timeout}s")
    except FileNotFoundError:
        raise HTTPException(404, f"Command not found: {parts[0]}")


@app.post("/applescript")
def applescript(req: AppleScriptRequest):
    """Run an AppleScript and return the result."""
    try:
        result = subprocess.run(
            ["osascript", "-e", req.script],
            capture_output=True,
            text=True,
            timeout=req.timeout,
        )
        return {
            "result": result.stdout.strip(),
            "error": result.stderr.strip() if result.returncode != 0 else None,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(408, f"AppleScript timed out after {req.timeout}s")


@app.post("/open")
def open_thing(req: OpenRequest):
    """Open an application, file, or URL with macOS 'open'."""
    what = req.what.strip()
    cmd = ["open", what] if "://" in what or what.startswith("/") else ["open", "-a", what]
    try:
        subprocess.run(cmd, check=True, timeout=10)
        return {"ok": True, "opened": what}
    except subprocess.CalledProcessError as e:
        raise HTTPException(400, f"Could not open '{what}': {e}")


@app.get("/spotlight")
def spotlight(q: str = Query(..., description="Search query"), limit: int = 10):
    """Search for files using macOS Spotlight (mdfind)."""
    try:
        result = subprocess.run(
            ["mdfind", "-onlyin", os.path.expanduser("~"), q],
            capture_output=True,
            text=True,
            timeout=10,
        )
        paths = [p for p in result.stdout.strip().splitlines() if p][:limit]
        return {"query": q, "results": paths, "count": len(paths)}
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Spotlight search timed out")


@app.post("/type")
def type_text(req: TypeRequest):
    """Type text into the currently focused application via AppleScript."""
    if req.app:
        _osascript(f'tell application "{req.app}" to activate')

    safe = req.text.replace('"', '\\"').replace("\\", "\\\\")
    _osascript(
        f'tell application "System Events" to keystroke "{safe}"'
    )
    return {"ok": True, "typed": req.text}


@app.post("/notify")
def notify(req: NotifyRequest):
    """Send a macOS native notification."""
    safe_body = req.body.replace('"', '\\"')
    safe_title = req.title.replace('"', '\\"')
    _osascript(
        f'display notification "{safe_body}" with title "{safe_title}"'
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Visible Browser automation (Playwright — headed, uses user's Chrome session)
# ---------------------------------------------------------------------------

class BrowserNavigateRequest(BaseModel):
    url: str

class BrowserClickRequest(BaseModel):
    text: str

class BrowserFillRequest(BaseModel):
    label: str
    value: str


@app.post("/browser/navigate")
async def browser_navigate(req: BrowserNavigateRequest):
    """Navigate the visible browser to a URL."""
    try:
        page = await _get_page()
        await page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
        title = await page.title()
        return {"status": f"Navigated to {req.url} — page title: '{title}'", "url": page.url}
    except Exception as e:
        raise HTTPException(500, f"Browser navigate failed: {e}")


@app.get("/browser/read")
async def browser_read():
    """Get the visible text content of the current page."""
    try:
        page = await _get_page()
        text = await page.evaluate("""() => {
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT,
                { acceptNode: n => {
                    const p = n.parentElement;
                    if (!p) return NodeFilter.FILTER_REJECT;
                    const s = window.getComputedStyle(p);
                    return (s.display === 'none' || s.visibility === 'hidden')
                        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
                }}
            );
            const parts = [];
            let n;
            while ((n = walker.nextNode())) {
                const t = n.textContent.trim();
                if (t) parts.push(t);
            }
            return parts.join('\\n');
        }""")
        return {"url": page.url, "text": text[:8000]}
    except Exception as e:
        raise HTTPException(500, f"Browser read failed: {e}")


@app.post("/browser/click")
async def browser_click(req: BrowserClickRequest):
    """Click the first element containing the given text."""
    try:
        page = await _get_page()
        locator = page.get_by_text(req.text, exact=False).first
        await locator.click(timeout=10000)
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
        return {"status": f"Clicked '{req.text}' — now at {page.url}"}
    except Exception as e:
        raise HTTPException(500, f"Browser click failed for '{req.text}': {e}")


@app.post("/browser/fill")
async def browser_fill(req: BrowserFillRequest):
    """Fill a form field by placeholder, label, or aria-label."""
    try:
        page = await _get_page()
        for loc in [
            page.get_by_placeholder(req.label, exact=False),
            page.get_by_label(req.label, exact=False),
            page.locator(f'[aria-label*="{req.label}" i]'),
        ]:
            try:
                await loc.first.fill(req.value, timeout=5000)
                return {"status": f"Filled '{req.label}' with '{req.value}'"}
            except Exception:
                continue
        raise HTTPException(404, f"Could not find field '{req.label}'")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Browser fill failed: {e}")


class BrowserJsRequest(BaseModel):
    script: str


@app.post("/browser/js")
async def browser_js(req: BrowserJsRequest):
    """Execute JavaScript in the visible browser page and return the result."""
    try:
        page = await _get_page()
        result = await page.evaluate(req.script)
        return {"result": result, "url": page.url}
    except Exception as e:
        raise HTTPException(500, f"Browser JS failed: {e}")


@app.get("/browser/screenshot")
async def browser_screenshot():
    """Take a screenshot of the current visible browser page."""
    try:
        page = await _get_page()
        data = await page.screenshot(type="png")
        b64 = base64.b64encode(data).decode()
        return {"image_base64": b64, "format": "png", "url": page.url}
    except Exception as e:
        raise HTTPException(500, f"Browser screenshot failed: {e}")


@app.get("/browser/url")
async def browser_url():
    """Return the current page URL."""
    try:
        page = await _get_page()
        return {"url": page.url, "title": await page.title()}
    except Exception as e:
        raise HTTPException(500, f"Browser URL failed: {e}")


@app.delete("/browser/close")
async def browser_close():
    """Close the visible browser session."""
    global _page
    if _page and not _page.is_closed():
        await _page.close()
        _page = None
    return {"status": "browser page closed"}


@app.post("/browser/save-state")
async def browser_save_state():
    """Save current browser cookies and localStorage to disk for session persistence.

    The grocery agent calls this after cart automation so the user stays
    logged in to stores (Amazon, Target, HEB) between weekly runs.
    """
    global _browser_context
    if _browser_context is None:
        raise HTTPException(400, "No browser context is active — open a page first.")
    try:
        os.makedirs(os.path.dirname(_BROWSER_STATE_PATH), exist_ok=True)
        await _browser_context.storage_state(path=_BROWSER_STATE_PATH)
        return {"status": "saved", "path": _BROWSER_STATE_PATH}
    except Exception as e:
        raise HTTPException(500, f"Save state failed: {e}")


@app.post("/browser/clear-state")
async def browser_clear_state():
    """Delete saved browser session state (forces re-login on next browser launch)."""
    global _browser_context, _page
    if os.path.exists(_BROWSER_STATE_PATH):
        os.unlink(_BROWSER_STATE_PATH)
    # Close existing context so next launch starts fresh
    if _browser_context:
        if _page and not _page.is_closed():
            await _page.close()
            _page = None
        await _browser_context.close()
        _browser_context = None
    return {"status": "cleared", "path": _BROWSER_STATE_PATH}


# ---------------------------------------------------------------------------
# Headless Scraper (Playwright — dedicated context for price scraping)
# ---------------------------------------------------------------------------

class ScraperNavigateRequest(BaseModel):
    url: str
    wait_until: str = "networkidle"   # domcontentloaded | load | networkidle
    timeout_ms: int = 30000

class ScraperJsRequest(BaseModel):
    script: str
    timeout_ms: int = 10000


@app.post("/scraper/navigate")
async def scraper_navigate(req: ScraperNavigateRequest):
    """Navigate the headless scraper to a URL and wait for it to settle."""
    try:
        page = await _get_scraper_page()
        await page.goto(
            req.url,
            wait_until=req.wait_until,
            timeout=req.timeout_ms,
        )
        # Extra scroll to trigger lazy-loaded content
        await page.evaluate("window.scrollBy(0, 600)")
        await page.wait_for_timeout(1500)
        title = await page.title()
        return {
            "status": "ok",
            "url": page.url,
            "title": title,
        }
    except Exception as e:
        raise HTTPException(500, f"Scraper navigate failed: {e}")


@app.get("/scraper/read")
async def scraper_read():
    """Get the full visible text of the current scraper page."""
    try:
        page = await _get_scraper_page()
        text = await page.evaluate("""() => {
            const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT,
                { acceptNode: n => {
                    const p = n.parentElement;
                    if (!p) return NodeFilter.FILTER_REJECT;
                    const s = window.getComputedStyle(p);
                    return (s.display === 'none' || s.visibility === 'hidden')
                        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
                }}
            );
            const parts = [];
            let n;
            while ((n = walker.nextNode())) {
                const t = n.textContent.trim();
                if (t) parts.push(t);
            }
            return parts.join('\\n');
        }""")
        return {"url": page.url, "text": text[:12000]}
    except Exception as e:
        raise HTTPException(500, f"Scraper read failed: {e}")


@app.post("/scraper/js")
async def scraper_js(req: ScraperJsRequest):
    """Execute JavaScript in the headless scraper page and return the result."""
    try:
        page = await _get_scraper_page()
        result = await page.evaluate(req.script)
        return {"result": result, "url": page.url}
    except Exception as e:
        raise HTTPException(500, f"Scraper JS failed: {e}")


@app.get("/scraper/screenshot")
async def scraper_screenshot():
    """Take a PNG screenshot of the current headless scraper page."""
    try:
        page = await _get_scraper_page()
        data = await page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(data).decode()
        return {"image_base64": b64, "format": "png", "url": page.url}
    except Exception as e:
        raise HTTPException(500, f"Scraper screenshot failed: {e}")


@app.post("/scraper/close")
async def scraper_close():
    """Close the headless scraper page (browser stays alive for reuse)."""
    global _scraper_page
    if _scraper_page and not _scraper_page.is_closed():
        await _scraper_page.close()
        _scraper_page = None
    return {"status": "scraper page closed"}


# ---------------------------------------------------------------------------
# JARVIS self-modification endpoints
# ---------------------------------------------------------------------------

_JARVIS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_DOCKER_SERVICES = {
    "llm_agent", "dashboard", "agent_runner", "mobile_gateway",
    "mosquitto", "redis",
}
_NATIVE_SERVICES = {
    "stt", "tts_mac", "wake_word", "speaker_verify", "mac_bridge",
}


def _safe_path(rel: str) -> str:
    """Resolve a repo-relative path and verify it stays inside the repo."""
    full = os.path.abspath(os.path.join(_JARVIS_ROOT, rel.lstrip("/")))
    if not full.startswith(_JARVIS_ROOT):
        raise HTTPException(403, f"Path escapes repo root: {rel}")
    return full


class JarvisWriteRequest(BaseModel):
    path: str
    content: str

class JarvisRebuildRequest(BaseModel):
    service: str


@app.get("/jarvis/read")
def jarvis_read(path: str = Query(...)):
    full = _safe_path(path)
    if not os.path.isfile(full):
        raise HTTPException(404, f"File not found: {path}")
    try:
        with open(full, encoding="utf-8") as f:
            return {"path": path, "content": f.read()}
    except Exception as e:
        raise HTTPException(500, f"Read failed: {e}")


@app.get("/jarvis/list")
def jarvis_list(path: str = Query("services/llm_agent/tools")):
    full = _safe_path(path)
    if not os.path.isdir(full):
        raise HTTPException(404, f"Directory not found: {path}")
    files = sorted(os.listdir(full))
    return {"path": path, "files": [os.path.join(path, f) for f in files]}


@app.post("/jarvis/write")
def jarvis_write(req: JarvisWriteRequest):
    full = _safe_path(req.path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if os.path.exists(full):
        import shutil
        shutil.copy2(full, full + ".bak")
    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"status": f"Written {req.path} ({len(req.content)} bytes)"}
    except Exception as e:
        raise HTTPException(500, f"Write failed: {e}")


@app.get("/jarvis/root")
def jarvis_root():
    return {"root": _JARVIS_ROOT}


class JarvisFindRequest(BaseModel):
    pattern: str
    directory: str = ""

class JarvisGrepRequest(BaseModel):
    pattern: str
    directory: str = ""
    file_pattern: str = ""


@app.post("/jarvis/find")
def jarvis_find(req: JarvisFindRequest):
    search_root = _JARVIS_ROOT
    if req.directory:
        search_root = os.path.join(_JARVIS_ROOT, req.directory.lstrip("/"))
    if not os.path.isdir(search_root):
        raise HTTPException(404, f"Directory not found: {req.directory}")
    try:
        result = subprocess.run(
            ["find", search_root, "-name", req.pattern, "-not", "-path", "*/__pycache__/*",
             "-not", "-path", "*/.git/*", "-not", "-path", "*/node_modules/*"],
            capture_output=True, text=True, timeout=10,
        )
        paths = result.stdout.strip().splitlines()
        rel_paths = [p.replace(_JARVIS_ROOT + "/", "") for p in paths if p]
        return {"matches": rel_paths, "total": len(rel_paths), "repo_root": _JARVIS_ROOT}
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Find timed out")


@app.post("/jarvis/grep")
def jarvis_grep(req: JarvisGrepRequest):
    search_root = _JARVIS_ROOT
    if req.directory:
        search_root = os.path.join(_JARVIS_ROOT, req.directory.lstrip("/"))
    cmd = ["grep", "-rn",
           "--include=*.py", "--include=*.yml", "--include=*.yaml",
           "--include=*.txt", "--include=*.md", "--include=*.sh",
           "--exclude-dir=__pycache__", "--exclude-dir=.git",
           req.pattern, search_root]
    if req.file_pattern:
        cmd = ["grep", "-rn", f"--include={req.file_pattern}",
               "--exclude-dir=__pycache__", "--exclude-dir=.git",
               req.pattern, search_root]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        lines = result.stdout.strip().splitlines()
        rel = [line.replace(_JARVIS_ROOT + "/", "") for line in lines]
        return {"matches": rel[:200], "total": len(lines), "repo_root": _JARVIS_ROOT}
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Grep timed out")


@app.post("/jarvis/rebuild")
def jarvis_rebuild(req: JarvisRebuildRequest):
    svc = req.service.strip()
    if svc in _DOCKER_SERVICES:
        cmd = (
            f"cd {_JARVIS_ROOT} && "
            f"docker compose build {svc} && "
            f"docker compose up -d {svc}"
        )
        try:
            out = _run(["bash", "-c", cmd], timeout=180)
            return {"status": f"Docker service '{svc}' rebuilt and restarted.", "output": out[-500:]}
        except Exception as e:
            raise HTTPException(500, f"Rebuild failed: {e}")

    if svc in _NATIVE_SERVICES:
        pids_file = os.path.join(_JARVIS_ROOT, ".audio_pids")
        if svc == "mac_bridge":
            cmd = f"cd {_JARVIS_ROOT} && bash scripts/run_mac_bridge.sh stop; bash scripts/run_mac_bridge.sh start"
        else:
            cmd = (
                f"svc={svc}; "
                f"pid=$(grep \"^$svc:\" {pids_file} | cut -d: -f2); "
                f"[ -n \"$pid\" ] && kill -9 $pid 2>/dev/null; "
                f"echo 'killed old $svc'; "
                f"source {_JARVIS_ROOT}/.venv-audio/bin/activate && "
                f"python -u {_JARVIS_ROOT}/services/{svc}/main.py "
                f">> {_JARVIS_ROOT}/logs/audio/{svc}.log 2>&1 & "
                f"echo $! > /tmp/{svc}_new_pid"
            )
        try:
            out = _run(["bash", "-c", cmd], timeout=30)
            return {"status": f"Native service '{svc}' restarted.", "output": out}
        except Exception as e:
            raise HTTPException(500, f"Restart failed: {e}")

    raise HTTPException(400, f"Unknown service '{svc}'. Docker: {_DOCKER_SERVICES}. Native: {_NATIVE_SERVICES}.")


# ---------------------------------------------------------------------------
# Safe-restart watchdog
# ---------------------------------------------------------------------------

_restart_queue: queue.Queue = queue.Queue()


class RestartRequest(BaseModel):
    service: str = "llm_agent"


@app.post("/jarvis/request-restart")
def jarvis_request_restart(req: RestartRequest):
    svc = req.service.strip()
    if svc not in _DOCKER_SERVICES:
        raise HTTPException(400, f"Unknown Docker service '{svc}'. Known: {_DOCKER_SERVICES}")
    _restart_queue.put(svc)
    print(f"[MacBridge] Restart queued for '{svc}'")
    return {"status": f"Restart queued for '{svc}'"}


def _restart_watchdog() -> None:
    while True:
        try:
            service = _restart_queue.get(timeout=2)
            print(f"[MacBridge] Watchdog: restarting '{service}' in 3 s…")
            threading.Event().wait(3)
            cmd = f"cd {_JARVIS_ROOT} && docker compose restart {service}"
            result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                print(f"[MacBridge] Watchdog: '{service}' restarted OK.")
            else:
                print(f"[MacBridge] Watchdog: restart failed — {result.stderr[:200]}")
        except queue.Empty:
            continue
        except Exception as exc:
            print(f"[MacBridge] Watchdog error: {exc}")


threading.Thread(target=_restart_watchdog, daemon=True, name="restart-watchdog").start()


# ---------------------------------------------------------------------------
# Developer tools — read/write/run on any project on the Mac
# ---------------------------------------------------------------------------

class DevWriteRequest(BaseModel):
    path: str
    content: str

class DevShellRequest(BaseModel):
    cmd: str
    cwd: str = ""
    timeout: int = 120

class DevSearchRequest(BaseModel):
    directory: str
    pattern: str
    file_pattern: str = ""


@app.get("/dev/read")
def dev_read(path: str = Query(...)):
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise HTTPException(404, f"File not found: {path}")
    try:
        with open(expanded, encoding="utf-8") as f:
            return {"path": path, "content": f.read()}
    except Exception as e:
        raise HTTPException(500, f"Read failed: {e}")


@app.get("/dev/list")
def dev_list(path: str = Query(...)):
    expanded = os.path.expanduser(path)
    if not os.path.isdir(expanded):
        raise HTTPException(404, f"Not a directory: {path}")
    items = []
    for name in sorted(os.listdir(expanded)):
        full = os.path.join(expanded, name)
        items.append({
            "name": name,
            "type": "dir" if os.path.isdir(full) else "file",
            "path": full,
        })
    return {"path": path, "items": items}


@app.post("/dev/write")
def dev_write(req: DevWriteRequest):
    import shutil
    expanded = os.path.expanduser(req.path)
    parent = os.path.dirname(expanded)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(expanded):
        shutil.copy2(expanded, expanded + ".bak")
    try:
        with open(expanded, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"status": f"Written {req.path} ({len(req.content)} bytes)"}
    except Exception as e:
        raise HTTPException(500, f"Write failed: {e}")


@app.post("/dev/shell")
def dev_shell(req: DevShellRequest):
    cwd = os.path.expanduser(req.cwd) if req.cwd else None
    if cwd and not os.path.isdir(cwd):
        raise HTTPException(400, f"Working directory not found: {req.cwd}")
    try:
        result = subprocess.run(
            req.cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=req.timeout,
        )
        output = (result.stdout + result.stderr).strip()
        return {
            "cmd": req.cmd,
            "cwd": req.cwd or os.getcwd(),
            "returncode": result.returncode,
            "output": output[:8000],
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(408, f"Command timed out after {req.timeout}s")


@app.post("/dev/search")
def dev_search(req: DevSearchRequest):
    expanded = os.path.expanduser(req.directory)
    if not os.path.isdir(expanded):
        raise HTTPException(404, f"Directory not found: {req.directory}")
    cmd = ["grep", "-rn", "--include", req.file_pattern or "*", req.pattern, expanded]
    if not req.file_pattern:
        cmd = ["grep", "-rn", req.pattern, expanded]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        lines = result.stdout.strip().splitlines()
        rel = [line.replace(expanded.rstrip("/") + "/", "") for line in lines]
        return {"matches": rel[:200], "total": len(lines)}
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Search timed out")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    print(f"[MacBridge] Starting on http://0.0.0.0:{PORT}")
    print(f"[MacBridge] Reachable from Docker at host.docker.internal:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
