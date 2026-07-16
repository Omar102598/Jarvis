"""apple_tv — control Apple TVs (power, apps, playback, navigation) via Home
Assistant's apple_tv integration (pyatv).

Each paired Apple TV appears in HA as a media_player.* (power, play/pause,
app launching via select_source, now-playing metadata) plus a remote.* entity
(directional/menu/home commands). With HDMI-CEC, powering the Apple TV on/off
also drives the physical TV.

One-time setup per Apple TV (no code): HA usually auto-discovers them on the
LAN — Settings → Devices & Services → Add Integration → "Apple TV" → a PIN
appears on the TV screen → enter it. The tool reports this if nothing is paired.
"""

from __future__ import annotations

import os

import aiohttp
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")

_REMOTE_COMMANDS = {"up", "down", "left", "right", "select", "menu", "home",
                    "play", "pause", "skip_forward", "skip_backward"}

_PAIR_HELP = (
    "No Apple TVs are paired in Home Assistant yet. One-time setup: open HA "
    "(port 8123) → Settings → Devices & Services → Add Integration → 'Apple TV' "
    "(they're usually auto-discovered) → enter the PIN shown on the TV screen. "
    "Then each Apple TV appears here by room name."
)


def _headers():
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


async def _get_states(session) -> list[dict]:
    async with session.get(f"{HA_URL}/api/states", headers=_headers()) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HA states failed ({resp.status})")
        return await resp.json()


def _apple_tvs(states: list[dict]) -> list[dict]:
    """media_player entities that look like Apple TVs (have an app source list
    or 'apple' in the name/id). Falls back to all media_players if none match."""
    players = [s for s in states if s["entity_id"].startswith("media_player.")]
    atvs = [s for s in players
            if "apple" in s["entity_id"].lower()
            or "apple" in (s["attributes"].get("friendly_name") or "").lower()
            or s["attributes"].get("source_list")]
    return atvs or players


def _match(entities: list[dict], name: str) -> dict | None:
    if not entities:
        return None
    if not name.strip():
        return entities[0] if len(entities) == 1 else None
    n = name.lower().strip()
    for e in entities:
        friendly = (e["attributes"].get("friendly_name") or "").lower()
        if n == friendly or n in friendly or n in e["entity_id"].lower():
            return e
    words = set(n.split())
    for e in entities:
        friendly = (e["attributes"].get("friendly_name") or "").lower()
        if words & set(friendly.split()):
            return e
    return None


async def _call(session, domain: str, service: str, payload: dict) -> bool:
    async with session.post(f"{HA_URL}/api/services/{domain}/{service}",
                            headers=_headers(), json=payload) as resp:
        return resp.status == 200


async def _get_state(session, entity_id: str) -> dict | None:
    async with session.get(f"{HA_URL}/api/states/{entity_id}",
                           headers=_headers()) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def _refresh(session, entity_id: str) -> None:
    """Ask HA to refresh the entity — push updates from the Apple TV can lag
    (state showed the previous app minutes after a launch), so poll on demand."""
    await _call(session, "homeassistant", "update_entity", {"entity_id": entity_id})


async def _wake_and_wait(session, eid: str, remote_id: str, max_wait: float = 15.0) -> dict | None:
    """Wake the Apple TV and poll until it reports non-off (fresh state dict).

    While asleep the media_player exposes NO source_list and select_source
    no-ops, so launching an app must wait for the wake to actually land.
    """
    import asyncio as _asyncio
    tv = await _get_state(session, eid)
    if tv and tv.get("state") not in ("off", "unavailable", "standby", None):
        return tv
    # Fire every wake path we have — different tvOS versions honour different ones.
    await _call(session, "remote", "turn_on", {"entity_id": remote_id})
    await _call(session, "media_player", "turn_on", {"entity_id": eid})
    waited = 0.0
    while waited < max_wait:
        await _asyncio.sleep(2.0)
        waited += 2.0
        await _refresh(session, eid)
        tv = await _get_state(session, eid)
        if tv and tv.get("state") not in ("off", "unavailable", "standby", None):
            return tv
    return tv


@tool
async def apple_tv(action: str, name: str = "", app: str = "", command: str = "") -> str:
    """Control an Apple TV: power, launch apps, play/pause, navigate, now-playing.

    Args:
        action: 'list' (show Apple TVs + their apps), 'on', 'off', 'open'
            (launch an app — set app), 'play_pause', 'status' (what's playing),
            or 'remote' (send a navigation command — set command).
        name: Which Apple TV, by room/name ('living room', 'bedroom'). Optional
            when there's only one.
        app: For 'open' — the app to launch ('Netflix', 'YouTube'). Fuzzy-matched
            against the TV's installed apps.
        command: For 'remote' — one of up, down, left, right, select, menu, home,
            play, pause, skip_forward, skip_backward.
    """
    if not HA_TOKEN:
        return "Home Assistant isn't configured (no HA_TOKEN) — Apple TV control needs it."
    action = (action or "").strip().lower()

    try:
        async with aiohttp.ClientSession() as session:
            states = await _get_states(session)
            tvs = _apple_tvs(states)
            if not tvs:
                return _PAIR_HELP

            if action == "list":
                lines = []
                for tv in tvs:
                    friendly = tv["attributes"].get("friendly_name", tv["entity_id"])
                    state = tv.get("state", "?")
                    apps = tv["attributes"].get("source_list") or []
                    line = f"• {friendly} — {state}"
                    if tv["attributes"].get("app_name"):
                        line += f" ({tv['attributes']['app_name']})"
                    if apps:
                        line += f" | apps: {', '.join(apps[:15])}"
                    lines.append(line)
                return "Apple TVs:\n" + "\n".join(lines)

            tv = _match(tvs, name)
            if tv is None:
                names = ", ".join(t["attributes"].get("friendly_name", t["entity_id"])
                                  for t in tvs)
                return f"Which one? Available: {names}."
            eid = tv["entity_id"]
            friendly = tv["attributes"].get("friendly_name", eid)
            # The paired remote entity shares the object id (media_player.living_room
            # → remote.living_room) — used for power + navigation.
            remote_id = "remote." + eid.split(".", 1)[1]

            if action == "on":
                fresh = await _wake_and_wait(session, eid, remote_id)
                st = (fresh or {}).get("state")
                if st and st not in ("off", "unavailable"):
                    return f"{friendly} is awake ({st})."
                return (f"Sent the wake to {friendly}, but it still reports "
                        f"{st or 'unknown'} — it may need a tap on its physical remote.")
            if action == "off":
                ok = await _call(session, "remote", "turn_off", {"entity_id": remote_id})
                if not ok:
                    ok = await _call(session, "media_player", "turn_off", {"entity_id": eid})
                return (f"{friendly} turned off." if ok
                        else f"Couldn't turn off {friendly}.")

            if action == "open":
                if not app.strip():
                    return "Which app should I open?"
                # Wake first and WAIT until it's actually awake — while asleep
                # the app list is empty and select_source silently no-ops (the
                # old fire-and-forget ordering launched into a sleeping device).
                fresh = await _wake_and_wait(session, eid, remote_id)
                if not fresh or fresh.get("state") in ("off", "unavailable", None):
                    return (f"{friendly} didn't wake up — try tapping its remote "
                            "once, then ask me again.")
                sources = fresh["attributes"].get("source_list") or []
                choice = next((s for s in sources if s.lower() == app.lower().strip()),
                              None) or next((s for s in sources
                                             if app.lower().strip() in s.lower()), None)
                if sources and not choice:
                    return (f"'{app}' isn't in {friendly}'s app list. "
                            f"Apps: {', '.join(sources[:20])}")
                ok = await _call(session, "media_player", "select_source",
                                 {"entity_id": eid, "source": choice or app.strip()})
                if not ok:
                    return f"Couldn't open {app} on {friendly}."
                # Confirm the switch (state can lag — refresh and check briefly).
                import asyncio as _asyncio
                for _ in range(3):
                    await _asyncio.sleep(2.0)
                    await _refresh(session, eid)
                    now = await _get_state(session, eid)
                    app_now = (now or {}).get("attributes", {}).get("app_name") or ""
                    if choice and app_now.lower() == choice.lower():
                        return f"{choice} is open on {friendly}."
                return (f"Opening {choice or app} on {friendly} — sent. (The TV may "
                        "take a moment to reflect it.)")

            if action == "play_pause":
                ok = await _call(session, "media_player", "media_play_pause",
                                 {"entity_id": eid})
                return (f"Toggled play/pause on {friendly}." if ok
                        else f"Couldn't control {friendly}.")

            if action == "status":
                # Push updates from the ATV can lag (stale app_name observed) —
                # force a refresh before reading.
                await _refresh(session, eid)
                import asyncio as _asyncio
                await _asyncio.sleep(1.0)
                fresh = await _get_state(session, eid) or tv
                a = fresh["attributes"]
                bits = [f"{friendly}: {fresh.get('state', 'unknown')}"]
                if a.get("app_name"):
                    bits.append(f"app {a['app_name']}")
                if a.get("media_title"):
                    title = a["media_title"]
                    if a.get("media_series_title"):
                        title = f"{a['media_series_title']} — {title}"
                    bits.append(f"playing “{title}”")
                if fresh.get("state") in ("paused", "idle") and a.get("app_name"):
                    bits.append("(note: this can reflect the last active playback)")
                return ", ".join(bits) + "."

            if action == "remote":
                cmd = command.strip().lower()
                if cmd not in _REMOTE_COMMANDS:
                    return f"Command must be one of: {', '.join(sorted(_REMOTE_COMMANDS))}."
                ok = await _call(session, "remote", "send_command",
                                 {"entity_id": remote_id, "command": cmd})
                return (f"Sent '{cmd}' to {friendly}." if ok
                        else f"Couldn't send '{cmd}' to {friendly}.")

            return "Unknown action. Use list | on | off | open | play_pause | status | remote."
    except Exception as exc:
        return f"Apple TV control failed: {exc}"
