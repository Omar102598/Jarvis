"""arrive_home / leave_home — manual presence triggers when the geofence misses.

iOS geofencing is fragile (a force-quit app stops firing region events), so these
give a reliable manual fallback: "Jarvis, I'm home" runs the arrival scene +
clears away mode; "I'm heading out" runs the departure scene + arms away mode.
The brain's own reply ("Welcome home, sir") is the spoken greeting, so the agent
skips its held camera-gated greeting for voice-triggered arrivals.
"""

from __future__ import annotations

import json
import os

import paho.mqtt.publish as mqtt_publish
from langchain_core.tools import tool

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))


def _publish_presence(event: str) -> bool:
    try:
        mqtt_publish.single(
            "jarvis/presence/home",
            json.dumps({"event": event, "source": "voice"}),
            hostname=MQTT_HOST, port=MQTT_PORT,
        )
        return True
    except Exception:
        return False


@tool
def arrive_home() -> str:
    """Mark the user as home NOW (manual arrival) — runs the arrival scene and
    clears away mode. Use when the user says "I'm home", "I just got back", etc.
    Then give a brief warm welcome yourself.
    """
    if not _publish_presence("arrived"):
        return "Couldn't reach the presence bus."
    return ("Marked you home — arrival scene running (lights come up in the "
            "evening window). Give a short welcome-home now.")


@tool
def leave_home() -> str:
    """Mark the user as away NOW (manual departure) — runs the departure scene
    (lights off) and arms away mode. Use for "I'm heading out", "leaving now".
    """
    if not _publish_presence("left"):
        return "Couldn't reach the presence bus."
    return "Marked you away — lights off and away mode on. Safe travels."
