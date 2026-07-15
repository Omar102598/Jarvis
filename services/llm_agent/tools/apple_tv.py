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

            if action in ("on", "off"):
                svc = "turn_on" if action == "on" else "turn_off"
                ok = await _call(session, "remote", svc, {"entity_id": remote_id})
                if not ok:  # fall back to the media_player service
                    ok = await _call(session, "media_player", svc, {"entity_id": eid})
                return (f"{friendly} turned {action}." if ok
                        else f"Couldn't turn {action} {friendly}.")

            if action == "open":
                if not app.strip():
                    return "Which app should I open?"
                sources = tv["attributes"].get("source_list") or []
                choice = next((s for s in sources if s.lower() == app.lower().strip()),
                              None) or next((s for s in sources
                                             if app.lower().strip() in s.lower()), None)
                if sources and not choice:
                    return (f"'{app}' isn't in {friendly}'s app list. "
                            f"Apps: {', '.join(sources[:20])}")
                # Wake it first so the launch isn't swallowed by sleep.
                await _call(session, "remote", "turn_on", {"entity_id": remote_id})
                ok = await _call(session, "media_player", "select_source",
                                 {"entity_id": eid, "source": choice or app.strip()})
                return (f"Opening {choice or app} on {friendly}." if ok
                        else f"Couldn't open {app} on {friendly}.")

            if action == "play_pause":
                ok = await _call(session, "media_player", "media_play_pause",
                                 {"entity_id": eid})
                return (f"Toggled play/pause on {friendly}." if ok
                        else f"Couldn't control {friendly}.")

            if action == "status":
                a = tv["attributes"]
                bits = [f"{friendly}: {tv.get('state', 'unknown')}"]
                if a.get("app_name"):
                    bits.append(f"app {a['app_name']}")
                if a.get("media_title"):
                    title = a["media_title"]
                    if a.get("media_series_title"):
                        title = f"{a['media_series_title']} — {title}"
                    bits.append(f"playing “{title}”")
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
