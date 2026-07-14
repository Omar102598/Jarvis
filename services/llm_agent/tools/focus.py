"""focus_mode — deep-work sessions where JARVIS goes quiet.

Starting Focus sets a Redis flag (jarvis:focus) with an auto-expiring TTL. While
it's active, Synapse's notification router HOLDS all non-urgent notifications
(they batch into a digest that arrives when Focus ends); genuinely urgent alerts
(a person at the door) still come through. The flag expires on its own, so a
forgotten session ends cleanly.

Pairs naturally with music/DND: after starting Focus, JARVIS can start a focus
playlist (spotify_control) on the user's request.
"""

from __future__ import annotations

import json
import os
import time

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)
_KEY = "jarvis:focus"


@tool
def focus_mode(action: str = "start", minutes: int = 60) -> str:
    """Start, end, or check a deep-work Focus session (JARVIS holds non-urgent
    notifications until it ends; urgent alerts still come through).

    Args:
        action: "start" | "end" | "status".
        minutes: session length for "start" (default 60). Auto-ends at expiry.
    """
    action = (action or "start").strip().lower()

    if action == "start":
        minutes = max(5, min(int(minutes or 60), 600))
        until = time.time() + minutes * 60
        _r.set(_KEY, json.dumps({"until": until, "minutes": minutes,
                                 "started": time.time()}), ex=minutes * 60)
        return (f"Focus mode on for {minutes} minutes. I'll hold everything "
                "non-urgent and surface it when you're done. Want a focus playlist?")

    if action == "end":
        if not _r.exists(_KEY):
            return "You're not in Focus mode right now."
        _r.delete(_KEY)
        return "Focus mode off — welcome back. I'll bring you up to speed on anything you missed."

    # status
    raw = _r.get(_KEY)
    if not raw:
        return "Focus mode is off."
    try:
        d = json.loads(raw)
        remaining = int((d.get("until", 0) - time.time()) / 60)
        return f"Focus mode is on — about {max(0, remaining)} minute(s) left."
    except Exception:
        return "Focus mode is on."
