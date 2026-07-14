"""get_readiness — the day's fused recovery/readiness score.

The mobile gateway computes one readiness score (0-100) from HealthKit signals
(HRV, resting HR, sleep, prior training load) against the user's own baselines
whenever a snapshot arrives. This tool reads it so JARVIS, Apollo, and Kai all
speak from the same number.
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
def get_readiness() -> str:
    """Get today's readiness/recovery score (0-100) with the factors behind it.

    Use for "how recovered am I?", "should I train hard today?", or to set the
    tone of a workout/plan around the user's recovery.
    """
    raw = _r.get("user:readiness:today")
    if not raw:
        return ("No readiness score yet — it's computed when today's HealthKit "
                "snapshot syncs from the iPhone.")
    try:
        d = json.loads(raw)
    except Exception:
        return "Readiness data is unreadable right now."

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stale = " (from an earlier day — today's health hasn't synced yet)" \
        if d.get("date") != today else ""
    factors = d.get("factors") or []
    fac = (" Drivers: " + "; ".join(factors) + ".") if factors else ""
    return f"Readiness {d.get('score','?')}/100 — {d.get('band','')}{stale}.{fac}"
