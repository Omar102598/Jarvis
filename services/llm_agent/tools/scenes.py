"""manage_scenes — save the current lighting as a named scene, replay on demand.

"Save this as movie night" captures the live state of every light (on/off,
brightness, colour/temperature, effect) into Redis; "set movie night" replays
it. A natural-language, user-defined layer on top of Home Assistant — no HA
scene editor required.

Redis:
    jarvis:scenes            set of saved scene names
    jarvis:scene:{name}      json list of per-light captured state
"""

from __future__ import annotations

import json
import os

import aiohttp
import redis
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)


def _headers():
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def _capture(state: dict) -> dict:
    attrs = state.get("attributes", {}) or {}
    snap = {"entity_id": state["entity_id"], "state": state.get("state", "off")}
    for k in ("brightness", "rgb_color", "color_temp_kelvin", "effect"):
        if attrs.get(k) is not None:
            snap[k] = attrs[k]
    return snap


@tool
async def manage_scenes(action: str, name: str = "") -> str:
    """Save or recall a lighting scene by name.

    Actions:
      • "save"   — capture the current state of all lights as scene <name>.
      • "apply"  — replay a saved scene (turn lights on/off to match).
      • "list"   — list saved scenes.
      • "delete" — remove a saved scene.

    Args:
        action: save | apply | list | delete.
        name: the scene name (e.g. "movie night", "dance party", "focus").
    """
    if not HA_TOKEN:
        return "Home Assistant isn't configured (no HA_TOKEN), so scenes are unavailable."
    action = (action or "").strip().lower()
    key = name.strip().lower()

    if action == "list":
        names = sorted(_r.smembers("jarvis:scenes"))
        return "Saved scenes: " + (", ".join(names) if names else "none yet.")

    if action == "delete":
        if not key:
            return "Which scene should I delete?"
        _r.srem("jarvis:scenes", key)
        _r.delete(f"jarvis:scene:{key}")
        return f"Deleted scene “{key}”."

    if action == "save":
        if not key:
            return "What should I name this scene?"
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{HA_URL}/api/states", headers=_headers()) as resp:
                if resp.status != 200:
                    return f"Couldn't read device states ({resp.status})."
                states = await resp.json()
        lights = [_capture(st) for st in states if st["entity_id"].startswith("light.")]
        if not lights:
            return "No lights found to capture."
        _r.set(f"jarvis:scene:{key}", json.dumps(lights))
        _r.sadd("jarvis:scenes", key)
        on = sum(1 for l in lights if l["state"] == "on")
        return f"Saved “{key}” — {len(lights)} lights ({on} on)."

    if action == "apply":
        if not key:
            return "Which scene should I set?"
        raw = _r.get(f"jarvis:scene:{key}")
        if not raw:
            names = sorted(_r.smembers("jarvis:scenes"))
            return (f"No scene named “{key}”. Saved: "
                    + (", ".join(names) if names else "none yet."))
        lights = json.loads(raw)
        applied = 0
        async with aiohttp.ClientSession() as s:
            for l in lights:
                eid = l["entity_id"]
                try:
                    if l.get("state") != "on":
                        async with s.post(f"{HA_URL}/api/services/light/turn_off",
                                          headers=_headers(),
                                          json={"entity_id": eid}) as _:
                            pass
                        applied += 1
                        continue
                    # Power on plain first (cloud lights can't power+apply at once),
                    # then apply the captured vibe.
                    async with s.post(f"{HA_URL}/api/services/light/turn_on",
                                      headers=_headers(),
                                      json={"entity_id": eid}) as _:
                        pass
                    payload = {"entity_id": eid}
                    for k in ("brightness", "rgb_color", "color_temp_kelvin", "effect"):
                        if l.get(k) is not None:
                            payload[k] = l[k]
                    async with s.post(f"{HA_URL}/api/services/light/turn_on",
                                      headers=_headers(), json=payload) as _:
                        pass
                    applied += 1
                except Exception:
                    continue
        return f"Set the scene “{key}” — adjusted {applied} lights."

    return "Unknown action. Use save | apply | list | delete."
