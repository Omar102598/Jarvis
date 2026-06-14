"""Notes / journal tool for JARVIS — a dated markdown notes vault.

Distinct from the scratch filesystem (files.py): this manages a personal
notes vault (Obsidian-compatible markdown) where entries are timestamped and
searchable — good for journaling, capturing ideas, and daily logs.

    NOTES_DIR (default /data/notes)
"""

import os
from datetime import datetime
from pathlib import Path

from langchain_core.tools import tool

NOTES_DIR = Path(os.environ.get("NOTES_DIR", "/data/notes")).resolve()


@tool
def manage_notes(action: str, title: str = "", content: str = "") -> str:
    """Manage the user's notes vault (markdown).

    Args:
        action: 'add' (append a timestamped note to today's daily file),
            'new' (create a titled note), 'search' (find notes containing
            text from `title`), or 'list' (recent notes).
        title: For 'new', the note title. For 'search', the query text.
        content: The note body (for 'add' and 'new').
    """
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    action = action.lower().strip()

    if action == "add":
        if not content:
            return "Nothing to add — content was empty."
        day = datetime.now().strftime("%Y-%m-%d")
        stamp = datetime.now().strftime("%H:%M")
        daily = NOTES_DIR / f"{day}.md"
        with daily.open("a", encoding="utf-8") as f:
            if daily.stat().st_size == 0:
                f.write(f"# {day}\n\n")
            f.write(f"- **{stamp}** — {content}\n")
        return f"Added to today's notes ({day}.md)."

    if action == "new":
        if not title:
            return "Please give the note a title."
        safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title).strip()
        path = NOTES_DIR / f"{safe or 'untitled'}.md"
        path.write_text(f"# {title}\n\n{content}\n", encoding="utf-8")
        return f"Created note '{title}'."

    if action == "search":
        if not title:
            return "What should I search your notes for?"
        query = title.lower()
        hits = []
        for md in NOTES_DIR.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if query in text.lower():
                # show the first matching line for context
                for line in text.splitlines():
                    if query in line.lower():
                        hits.append(f"{md.name}: {line.strip()[:120]}")
                        break
        return "\n".join(hits[:20]) if hits else f"No notes mention '{title}'."

    if action == "list":
        notes = sorted(NOTES_DIR.glob("*.md"), key=os.path.getmtime, reverse=True)
        if not notes:
            return "Your notes vault is empty."
        return "Recent notes:\n" + "\n".join(f"• {n.name}" for n in notes[:15])

    return f"Unknown action '{action}'. Use add, new, search, or list."
