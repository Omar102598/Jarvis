"""JARVIS self-modification tools — read, write, and rebuild its own code.

Allows JARVIS to extend itself: add new tools, fix broken ones, and restart
affected services — all without the user having to touch code manually.

Safety rules enforced by the mac_bridge:
  - Reads are unrestricted within the JARVIS repo.
  - Writes are restricted to the JARVIS repo directory.
  - Rebuilds only work for known service names.
"""

import os

import httpx
from langchain_core.tools import tool

_HOST = os.environ.get("MAC_BRIDGE_HOST", "host.docker.internal")
_PORT = int(os.environ.get("MAC_BRIDGE_PORT", "7777"))
_BASE = f"http://{_HOST}:{_PORT}"


def _get(endpoint: str, **params) -> dict:
    r = httpx.get(f"{_BASE}{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(endpoint: str, **body) -> dict:
    r = httpx.post(f"{_BASE}{endpoint}", json=body, timeout=120)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Write safety guard
# ---------------------------------------------------------------------------

_BLOCKED_PATHS = {".env", "docker-compose.yml", "docker-compose.gpu.yml"}
_BLOCKED_CONTENT_PATTERNS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "MOBILE_API_KEY",
    "GITHUB_TOKEN",
    "SPOTIFY_CLIENT_SECRET",
]


def _validate_write(path: str, content: str) -> str | None:
    """Return an error string if the write should be rejected, else None."""
    basename = os.path.basename(path)
    if basename in _BLOCKED_PATHS:
        return f"Writing to '{basename}' is blocked for safety — edit it manually."
    if ".." in path or path.startswith("/"):
        return "Absolute paths and directory traversal ('..') are not allowed."
    for pat in _BLOCKED_CONTENT_PATTERNS:
        if pat in content:
            return (
                f"Content contains blocked pattern '{pat}' — write rejected. "
                "Edit credential files manually outside of JARVIS."
            )
    return None


@tool
def jarvis_read_code(path: str) -> str:
    """Read any file in the JARVIS codebase.

    Use this to inspect existing tools, configs, or service code before
    modifying them. Path is relative to the JARVIS repo root.

    Examples: 'services/llm_agent/tools/spotify.py', 'docker-compose.yml',
              'services/mac_bridge/main.py'

    Args:
        path: File path relative to the JARVIS repo root.
    """
    try:
        data = _get("/jarvis/read", path=path)
        return data.get("content", "(empty file)")
    except httpx.HTTPStatusError as e:
        return f"Error reading {path}: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        return f"Error reading {path}: {e}"


@tool
def jarvis_write_code(path: str, content: str) -> str:
    """Write or update a file in the JARVIS codebase.

    Use to add new tools, fix bugs, or modify configs. The change takes effect
    after you call jarvis_rebuild for the affected service.

    Paths must be within the JARVIS repo (cannot write to system paths).
    To add a new tool: write to 'services/llm_agent/tools/mytool.py', then
    update '__init__.py' and 'main.py' to import it, then rebuild llm_agent.

    Args:
        path: File path relative to the JARVIS repo root.
        content: Full file content to write.
    """
    err = _validate_write(path, content)
    if err:
        return err
    try:
        data = _post("/jarvis/write", path=path, content=content)
        return data.get("status", f"Written: {path}")
    except httpx.HTTPStatusError as e:
        return f"Error writing {path}: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        return f"Error writing {path}: {e}"


@tool
def jarvis_rebuild(service: str) -> str:
    """Rebuild and restart a JARVIS Docker service to apply code changes.

    Call this after jarvis_write_code to make the new code live.
    For native services (wake_word, stt, tts_mac, speaker_verify, mac_bridge),
    this restarts the process without Docker.

    Docker services: llm_agent, dashboard, agent_runner, mobile_gateway
    Native services: stt, tts_mac, wake_word, speaker_verify, mac_bridge

    Args:
        service: Service name, e.g. 'llm_agent', 'dashboard', 'stt'
    """
    try:
        data = _post("/jarvis/rebuild", service=service)
        return data.get("status", f"Rebuild triggered for: {service}")
    except httpx.HTTPStatusError as e:
        return f"Error rebuilding {service}: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        return f"Error rebuilding {service}: {e}"


@tool
def jarvis_list_files(directory: str = "services/llm_agent/tools") -> str:
    """List files in a JARVIS repo directory.

    Use before reading or writing to understand what already exists.
    To find a specific file anywhere in the repo, use jarvis_find_file instead.

    Args:
        directory: Directory path relative to JARVIS repo root.
                   Defaults to the tools directory.
    """
    try:
        data = _get("/jarvis/list", path=directory)
        files = data.get("files", [])
        return "\n".join(files) if files else "(empty directory)"
    except Exception as e:
        return f"Error listing {directory}: {e}"


@tool
def jarvis_find_file(pattern: str, directory: str = "") -> str:
    """Find files by name pattern anywhere in the JARVIS repo.

    Use this when you don't know where a file is. No path required.

    Examples:
      jarvis_find_file("mac.py")          → finds services/llm_agent/tools/mac.py
      jarvis_find_file("*.py", "services/llm_agent")  → all Python files under that dir
      jarvis_find_file("docker-compose.yml")           → finds the compose file

    Args:
        pattern: Filename or glob pattern, e.g. 'mac.py', '*.py', 'requirements.txt'
        directory: Optional repo-relative subdirectory to narrow the search.
    """
    try:
        data = _post("/jarvis/find", pattern=pattern, directory=directory)
        matches = data.get("matches", [])
        root = data.get("repo_root", "")
        if not matches:
            return f"No files matching '{pattern}' found in the JARVIS repo."
        header = f"Found {len(matches)} file(s) (repo root: {root}):"
        return header + "\n" + "\n".join(matches)
    except Exception as e:
        return f"Error finding '{pattern}': {e}"


@tool
def jarvis_grep_code(pattern: str, directory: str = "", file_pattern: str = "") -> str:
    """Search for a text or regex pattern across the JARVIS source code.

    Use to find where a function is defined, locate a bug, or understand
    how something is currently implemented — without needing to know the file path.

    Examples:
      jarvis_grep_code("mac_chrome_navigate")         → find all uses
      jarvis_grep_code("def _chrome_osa")             → find the function definition
      jarvis_grep_code("osascript", file_pattern="*.py")  → find all osascript calls

    Args:
        pattern: Text or regex to search for.
        directory: Optional repo-relative subdirectory to search in.
        file_pattern: Optional file extension filter, e.g. '*.py', '*.yml'
    """
    try:
        data = _post("/jarvis/grep", pattern=pattern, directory=directory,
                     file_pattern=file_pattern)
        matches = data.get("matches", [])
        total = data.get("total", 0)
        root = data.get("repo_root", "")
        if not matches:
            return f"No matches for '{pattern}' in JARVIS repo."
        header = f"{total} match(es) for '{pattern}' (repo root: {root}):"
        if total > len(matches):
            header += f" (showing first {len(matches)})"
        return header + "\n\n" + "\n".join(matches)
    except Exception as e:
        return f"Error searching for '{pattern}': {e}"
