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
def propose_booking(trip_id: str, itinerary: str, total_price_usd: float,
                    booking_links: str, watch_price: bool = True) -> str:
    """File the chosen trip option in the Approval Inbox (ASSISTED booking —
    Jarvis never pays). Call after the user picks one of the options you
    presented: summarize the exact flights/stay, total price, and the direct
    booking links. Approving sends the user a card with the links to book
    themselves; until then Miles watches the price daily (if watch_price).

    Args:
        trip_id: id from plan_trip (check get_trips if unsure).
        itinerary: 2-4 line summary — airline/flight times, stay name/dates.
        total_price_usd: the all-in total you quoted.
        booking_links: the direct URLs to book, one per line.
        watch_price: keep re-checking daily and alert on a drop (default on).
    """
    raw = _r.hget(_KEY, trip_id)
    if not raw:
        return f"No trip with id {trip_id} — call get_trips to check."
    trip = json.loads(raw)
    trip.update({
        "status": "proposed",
        "itinerary": itinerary.strip(),
        "total_price_usd": float(total_price_usd),
        "booking_links": booking_links.strip(),
        "watch_price": bool(watch_price),
        "proposed": datetime.now(timezone.utc).isoformat(),
    })
    _r.hset(_KEY, trip_id, json.dumps(trip))

    # Approval Inbox entry (same schema as agent_runner/approvals.py). On
    # approve, the executor triggers Miles' confirm action, which sends the
    # booking-links card — the user books in their own browser and pays there.
    approval_id = uuid.uuid4().hex[:12]
    record = {
        "id": approval_id,
        "source": "Miles",
        "title": f"Trip: {trip['destination']} — ${total_price_usd:,.0f}",
        "text": itinerary.strip() + "\n\nApprove to get the booking links.",
        "action": {"type": "mqtt", "topic": "jarvis/agents/travel/trigger",
                   "payload": {"params": {"action": "confirm",
                                          "trip_id": trip_id}}},
        "media_url": None,
        "created": datetime.now(timezone.utc).isoformat(),
        "expires": datetime.now(timezone.utc).timestamp() + 7 * 86400,
    }
    _r.hset("jarvis:approvals:pending", approval_id, json.dumps(record))
    try:
        import paho.mqtt.publish as mqtt_publish
        mqtt_publish.single("jarvis/notify", json.dumps({
            "title": f"🟠 Approval — Trip: {trip['destination']}",
            "text": f"${total_price_usd:,.0f} — {itinerary.strip()[:200]}",
            "urgency": "urgent", "source": "Miles",
            "dedup_key": f"approval:{approval_id}",
        }), hostname=os.environ.get("MQTT_HOST", "localhost"),
            port=int(os.environ.get("MQTT_PORT", "1883")))
    except Exception:
        pass
    return (f"Filed in the Approval Inbox (${total_price_usd:,.0f}). Tell the "
            "user it's waiting on the dashboard/app — approving sends them the "
            "booking links" + (", and I'll watch the price daily meanwhile."
                               if watch_price else "."))


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
