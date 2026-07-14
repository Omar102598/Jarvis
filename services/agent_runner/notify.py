"""Agent-side helper to route a notification through Synapse's notify router.

Instead of publishing straight to ``jarvis/surfaces/iphone/push`` (which fires an
immediate card, one per agent → notification spam), agents publish to
``jarvis/notify`` and let Synapse decide:

    urgency="urgent"           → surfaced immediately (Sentry person-at-door,
                                 a favorite class window about to close)
    urgency="normal" | "low"   → batched into the next digest card

A media_url (Ring snapshot / live view) always goes through immediately —
Synapse never batches a camera card.

Usage from any agent:
    from notify import route_notification
    route_notification("Remy", "Grocery list ready — 3 stores, $118.",
                       urgency="normal")

Falls back to a direct surface push if MQTT publish fails, so a notification is
never silently lost.
"""

from __future__ import annotations

import json
import os

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))


def route_notification(source: str, text: str, *, title: str = "",
                       media_url: str = "", urgency: str = "normal",
                       dedup_key: str = "") -> bool:
    """Send a notification through the router. Returns True on publish success."""
    body = {
        "title": title or source or "Jarvis",
        "text": text,
        "urgency": urgency,
        "source": source,
    }
    if media_url:
        body["media_url"] = media_url
    if dedup_key:
        body["dedup_key"] = dedup_key
    try:
        import paho.mqtt.publish as mqtt_pub
        mqtt_pub.single("jarvis/notify", json.dumps(body),
                        hostname=MQTT_HOST, port=MQTT_PORT)
        return True
    except Exception:
        # Fail open: deliver directly rather than lose the message.
        try:
            import paho.mqtt.publish as mqtt_pub
            direct = {"title": body["title"], "text": text}
            if media_url:
                direct["media_url"] = media_url
            mqtt_pub.single("jarvis/surfaces/iphone/push", json.dumps(direct),
                            hostname=MQTT_HOST, port=MQTT_PORT)
        except Exception:
            return False
        return True
