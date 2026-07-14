"""Synapse — JARVIS's always-on nervous system.

One lightweight bus consumer that does three foundational jobs the rest of the
intelligence layer builds on:

  1. Event store   — mirror meaningful bus traffic into a durable, replayable
                     Redis Stream (event_store.py). Chronicle and the "what did
                     I miss" flows read from it.
  2. Notify router — funnel jarvis/notify into either an immediate surface push
                     (urgent) or a batched digest card (notify_router.py).
  3. Correlation   — periodically join signals across agents and surface
                     cross-domain insights (rules.py).

It holds no state of its own beyond Redis, makes no LLM calls on the hot path,
and is a near-no-op when the house is quiet — so it's cheap to run 24/7.
"""

import os
import threading
import time

import paho.mqtt.client as mqtt
import redis

import event_store
import notify_router
import rules

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
RULE_INTERVAL_S = int(os.environ.get("SYNAPSE_RULE_INTERVAL_S", "300"))  # 5 min

_redis = redis.Redis(host=REDIS_HOST, decode_responses=False)  # raw bytes for the store
_redis_txt = redis.Redis(host=REDIS_HOST, decode_responses=True)  # text ops (rules/router)
_mqtt = mqtt.Client()


# ---------------------------------------------------------------------------
# Bus ingestion
# ---------------------------------------------------------------------------


def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe("jarvis/#")
        client.subscribe("ring/#")
        client.subscribe("homeassistant/#")
        print("[Synapse] MQTT connected — mirroring jarvis/#, ring/#, homeassistant/#")
    else:
        print(f"[Synapse] MQTT connect failed (rc={rc})")


def _on_message(client, userdata, msg):
    # Mirror every meaningful event into the durable stream (filters binary/noise).
    event_store.record(_redis, msg.topic, msg.payload)


def _on_notify(client, userdata, msg):
    # jarvis/notify → route (urgent passes through, rest batches into the digest).
    notify_router.handle(_redis_txt, client, msg.payload)


def _on_flush(client, userdata, msg):
    # jarvis/notify/flush → force the digest out now (used by the morning brief).
    notify_router.flush(_redis_txt, client, force=True)
    _redis_txt.set("jarvis:digest:last_flush", str(time.time()))


# ---------------------------------------------------------------------------
# Correlation + digest timer (background thread, cheap)
# ---------------------------------------------------------------------------


def _timer_loop():
    """Every RULE_INTERVAL_S: run correlation rules and check the digest timer."""
    while True:
        time.sleep(RULE_INTERVAL_S)
        try:
            for insight in rules.evaluate_all(_redis_txt):
                _mqtt.publish("jarvis/notify", _json({
                    "title": insight.title,
                    "text": insight.text,
                    "urgency": insight.urgency,
                    "source": "Synapse",
                    "dedup_key": insight.dedup_key,
                }))
        except Exception as exc:
            print(f"[Synapse] rule pass error: {exc}")
        try:
            notify_router.maybe_flush_on_timer(_redis_txt, _mqtt)
        except Exception as exc:
            print(f"[Synapse] digest flush error: {exc}")


def _json(d) -> str:
    import json
    return json.dumps(d)


def main():
    _mqtt.on_connect = _on_connect
    _mqtt.on_message = _on_message
    # Specific handlers take precedence over the catch-all _on_message.
    _mqtt.message_callback_add("jarvis/notify", _on_notify)
    _mqtt.message_callback_add("jarvis/notify/flush", _on_flush)
    _mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    threading.Thread(target=_timer_loop, daemon=True, name="synapse-timer").start()
    print("[Synapse] online — event store + notify router + correlation engine")
    _mqtt.loop_forever()


if __name__ == "__main__":
    main()
