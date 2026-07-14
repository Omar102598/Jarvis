"""get_packages — the delivery log Sentry's doorbell concierge keeps.

Sentry logs every package it sees at the door (flagging ones that arrived while
you were away). This surfaces that log: "did any packages come?", "anything get
delivered while I was out?".
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)


@tool
def get_packages(since_hours: int = 48) -> str:
    """List deliveries Sentry has seen at the door recently.

    Args:
        since_hours: how far back to look (default 48h).
    """
    try:
        rows = _r.lrange("packages:log", 0, 49) or []
    except Exception:
        rows = []
    if not rows:
        return "No deliveries logged recently."
    cutoff = datetime.now(timezone.utc).timestamp() - since_hours * 3600
    out = []
    for raw in rows:
        try:
            p = json.loads(raw)
            ts = p.get("ts", "")
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.timestamp() < cutoff:
                continue
            when = t.strftime("%a %-I:%M %p")
            tag = " (while you were out)" if p.get("away") else ""
            out.append(f"📦 {when} — {p.get('summary','a delivery')}{tag}")
        except Exception:
            continue
    if not out:
        return f"No deliveries in the last {since_hours}h."
    return "Recent deliveries:\n" + "\n".join(out)
