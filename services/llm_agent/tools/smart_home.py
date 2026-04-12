"""Smart Home tools for JARVIS — interfaces with Home Assistant."""

import os

import aiohttp
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")


def _headers():
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }


@tool
async def control_device(entity_id: str, action: str, params: dict = None) -> str:
    """Control a smart home device (light, switch, fan, etc.).

    Args:
        entity_id: The device entity ID, e.g. 'light.bedroom_govee', 'switch.office_fan'
        action: One of 'turn_on', 'turn_off', 'toggle'
        params: Optional dict with brightness (0-255), rgb_color ([r,g,b]),
                color_temp_kelvin (2000-9000), effect name
    """
    domain = entity_id.split(".")[0]
    payload = {"entity_id": entity_id}
    if params:
        payload.update(params)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HA_URL}/api/services/{domain}/{action}",
            headers=_headers(),
            json=payload,
        ) as resp:
            if resp.status == 200:
                return f"Done. {entity_id} → {action}"
            else:
                error = await resp.text()
                return f"Error controlling {entity_id}: {resp.status} - {error}"


@tool
async def get_device_states(area: str = None) -> str:
    """Get current state of smart home devices, optionally filtered by room/area.

    Args:
        area: Optional room name to filter by, e.g. 'bedroom', 'living room', 'office'
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{HA_URL}/api/states", headers=_headers()) as resp:
            if resp.status != 200:
                return f"Error fetching states: {resp.status}"
            states = await resp.json()
            relevant = []
            for s in states:
                friendly = s.get("attributes", {}).get("friendly_name", "")
                if area and area.lower() not in friendly.lower():
                    continue
                eid = s["entity_id"]
                if eid.startswith(("light.", "switch.", "sensor.", "climate.", "media_player.")):
                    attrs = s.get("attributes", {})
                    info = f"{friendly}: {s['state']}"
                    if "brightness" in attrs:
                        pct = round(attrs["brightness"] / 255 * 100)
                        info += f" ({pct}% brightness)"
                    if "rgb_color" in attrs:
                        info += f" (color: {attrs['rgb_color']})"
                    relevant.append(info)
            return "\n".join(relevant[:30]) or "No devices found."


@tool
async def set_scene(room: str, scene_name: str) -> str:
    """Activate a lighting or environment scene in a room.

    Args:
        room: Room name, e.g. 'bedroom', 'living_room', 'office'
        scene_name: Scene name, e.g. 'movie_mode', 'bedtime', 'energize', 'relax'
    """
    scene_id = f"scene.{room}_{scene_name}".lower().replace(" ", "_")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HA_URL}/api/services/scene/turn_on",
            headers=_headers(),
            json={"entity_id": scene_id},
        ) as resp:
            if resp.status == 200:
                return f"Scene '{scene_name}' activated in {room}."
            else:
                return f"Scene not found: {scene_id}. Available scenes may differ."


@tool
async def get_presence(person: str = None) -> str:
    """Check who is home and optionally which room they are in.

    Args:
        person: Optional person name to check, or leave empty for everyone
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{HA_URL}/api/states", headers=_headers()) as resp:
            if resp.status != 200:
                return f"Error fetching presence: {resp.status}"
            states = await resp.json()
            results = []
            for s in states:
                if s["entity_id"].startswith("person."):
                    name = s["attributes"].get("friendly_name", s["entity_id"])
                    if person and person.lower() not in name.lower():
                        continue
                    results.append(f"{name}: {s['state']}")
            return "\n".join(results) or "No presence data available."
