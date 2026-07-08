"""Smart Home tools for JARVIS — interfaces with Home Assistant."""

import asyncio
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
        # Verify the entity EXISTS first — HA's turn_on returns 200 for unknown
        # entities, so without this the agent guesses 'light.living_room'
        # (nonexistent) and falsely reports success. Fail loudly with the real
        # options so the agent self-corrects.
        async with session.get(f"{HA_URL}/api/states/{entity_id}",
                               headers=_headers()) as chk:
            if chk.status == 404:
                async with session.get(f"{HA_URL}/api/states",
                                       headers=_headers()) as allst:
                    names = []
                    if allst.status == 200:
                        names = [s["entity_id"] for s in await allst.json()
                                 if s["entity_id"].startswith(domain + ".")]
                    return (f"No such entity '{entity_id}'. Available {domain} "
                            f"entities: {', '.join(names) or 'none'}. "
                            "Use one of these exact ids.")

        # Govee (and some cloud lights) won't power on AND apply an effect/
        # color in one call. When turning on WITH a vibe (effect/rgb/color/
        # brightness), power on plain first, then apply the vibe.
        vibe_params = bool(params) and action == "turn_on" and any(
            k in params for k in ("effect", "rgb_color", "color_temp_kelvin",
                                   "color_temp", "brightness", "hs_color", "xy_color"))
        if vibe_params:
            async with session.post(f"{HA_URL}/api/services/{domain}/turn_on",
                                    headers=_headers(),
                                    json={"entity_id": entity_id}) as _:
                pass
            await asyncio.sleep(1.5)

        async with session.post(
            f"{HA_URL}/api/services/{domain}/{action}",
            headers=_headers(),
            json=payload,
        ) as resp:
            if resp.status != 200:
                error = await resp.text()
                return f"Error controlling {entity_id}: {resp.status} - {error}"

        # Confirm the state changed — but tolerate cloud lag (Govee updates its
        # reported state a few seconds late). Retry a couple times before
        # concluding it failed; the 404 check above already prevents the main
        # "reported success on a nonexistent entity" failure.
        if action in ("turn_on", "turn_off"):
            want = "on" if action == "turn_on" else "off"
            for _ in range(3):
                await asyncio.sleep(2)
                async with session.get(f"{HA_URL}/api/states/{entity_id}",
                                       headers=_headers()) as ver:
                    if ver.status == 200 and (await ver.json()).get("state") == want:
                        break
            else:
                return (f"Sent {action}"
                        + (f" + {list(params.keys())}" if params else "")
                        + f" to {entity_id}. It's slow to confirm — likely just "
                        "Govee cloud lag; check the light.")
        extra = f" ({', '.join(params.keys())})" if params else ""
        return f"Done. {entity_id} → {action}{extra}"


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
                # Skip the per-segment sub-entities (light.x_segment_N) — noise.
                if "_segment_" in eid:
                    continue
                if eid.startswith(("light.", "switch.", "sensor.", "climate.", "media_player.")):
                    attrs = s.get("attributes", {})
                    info = f"{eid} ({friendly}): {s['state']}"
                    bri = attrs.get("brightness")
                    if bri is not None:
                        info += f" ({round(bri / 255 * 100)}% brightness)"
                    if attrs.get("rgb_color"):
                        info += f" rgb={attrs['rgb_color']}"
                    # Expose available EFFECTS so the agent can set vibes on the
                    # fly ("dance party", "sunset") via control_device(effect=...).
                    effects = attrs.get("effect_list")
                    if effects:
                        info += f" | effects: {', '.join(effects[:60])}"
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
