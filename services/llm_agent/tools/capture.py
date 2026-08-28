"""quick_capture — hands-free "just capture this" that files itself.

Say "note that…", "remind me to…", "add milk to the list", "idea:…" from
anywhere and it lands in the right place without you choosing a tool:
  • actionable ("remind me", "need to", "todo")  → the GTD task inbox
  • shopping  ("buy", "pick up", "we're out of")  → a grocery capture list
  • anything else                                 → notes/ideas

One call, correctly routed — ideal for glasses/voice capture on the move.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
USER_ID = os.environ.get("JARVIS_USER_ID", "default")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)

_TASKS_KEY = f"{USER_ID}:jarvis:tasks"     # same store as manage_tasks
_GROCERY_CAPTURE = "grocery:capture"
_NOTES_KEY = "capture:notes"

_TASK_HINTS = ("remind me", "remember to", "need to", "have to", "todo", "to-do",
               "don't forget", "make sure", "schedule", "call ", "email ", "book ")
_SHOP_HINTS = ("buy ", "pick up", "we're out", "were out", "out of", "add to the list",
               "grocery", "groceries", "get more", "restock")


def _classify(text: str) -> str:
    t = text.lower()
    if any(h in t for h in _SHOP_HINTS):
        return "shopping"
    if any(h in t for h in _TASK_HINTS):
        return "task"
    return "note"


@tool
def quick_capture(text: str, kind: str = "auto") -> str:
    """Capture a quick thought hands-free and file it automatically.

    Args:
        text: the thing to capture, in the user's words.
        kind: 'auto' (default, classify it), or force 'task' | 'shopping' | 'note'.
    """
    text = text.strip()
    if not text:
        return "What should I capture?"
    kind = (kind or "auto").strip().lower()
    if kind not in ("task", "shopping", "note"):
        kind = _classify(text)
    now = datetime.now(timezone.utc).isoformat()

    try:
        if kind == "task":
            tid = uuid.uuid4().hex[:10]
            _r.hset(_TASKS_KEY, tid, json.dumps({
                "id": tid, "text": text, "status": "inbox", "project": "",
                "due": "", "created": now, "done_at": "",
            }))
            return f"Captured to your task inbox: {text}"
        if kind == "shopping":
            _r.lpush(_GROCERY_CAPTURE, json.dumps({"item": text, "ts": now}))
            _r.ltrim(_GROCERY_CAPTURE, 0, 199)
            return f"Added to your shopping list: {text}"
        _r.lpush(_NOTES_KEY, json.dumps({"text": text, "ts": now}))
        _r.ltrim(_NOTES_KEY, 0, 499)
        return f"Noted: {text}"
    except Exception as exc:
        return f"Couldn't capture that: {exc}"
