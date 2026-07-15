"""Miles — travel planning. plan_trip / get_trips.

Flight and hotel SEARCH already work via MCP (Kiwi flights, Jinko hotels), and
scrape_page handles attraction/fare pages. These tools give trip planning
structure and memory: record a trip, and get a planning workflow the brain
executes with those tools + the calendar + weather.

Redis: trips:{USER_ID} — hash of trip_id → {destination, start, end, notes, status, created}
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
_KEY = f"trips:{USER_ID}"


@tool
def plan_trip(destination: str, start: str = "", end: str = "", notes: str = "") -> str:
    """Start planning a trip — records it and returns the planning workflow.

    After calling this, actually build the plan: check the calendar for conflicts
    over the dates, search flights (Kiwi MCP) and hotels/rentals (Jinko/Airbnb
    MCP), look up weather and a few things to do (scrape_page/web_search), then
    present options for the user to approve (never book without confirmation).

    Args:
        destination: where to.
        start / end: trip dates (ISO or natural language).
        notes: preferences, budget, who's going, etc.
    """
    if not destination.strip():
        return "Where would you like to go?"
    trip = {
        "id": uuid.uuid4().hex[:8],
        "destination": destination.strip(),
        "start": start.strip(),
        "end": end.strip(),
        "notes": notes.strip(),
        "status": "planning",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _r.hset(_KEY, trip["id"], json.dumps(trip))
    except Exception:
        pass
    when = f" ({start} → {end})" if start and end else ""
    return (
        f"Trip to {trip['destination']}{when} noted. Now planning: I'll check your "
        "calendar for those dates, search flights and stays, and pull weather + a few "
        "things to do — then give you options to approve. I won't book anything "
        "without your OK."
    )


@tool
def get_trips() -> str:
    """Show trips the user is planning or has planned. Use for "what trips do I have?"."""
    trips = _r.hgetall(_KEY) or {}
    if not trips:
        return "No trips on file. Say 'plan a trip to …' to start one."
    out = []
    for _, val in trips.items():
        try:
            t = json.loads(val)
            when = f" {t['start']}→{t['end']}" if t.get("start") else ""
            out.append(f"• {t['destination']}{when} — {t.get('status','planning')}")
        except Exception:
            continue
    return "Trips:\n" + "\n".join(out)
