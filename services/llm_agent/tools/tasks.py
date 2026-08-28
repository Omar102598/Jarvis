"""manage_tasks — a real GTD-style task loop (capture → next-actions → done).

A persistent inbox + next-actions list that lives across conversations and
surfaces, so "add X to my list", "what's on my plate?", and "mark Y done" all
work by voice from anywhere. Backed by a Redis hash (id → task json).

Task shape:
    {id, text, status: inbox|next|done, project, due (ISO|""), created, done_at}
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

import redis
from langchain_core.tools import tool

from .actions import record_action

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
USER_ID = os.environ.get("JARVIS_USER_ID", "default")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)
_KEY = f"{USER_ID}:jarvis:tasks"

_OPEN = ("inbox", "next")


def _all() -> list[dict]:
    try:
        raw = _r.hgetall(_KEY) or {}
    except Exception:
        return []
    out = []
    for tid, val in raw.items():
        try:
            t = json.loads(val)
            t["id"] = tid
            out.append(t)
        except Exception:
            continue
    return out


def _save(t: dict) -> None:
    _r.hset(_KEY, t["id"], json.dumps(t))


def _find(query: str) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return []
    exact = [t for t in _all() if t["id"] == query]
    if exact:
        return exact
    return [t for t in _all() if q in t.get("text", "").lower()]


def _fmt(t: dict) -> str:
    mark = "▸" if t.get("status") == "next" else "•"
    proj = f" [{t['project']}]" if t.get("project") else ""
    due = f" (due {t['due'][:10]})" if t.get("due") else ""
    return f"{mark} {t.get('text','')}{proj}{due}"


@tool
def manage_tasks(action: str, text: str = "", task_id: str = "",
                 project: str = "", due: str = "") -> str:
    """Manage the user's task list (inbox, next-actions, projects).

    Actions:
      • "add"     — capture a new task to the inbox. Requires text; optional
                    project, due (ISO date).
      • "list"    — show open tasks (next-actions first). Optional project filter.
      • "next"    — promote a task to a next-action. Match by task_id or text.
      • "done"    — complete a task. Match by task_id or text.
      • "remove"  — delete a task. Match by task_id or text.
      • "clear"   — remove all completed tasks.

    Args:
        action: one of add | list | next | done | remove | clear.
        text: task text (for add), or a substring to match (next/done/remove).
        task_id: exact task id, if known.
        project: project/label to attach (add) or filter by (list).
        due: ISO date (YYYY-MM-DD) the task is due, optional.
    """
    action = (action or "").strip().lower()

    if action == "add":
        if not text.strip():
            return "Nothing to add — give me the task text."
        t = {
            "id": uuid.uuid4().hex[:10],
            "text": text.strip(),
            "status": "inbox",
            "project": project.strip(),
            "due": due.strip(),
            "created": datetime.now(timezone.utc).isoformat(),
            "done_at": "",
        }
        _save(t)
        record_action("task", f"added task “{t['text']}”",
                      undo={"op": "del_task", "id": t["id"]})
        return f"Added to your list: {t['text']}" + (f" [{t['project']}]" if t["project"] else "")

    if action == "list":
        items = [t for t in _all() if t.get("status") in _OPEN]
        if project.strip():
            items = [t for t in items if t.get("project", "").lower() == project.strip().lower()]
        if not items:
            return "Your list is clear — nothing open."
        # next-actions first, then by created
        items.sort(key=lambda t: (t.get("status") != "next", t.get("created", "")))
        n_next = sum(1 for t in items if t.get("status") == "next")
        header = f"You have {len(items)} open ({n_next} next-action{'s' if n_next != 1 else ''}):"
        return header + "\n" + "\n".join(_fmt(t) for t in items[:25])

    if action in ("next", "done", "remove"):
        key = task_id or text
        matches = _find(key)
        if not matches:
            return f"No task matches “{key}”."
        if len(matches) > 1 and not task_id:
            return ("Which one?\n" + "\n".join(f"  ({t['id']}) {t.get('text','')}" for t in matches[:8]))
        t = matches[0]
        if action == "next":
            t["status"] = "next"
            _save(t)
            return f"Marked as a next-action: {t['text']}"
        if action == "done":
            prev = t.get("status", "inbox")
            t["status"] = "done"
            t["done_at"] = datetime.now(timezone.utc).isoformat()
            _save(t)
            record_action("task", f"completed “{t['text']}”",
                          undo={"op": "reopen_task", "id": t["id"], "prev_status": prev})
            return f"Done: {t['text']} ✓"
        _r.hdel(_KEY, t["id"])
        return f"Removed: {t['text']}"

    if action == "clear":
        removed = 0
        for t in _all():
            if t.get("status") == "done":
                _r.hdel(_KEY, t["id"])
                removed += 1
        return f"Cleared {removed} completed task(s)."

    return ("Unknown action. Use add | list | next | done | remove | clear.")
