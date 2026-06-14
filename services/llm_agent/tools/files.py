"""Filesystem tools for JARVIS — sandboxed read/write under a workspace dir.

All paths are resolved relative to JARVIS_WORKSPACE (default /data/workspace)
and access outside that directory is refused. This gives JARVIS a place to
keep notes, todo lists, logs, and scratch files without exposing the host FS.

    write_file("todo.md", "- buy milk")
    read_file("todo.md")
    list_files(".")
"""

import os
from pathlib import Path

from langchain_core.tools import tool

WORKSPACE = Path(os.environ.get("JARVIS_WORKSPACE", "/data/workspace")).resolve()
MAX_READ = int(os.environ.get("FILE_MAX_READ_CHARS", "20000"))


def _safe_path(path: str) -> Path:
    """Resolve a user path inside the workspace, or raise ValueError."""
    candidate = (WORKSPACE / path.lstrip("/")).resolve()
    if candidate != WORKSPACE and WORKSPACE not in candidate.parents:
        raise ValueError("Path escapes the JARVIS workspace.")
    return candidate


@tool
def read_file(path: str) -> str:
    """Read a text file from the JARVIS workspace.

    Args:
        path: Path relative to the workspace, e.g. 'todo.md', 'notes/ideas.txt'.
    """
    try:
        target = _safe_path(path)
    except ValueError as e:
        return str(e)
    if not target.is_file():
        return f"No such file: {path}"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Could not read {path}: {e}"
    return text[:MAX_READ] if text else f"{path} is empty."


@tool
def write_file(path: str, content: str, append: bool = False) -> str:
    """Write (or append to) a text file in the JARVIS workspace.

    Args:
        path: Path relative to the workspace, e.g. 'todo.md'.
        content: The text to write.
        append: If true, add to the end of the file instead of overwriting.
    """
    try:
        target = _safe_path(path)
    except ValueError as e:
        return str(e)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with target.open(mode, encoding="utf-8") as f:
            f.write(content if content.endswith("\n") else content + "\n")
    except OSError as e:
        return f"Could not write {path}: {e}"
    return f"{'Appended to' if append else 'Wrote'} {path} ({len(content)} chars)."


@tool
def list_files(path: str = ".") -> str:
    """List files and folders in a workspace directory.

    Args:
        path: Directory relative to the workspace. Default is the root.
    """
    try:
        target = _safe_path(path)
    except ValueError as e:
        return str(e)
    if not target.exists():
        return f"No such directory: {path}"
    if not target.is_dir():
        return f"{path} is a file, not a directory."

    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
    if not entries:
        return f"{path} is empty."
    lines = [f"{'📄' if p.is_file() else '📁'} {p.name}" for p in entries]
    return "\n".join(lines)
