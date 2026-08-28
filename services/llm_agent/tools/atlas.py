"""get_health_overview — Atlas's fused weekly health picture, on demand.

Reads the summary Atlas (agent_runner) computes weekly; if it's stale or
missing, triggers a fresh run and says so rather than blocking the
conversation (Atlas takes ~10s; the user can ask again or Atlas's report
lands as an agent completion).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import paho.mqtt.publish as mqtt_publish
import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
STALE_HOURS = 30

_r = redis.Redis(host=REDIS_HOST, decode_responses=True)


@tool
def get_health_overview() -> str:
    """The user's fused health picture: sleep/steps/HRV/resting-HR trends
    (this week vs last), training days, nutrition adherence, readiness —
    with Atlas's synthesis. Use for "how's my health trending?", "how am I
    recovering?", "give me my health overview".
    """
    raw = _r.get("health:atlas:summary")
    if raw:
        try:
            data = json.loads(raw)
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(data["ts"])).total_seconds() / 3600
            if age_h <= STALE_HOURS:
                metrics = data.get("metrics", {})
                nums = ", ".join(f"{k}={v}" for k, v in metrics.items()
                                 if v is not None)
                return (f"{data.get('summary','')}\n\n"
                        f"(Underlying numbers: {nums})")
        except Exception:
            pass
    try:
        mqtt_publish.single("jarvis/agents/atlas/trigger", "{}",
                            hostname=MQTT_HOST, port=MQTT_PORT)
        return ("Atlas's overview is stale — I've asked him to recompute "
                "(takes ~10 seconds). Ask again in a moment, or his report "
                "will come through as an agent completion.")
    except Exception:
        return "No health overview available and I couldn't reach Atlas."
