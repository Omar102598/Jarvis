"""Action journal + undo — a safety rail for things JARVIS does on your behalf.

Transactional/stateful actions call record_action() to log what happened (and,
when reversible, how to undo it). Two tools expose that:
  • get_recent_actions — "what have you done recently?" (audit trail)
  • undo_last_action   — reverse the most recent reversible action ("undo that")

Non-reversible actions (a sent email, a placed order) are still logged for
awareness, but undo reports that they can't be taken back.

Redis: actions:log — list of {id, kind, summary, undo, ts}, newest-first.
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
_KEY = "actions:log"


def record_action(kind: str, summary: str, undo: dict | None = None) -> None:
    """Log an action JARVIS took. `undo` (optional) is a descriptor the undo
    handler understands, e.g. {"op": "del_task", "id": "abc"}. Never raises."""
    try:
        _r.lpush(_KEY, json.dumps({
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "summary": summary,
            "undo": undo,
            "ts": datetime.now(timezone.utc).isoformat(),
        }))
        _r.ltrim(_KEY, 0, 199)
    except Exception:
        pass


# --- undo handlers: op name → callable(descriptor) -> (ok, message) ----------

def _undo_del_task(d: dict):
    tid = d.get("id")
    key = f"{USER_ID}:jarvis:tasks"
    if tid and _r.hdel(key, tid):
        return True, "removed the task I added"
    return False, "the task was already gone"


def _undo_reopen_task(d: dict):
    tid = d.get("id")
    key = f"{USER_ID}:jarvis:tasks"
    raw = _r.hget(key, tid) if tid else None
    if not raw:
        return False, "that task no longer exists"
    t = json.loads(raw)
    t["status"] = d.get("prev_status", "inbox")
    t["done_at"] = ""
    _r.hset(key, tid, json.dumps(t))
    return True, f"reopened “{t.get('text','')}”"


def _undo_restore_key(d: dict):
    """Restore a Redis string key to a prior value (generic reversible)."""
    key, prev = d.get("key"), d.get("prev")
    if not key:
        return False, "nothing to restore"
    if prev is None:
        _r.delete(key)
    else:
        _r.set(key, prev)
    return True, f"restored {d.get('label', key)}"


_UNDO_OPS = {
    "del_task": _undo_del_task,
    "reopen_task": _undo_reopen_task,
    "restore_key": _undo_restore_key,
}


@tool
def get_recent_actions(limit: int = 10) -> str:
    """Show the recent actions JARVIS has taken on the user's behalf (audit trail).

    Use for "what did you just do?", "what have you changed?".
    """
    try:
        rows = _r.lrange(_KEY, 0, max(1, min(limit, 30)) - 1) or []
    except Exception:
        rows = []
    if not rows:
        return "I haven't taken any logged actions recently."
    out = []
    for raw in rows:
        try:
            a = json.loads(raw)
            when = a.get("ts", "")[11:16]
            rev = " (undoable)" if a.get("undo") else ""
            out.append(f"• {when} — {a.get('summary','')}{rev}")
        except Exception:
            continue
    return "Recent actions:\n" + "\n".join(out)


@tool
def undo_last_action() -> str:
    """Undo the most recent reversible action ("undo that", "never mind, undo it").

    Reverses reversible actions (a task added/completed, a setting changed).
    Actions that can't be taken back (a sent message, a placed order) are
    reported as such.
    """
    try:
        rows = _r.lrange(_KEY, 0, 29) or []
    except Exception:
        rows = []
    for idx, raw in enumerate(rows):
        try:
            a = json.loads(raw)
        except Exception:
            continue
        undo = a.get("undo")
        if not undo:
            continue
        handler = _UNDO_OPS.get(undo.get("op"))
        if not handler:
            continue
        ok, msg = handler(undo)
        # Remove this entry from the log regardless (consumed).
        try:
            _r.lrem(_KEY, 1, raw)
        except Exception:
            pass
        return (f"Done — {msg}." if ok else f"Couldn't undo that — {msg}.")
    # Nothing reversible; report the most recent action if any.
    if rows:
        try:
            last = json.loads(rows[0]).get("summary", "")
            return f"The last thing I did ({last}) can't be undone."
        except Exception:
            pass
    return "Nothing to undo."
