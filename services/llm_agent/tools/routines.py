"""detect_routines — find the user's recurring patterns from the event stream.

Synapse's durable event log (jarvis:events) now holds real history, so we can
mine it for routines: "you usually leave home around 5pm on Tue/Thu",
"you're up by 6:30 on weekdays." JARVIS can use these to anticipate and
pre-stage ("want me to book your 6pm class?").

Reads presence arrivals/departures (the most actionable signals), buckets by
weekday + hour, and reports patterns that recur across multiple weeks.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
USER_TZ = os.environ.get("USER_TZ", "America/Chicago")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@tool
def detect_routines() -> str:
    """Find the user's recurring routines from logged history (arrivals/departures).

    Use for "what are my routines?", "when do I usually leave/get home?", or to
    anticipate/pre-stage around habits.
    """
    try:
        rows = _r.xrange("jarvis:events", min="-", max="+", count=20000)
    except Exception:
        return "No event history to analyze yet."
    if not rows:
        return "No event history yet — routines build up over a few weeks."

    tz = ZoneInfo(USER_TZ)
    # bucket[(event, weekday)] -> list of hours (local)
    buckets: dict[tuple, list[int]] = defaultdict(list)
    weeks_seen: dict[tuple, set] = defaultdict(set)
    for _id, f in rows:
        if f.get("domain") != "presence":
            continue
        try:
            ev = json.loads(f.get("payload", "{}")).get("event", "")
            if ev not in ("arrived", "left"):
                continue
            ms = int(f.get("ts") or _id.split("-")[0])
            dt = datetime.fromtimestamp(ms / 1000, tz)
            key = (ev, dt.weekday())
            buckets[key].append(dt.hour + dt.minute / 60.0)
            weeks_seen[key].add(dt.isocalendar()[1])
        except Exception:
            continue

    lines = []
    for (ev, wd), hours in buckets.items():
        # A routine = recurs across ≥3 distinct weeks with a consistent time.
        if len(weeks_seen[(ev, wd)]) < 3:
            continue
        hours.sort()
        median = hours[len(hours) // 2]
        spread = max(hours) - min(hours)
        if spread > 3:   # too scattered to be a routine
            continue
        h = int(median)
        m = int((median - h) * 60)
        verb = "gets home" if ev == "arrived" else "leaves"
        lines.append(f"• {_WEEKDAYS[wd]}: usually {verb} around "
                     f"{datetime(2000,1,1,h,m).strftime('%-I:%M %p')} "
                     f"({len(weeks_seen[(ev, wd)])} weeks)")

    if not lines:
        return ("Not enough consistent history to call anything a routine yet — "
                "give it a few weeks of comings and goings.")
    return "Your routines (from history):\n" + "\n".join(sorted(lines))
