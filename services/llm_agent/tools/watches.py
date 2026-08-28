"""manage_watches — add/list/remove Scout web-monitor watches at runtime.

Scout (web_monitor) watches specific pages for NEW items matching a goal
(apartments, restocks, ticket drops, price changes, job postings…). Watches used
to live only in config/agents.yml (static, needs a restart). This lets you manage
them live — "watch this URL for concert tickets", "what am I watching?", "stop
watching the Luca page" — by storing dynamic watches in Redis (scout:watches),
which Scout merges with the config ones each run.
"""

from __future__ import annotations

import hashlib
import json
import os

import paho.mqtt.publish as mqtt_publish
import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)
_KEY = "scout:watches"


def _load() -> list:
    try:
        return json.loads(_r.get(_KEY) or "[]")
    except Exception:
        return []


@tool
def manage_watches(action: str, url: str = "", goal: str = "", name: str = "",
                   keywords: str = "") -> str:
    """Manage what Scout watches on the web for new items.

    Actions:
      • "add"    — watch a URL. Give the url and a goal (what counts as a new
        item to alert on, e.g. "available tickets", "in-stock restocks",
        "price drops"). Optional name and comma-separated priority keywords.
      • "list"   — show the watches you've added.
      • "remove" — stop watching (match by url or name).

    Args:
        action: add | list | remove.
        url: the page to watch.
        goal: what to extract / alert on (for add).
        name: a friendly label.
        keywords: comma-separated terms that flag a match as high-priority.
    """
    action = (action or "").strip().lower()

    if action == "list":
        watches = _load()
        if not watches:
            return "No dynamic watches. (Config watches in agents.yml still run.)"
        return "Watching:\n" + "\n".join(
            f"• {w.get('name') or w.get('url')} — {w.get('goal','new items')}"
            for w in watches)

    if action == "remove":
        key = (url or name).strip().lower()
        if not key:
            return "Which watch? Give me its URL or name."
        watches = _load()
        kept = [w for w in watches
                if key not in w.get("url", "").lower() and key not in w.get("name", "").lower()]
        if len(kept) == len(watches):
            return f"No watch matching “{key}”."
        _r.set(_KEY, json.dumps(kept))
        return f"Stopped watching “{key}” ({len(watches) - len(kept)} removed)."

    if action == "add":
        if not url.strip() or not goal.strip():
            return "I need a URL and a goal (what should count as a new item to alert on)."
        watches = _load()
        if any(w.get("url") == url.strip() for w in watches):
            return "I'm already watching that URL."
        watch = {
            "name": name.strip() or url.strip()[:60],
            "url": url.strip(),
            "goal": goal.strip(),
            "noun": "item",
            "alert_title": f"🔔 Scout — {name.strip() or 'new match'}",
        }
        kws = [k.strip() for k in keywords.split(",") if k.strip()]
        if kws:
            watch["priority_keywords"] = kws
        watches.append(watch)
        _r.set(_KEY, json.dumps(watches))
        # Kick a run now so the baseline is recorded (won't alert on first scan).
        try:
            mqtt_publish.single("jarvis/agents/web_monitor/trigger", json.dumps({}),
                                hostname=MQTT_HOST, port=MQTT_PORT)
        except Exception:
            pass
        return (f"On it — Scout is now watching {watch['name']} for {watch['goal']}. "
                "I'll record the current state and alert you when something new appears.")

    return "Unknown action. Use add | list | remove."
