"""Miles — travel agent (runner side): assisted booking + daily price watch.

Planning happens IN the brain — it already has the flight/hotel search MCPs
(Kiwi, Jinko, Airbnb) and the plan_trip / propose_booking tools; MCP tools
never load into runner agents. This agent handles the two parts that need to
run outside a conversation:

  confirm  (triggered by an approved Approval-Inbox card): mark the trip and
           send the user the booking-links card — ASSISTED booking, the user
           pays in their own browser. Jarvis never holds payment details.
  watch    (daily cron): for each proposed, price-watched, future trip, ask
           the BRAIN over the bus to re-run the search (it has the MCPs) and
           report the current total; alert on a drop ≥ WATCH_DROP_PCT.

Design doc: docs/TRAVEL_AGENT_DESIGN.md (mode A chosen 2026-07-18).
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_pub

from base_agent import BaseAgent
from notify import route_notification

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
USER_ID = os.environ.get("JARVIS_USER_ID", "default")
TRIPS_KEY = f"trips:{USER_ID}"
WATCH_DROP_PCT = float(os.environ.get("TRAVEL_WATCH_DROP_PCT", "5"))
BRAIN_TIMEOUT_S = int(os.environ.get("TRAVEL_BRAIN_TIMEOUT_S", "120"))


class TravelAgent(BaseAgent):
    async def run(self) -> str:
        action = (self.params or {}).get("action", "watch")
        if action == "confirm":
            return self._confirm((self.params or {}).get("trip_id", ""))
        return await self._watch()

    # ------------------------------------------------------------- confirm

    def _confirm(self, trip_id: str) -> str:
        raw = self.r.hget(TRIPS_KEY, trip_id)
        if not raw:
            return f"Miles: approved trip {trip_id} not found."
        trip = json.loads(raw)
        trip["status"] = "approved — book via links"
        trip["approved"] = datetime.now(timezone.utc).isoformat()
        self.r.hset(TRIPS_KEY, trip_id, json.dumps(trip))
        links = trip.get("booking_links", "")
        route_notification(
            "Miles",
            f"{trip.get('itinerary','')}\n\nBook here:\n{links}",
            title=f"✈️ {trip.get('destination','Trip')} — ready to book "
                  f"(${trip.get('total_price_usd',0):,.0f})",
            urgency="urgent")
        return (f"Miles: sent the booking links for {trip.get('destination')} "
                "— book on your phone, payment stays with you.")

    # --------------------------------------------------------------- watch

    async def _watch(self) -> str:
        today = datetime.now(timezone.utc).date().isoformat()
        watched = []
        for tid, raw in (self.r.hgetall(TRIPS_KEY) or {}).items():
            try:
                t = json.loads(raw)
                if (t.get("status") == "proposed" and t.get("watch_price")
                        and t.get("total_price_usd")
                        and (not t.get("start") or str(t["start"]) >= today)):
                    watched.append((tid, t))
            except Exception:
                continue
        if not watched:
            return "Miles: no price-watched trips."

        results = []
        for tid, trip in watched[:3]:   # cap brain calls per run
            price = self._ask_brain_for_price(trip)
            if price is None:
                results.append(f"{trip['destination']}: recheck failed")
                continue
            old = float(trip["total_price_usd"])
            drop_pct = (old - price) / old * 100 if old else 0
            self.log_event("finding",
                           f"{trip['destination']}: ${price:,.0f} vs ${old:,.0f}")
            if drop_pct >= WATCH_DROP_PCT:
                trip["total_price_usd"] = price
                trip["price_dropped"] = datetime.now(timezone.utc).isoformat()
                self.r.hset(TRIPS_KEY, tid, json.dumps(trip))
                route_notification(
                    "Miles",
                    f"{trip['destination']} dropped {drop_pct:.0f}% — now "
                    f"${price:,.0f} (was ${old:,.0f}). The approval card links "
                    "may need a re-search before booking.",
                    title="✈️ Price drop", urgency="urgent")
                results.append(f"{trip['destination']}: DROP {drop_pct:.0f}%")
            else:
                results.append(f"{trip['destination']}: ${price:,.0f} (no drop)")
        return "Miles price watch: " + "; ".join(results)

    def _ask_brain_for_price(self, trip: dict) -> float | None:
        """Ask the brain (which holds the search MCPs) to re-price the trip.
        Same bus pattern as Vega: publish jarvis/llm/request, collect the
        jarvis/tts/{room}/speak reply."""
        room = f"miles-{uuid.uuid4().hex[:10]}"
        done = threading.Event()
        chunks: list[str] = []

        def on_message(_c, _u, msg):
            try:
                body = json.loads(msg.payload)
                if (body.get("room") or msg.topic.split("/")[2]) == room:
                    chunks.append(body.get("text", ""))
                    if body.get("is_final", True):
                        done.set()
            except Exception:
                pass

        client = mqtt.Client()
        client.on_message = on_message
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            client.subscribe(f"jarvis/tts/{room}/speak")
            client.loop_start()
            prompt = (
                f"Re-check the current total price for this exact trip using the "
                f"flight/hotel search tools: {trip.get('itinerary','')} "
                f"(destination {trip.get('destination')}, "
                f"{trip.get('start','?')} to {trip.get('end','?')}). "
                'Reply with ONLY JSON: {"total_price_usd": <number>}')
            client.publish("jarvis/llm/request", json.dumps({
                "text": prompt, "room": room, "verified": True,
                "source": "travel_watch",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }))
            done.wait(BRAIN_TIMEOUT_S)
        finally:
            client.loop_stop()
            client.disconnect()
        text = " ".join(chunks)
        match = re.search(r'"total_price_usd"\s*:\s*([0-9][0-9,.]*)', text)
        if not match:
            match = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text)
        try:
            return float(match.group(1).replace(",", "")) if match else None
        except Exception:
            return None
