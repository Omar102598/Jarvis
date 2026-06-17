"""Developer agent tools — let JARVIS read, write, run, and search any project on the Mac.

Works on any codebase on the Mac, not just the JARVIS repo.
All calls route through mac_bridge (native) via host.docker.internal:7777.
"""

import os

import httpx
from langchain_core.tools import tool

_HOST = os.environ.get("MAC_BRIDGE_HOST", "host.docker.internal")
_PORT = int(os.environ.get("MAC_BRIDGE_PORT", "7777"))
_BASE = f"http://{_HOST}:{_PORT}"


def _get(path: str, **params) -> dict:
    r = httpx.get(f"{_BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path: str, **body) -> dict:
    r = httpx.post(f"{_BASE}{path}", json=body, timeout=130)
    r.raise_for_status()
    return r.json()


@tool
def dev_read_file(path: str) -> str:
    """Read any file on the Mac by absolute or ~ path.

    Use to inspect source code, configs, logs, or any text file in a project.

    Args:
        path: Absolute or ~ path, e.g. '~/Projects/myapp/src/index.ts'
    """
    try:
        data = _get("/dev/read", path=path)
        return data.get("content", "(empty file)")
    except httpx.HTTPStatusError as e:
        return f"Error reading {path}: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        return f"Error reading {path}: {e}"


@tool
def dev_write_file(path: str, content: str) -> str:
    """Write or create a file at any path on the Mac.

    Creates parent directories if needed. Keeps a .bak backup of the original.
    Use this to add features, fix bugs, or update configs in any project.

    Args:
        path: Absolute or ~ path, e.g. '~/Projects/myapp/src/utils.ts'
        content: Full file content to write
    """
    try:
        data = _post("/dev/write", path=path, content=content)
        return data.get("status", f"Written: {path}")
    except httpx.HTTPStatusError as e:
        return f"Error writing {path}: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        return f"Error writing {path}: {e}"


@tool
def dev_list_dir(path: str) -> str:
    """List the contents of any directory on the Mac.

    Use to explore a project structure before reading or modifying files.

    Args:
        path: Absolute or ~ path, e.g. '~/Projects/myapp' or '~/Projects/myapp/src'
    """
    try:
        data = _get("/dev/list", path=path)
        items = data.get("items", [])
        lines = []
        for item in items:
            prefix = "[DIR] " if item["type"] == "dir" else "      "
            lines.append(f"{prefix}{item['name']}")
        return "\n".join(lines) if lines else "(empty directory)"
    except httpx.HTTPStatusError as e:
        return f"Error listing {path}: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        return f"Error listing {path}: {e}"


@tool
def dev_shell(cmd: str, cwd: str = "") -> str:
    """Run a shell command on the Mac in a project directory.

    Use for: git status/commit/push, npm install/run, pytest, cargo build,
    make, pip install, or any build/test/lint command.

    Returns stdout + stderr combined with the exit code.

    Args:
        cmd: Shell command, e.g. 'npm run build' or 'git log --oneline -10'
        cwd: Working directory (absolute or ~ path). Required for project commands.
             e.g. '~/Projects/myapp'
    """
    try:
        data = _post("/dev/shell", cmd=cmd, cwd=cwd, timeout=120)
        rc = data.get("returncode", 0)
        out = data.get("output", "").strip()
        status = "✓" if rc == 0 else f"✗ (exit {rc})"
        return f"{status}\n{out}" if out else status
    except httpx.HTTPStatusError as e:
        return f"Shell error: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        return f"Shell error: {e}"


@tool
def dev_search_code(directory: str, pattern: str, file_pattern: str = "") -> str:
    """Search for a text or regex pattern across a project's source files.

    Use to find where a function is defined, where an import is used, find TODOs,
    locate error messages, etc.

    Args:
        directory: Project root to search in, e.g. '~/Projects/myapp'
        pattern: Text or regex to search for, e.g. 'useEffect', 'TODO', 'def handle_'
        file_pattern: Optional file extension filter, e.g. '*.py', '*.ts', '*.js'
    """
    try:
        data = _post("/dev/search", directory=directory, pattern=pattern, file_pattern=file_pattern)
        matches = data.get("matches", [])
        total = data.get("total", 0)
        if not matches:
            return f"No matches for '{pattern}' in {directory}"
        header = f"{total} match{'es' if total != 1 else ''} for '{pattern}'"
        if total > len(matches):
            header += f" (showing first {len(matches)})"
        return header + "\n\n" + "\n".join(matches)
    except httpx.HTTPStatusError as e:
        return f"Search error: {e.response.json().get('detail', str(e))}"
    except Exception as e:
        return f"Search error: {e}"
