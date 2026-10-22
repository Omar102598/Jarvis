"""sleep_mode — the one-phrase bedtime routine.

"Hey Jarvis, activate sleep mode" should do four things without further
conversation: cool the house, put the bedroom lamp on something dim, start the
loop track, and make sure the music actually stops later.

Each step reports its own outcome rather than the routine reporting a single
"done". A bedtime routine that claims success while the lamp never came on or
the music runs all night is worse than one that says which part failed --
you are asleep and cannot check.

The music timer is the part worth being careful about. Spotify's Web API has no
sleep timer, so we create one: an in-process task pauses at the deadline, and
the deadline is ALSO written to Redis (jarvis:sleep:stop_at) so a restart of
this container cannot strand the music playing. scripts/watchdog.sh enforces
that key every 5 minutes as the durable backstop; the in-process task is just
the precise one.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import redis
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))

THERMOSTAT = os.environ.get("SLEEP_THERMOSTAT_ENTITY", "climate.thermostat")
BEDROOM_LAMP = os.environ.get("SLEEP_LAMP_ENTITY", "light.bedroom_lamp")
LAMP_EFFECT = os.environ.get("SLEEP_LAMP_EFFECT", "Night Light")
LAMP_BRIGHTNESS = int(os.environ.get("SLEEP_LAMP_BRIGHTNESS", "25"))   # percent
SLEEP_TEMP = int(os.environ.get("SLEEP_TEMP_F", "69"))
SLEEP_MINUTES = int(os.environ.get("SLEEP_MUSIC_MINUTES", "45"))
SLEEP_TRACK = os.environ.get("SLEEP_TRACK", "")        # set this to your loop song
SLEEP_DEVICE = os.environ.get("SLEEP_SPOTIFY_DEVICE", "MacBook")
SLEEP_VOLUME = int(os.environ.get("SLEEP_VOLUME", "25"))
# Spoken aloud, so never read the raw spotify: URI back to the user.
SLEEP_TRACK_NAME = os.environ.get("SLEEP_TRACK_NAME", "") or SLEEP_TRACK

_STOP_KEY = "jarvis:sleep:stop_at"

_r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def _ha_headers() -> dict:
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


async def _set_thermostat(session: aiohttp.ClientSession, temp: int) -> str:
    """Set the target temperature, leaving the hvac mode alone."""
    try:
        async with session.get(f"{HA_URL}/api/states/{THERMOSTAT}",
                               headers=_ha_headers(), timeout=aiohttp.ClientTimeout(total=15)) as chk:
            if chk.status == 404:
                return f"✗ thermostat: no entity {THERMOSTAT}"
        async with session.post(
            f"{HA_URL}/api/services/climate/set_temperature",
            headers=_ha_headers(), json={"entity_id": THERMOSTAT, "temperature": temp},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return f"✗ thermostat: HTTP {resp.status}"
        # Read back rather than trusting the 200 -- a cloud thermostat can
        # accept the call and not apply it.
        await asyncio.sleep(2)
        async with session.get(f"{HA_URL}/api/states/{THERMOSTAT}",
                               headers=_ha_headers(), timeout=aiohttp.ClientTimeout(total=15)) as ver:
            got = (await ver.json()).get("attributes", {}).get("temperature") if ver.status == 200 else None
        if got is not None and abs(float(got) - temp) < 0.6:
            return f"✓ thermostat → {temp}°F"
        return f"⚠ thermostat: asked for {temp}°F, it reports {got}"
    except Exception as exc:
        return f"✗ thermostat: {type(exc).__name__}"


async def _set_lamp(session: aiohttp.ClientSession) -> str:
    """Bedroom lamp on, dim, with a soft effect."""
    try:
        async with session.get(f"{HA_URL}/api/states/{BEDROOM_LAMP}",
                               headers=_ha_headers(), timeout=aiohttp.ClientTimeout(total=15)) as chk:
            if chk.status == 404:
                return f"✗ lamp: no entity {BEDROOM_LAMP}"
            state = (await chk.json()).get("state")
        # An unavailable Govee lamp accepts turn_on and does nothing, so say so
        # instead of reporting a light that is still dark.
        if state == "unavailable":
            return ("✗ lamp: unavailable (Govee cloud offline, or the lamp has no "
                    "power — nothing was sent)")

        # Power on plain first: Govee will not power on AND apply an effect in
        # one call, the same trap smart_home.control_device documents.
        async with session.post(f"{HA_URL}/api/services/light/turn_on",
                                headers=_ha_headers(), json={"entity_id": BEDROOM_LAMP},
                                timeout=aiohttp.ClientTimeout(total=20)) as _:
            pass
        await asyncio.sleep(1.5)
        payload = {"entity_id": BEDROOM_LAMP,
                   "brightness": max(1, min(255, round(LAMP_BRIGHTNESS / 100 * 255)))}
        if LAMP_EFFECT:
            payload["effect"] = LAMP_EFFECT
        async with session.post(f"{HA_URL}/api/services/light/turn_on",
                                headers=_ha_headers(), json=payload,
                                timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return f"✗ lamp: HTTP {resp.status}"
        await asyncio.sleep(2)
        async with session.get(f"{HA_URL}/api/states/{BEDROOM_LAMP}",
                               headers=_ha_headers(), timeout=aiohttp.ClientTimeout(total=15)) as ver:
            now = (await ver.json()).get("state") if ver.status == 200 else "?"
        if now == "on":
            return f"✓ lamp → on, {LAMP_BRIGHTNESS}%, '{LAMP_EFFECT}'"
        return f"⚠ lamp: sent, but it reports '{now}'"
    except Exception as exc:
        return f"✗ lamp: {type(exc).__name__}"


async def _start_music(minutes: int) -> str:
    """Start the loop track on the Mac, set repeat, and arm the stop timer."""
    from .spotify import spotify_control  # reuse the configured client

    call = getattr(spotify_control, "func", None) or getattr(
        spotify_control, "coroutine", None) or spotify_control

    if not SLEEP_TRACK.strip():
        return ("✗ music: no SLEEP_TRACK configured — set SLEEP_TRACK in .env to "
                "the song you want looped")

    out = []
    if SLEEP_TRACK.strip().startswith("spotify:"):
        # Play the exact URI rather than searching. "twilight zone" returns two
        # near-identical Ariana Grande entries from different album editions, so
        # a search would pick whichever ranked first that night -- fine most of
        # the time, wrong silently the rest. A pinned URI cannot drift.
        r1 = await _play_uri(SLEEP_TRACK.strip())
    else:
        r1 = await call(action="search_play", query=SLEEP_TRACK, device=SLEEP_DEVICE)
    out.append(str(r1))
    if str(r1).startswith("✗") or "couldn't" in str(r1).lower() \
            or "no spotify device" in str(r1).lower():
        return "✗ music: " + str(r1)

    await asyncio.sleep(2)
    await call(action="volume", query=str(SLEEP_VOLUME), device=SLEEP_DEVICE)
    r2 = await call(action="repeat", query="track", device=SLEEP_DEVICE)
    out.append(str(r2))

    _arm_timer(minutes)
    return (f"✓ music → '{SLEEP_TRACK_NAME}' looping at {SLEEP_VOLUME}%, "
            f"stops in {minutes} min")


async def _play_uri(uri: str) -> str:
    """Start an exact track/playlist URI on the sleep device."""
    from .spotify import (_API, _access_token, _get_devices, _match_device,
                          _set_media_flag)
    async with aiohttp.ClientSession() as session:
        token = await _access_token(session)
        if not token:
            return "✗ Spotify auth failed"
        headers = {"Authorization": f"Bearer {token}"}
        devices = await _get_devices(session, headers)
        target = _match_device(devices, SLEEP_DEVICE) if SLEEP_DEVICE else None
        if SLEEP_DEVICE and target is None:
            names = ", ".join(d.get("name", "?") for d in devices) or "none"
            return (f"✗ no Spotify device matching '{SLEEP_DEVICE}' "
                    f"(available: {names}) — open Spotify on the Mac first")
        params = {"device_id": target["id"]} if target else {}
        body = ({"context_uri": uri} if ":playlist:" in uri or ":album:" in uri
                else {"uris": [uri]})
        async with session.put(f"{_API}/me/player/play", headers=headers,
                               params=params, json=body,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status in (200, 204):
                _set_media_flag(True)
                where = f" on {target['name']}" if target else ""
                return f"started{where}"
            return f"✗ play failed (HTTP {resp.status})"


def _arm_timer(minutes: int) -> None:
    """Record the deadline, then schedule the precise in-process stop.

    The Redis key is the durable half: watchdog.sh pauses Spotify once the
    deadline passes, so a container restart cannot leave music playing all
    night. The asyncio task is the accurate half -- it fires on the minute
    rather than at the next watchdog tick.
    """
    deadline = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    try:
        _r.set(_STOP_KEY, deadline.isoformat(), ex=minutes * 60 + 3600)
    except Exception:
        pass   # the asyncio task below still covers the normal case

    async def _stop_later() -> None:
        try:
            await asyncio.sleep(minutes * 60)
            # Re-check the key: if the user cancelled sleep mode, it is gone and
            # we must not pause music they deliberately restarted.
            try:
                if not _r.get(_STOP_KEY):
                    return
            except Exception:
                pass
            from .spotify import spotify_control
            call = getattr(spotify_control, "func", None) or getattr(
                spotify_control, "coroutine", None) or spotify_control
            await call(action="pause")
            try:
                _r.delete(_STOP_KEY)
            except Exception:
                pass
        except asyncio.CancelledError:
            pass

    try:
        asyncio.get_running_loop().create_task(_stop_later())
    except RuntimeError:
        pass   # no loop (sync caller) -- watchdog remains the backstop


@tool
async def sleep_mode(action: str = "on", minutes: int = 0) -> str:
    """Run the bedtime routine: cool the house, dim the bedroom lamp, loop the
    sleep track, and stop the music automatically.

    Use this for "activate sleep mode", "sleep mode", "bedtime", "goodnight",
    "I'm going to bed".

    Args:
        action: 'on' to start the routine, 'off' to cancel it (stops the music
            now and clears the pending timer), or 'status' to report whether a
            sleep timer is armed and how long is left.
        minutes: Optional override for how long the music plays. 0 uses the
            configured default (SLEEP_MUSIC_MINUTES, currently 45).
    """
    action = (action or "on").strip().lower()

    if action == "status":
        try:
            raw = _r.get(_STOP_KEY)
        except Exception:
            return "Couldn't reach Redis to check the sleep timer."
        if not raw:
            return "No sleep timer is armed."
        try:
            left = (datetime.fromisoformat(raw) - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            return "A sleep timer is armed, but its deadline is unreadable."
        return (f"Music stops in {int(max(0, left) // 60)} min."
                if left > 0 else "The sleep timer has already elapsed.")

    if action in ("off", "cancel", "stop"):
        try:
            _r.delete(_STOP_KEY)
        except Exception:
            pass
        from .spotify import spotify_control
        call = getattr(spotify_control, "func", None) or getattr(
            spotify_control, "coroutine", None) or spotify_control
        res = await call(action="pause")
        return f"Sleep mode off. {res}"

    mins = minutes if minutes and minutes > 0 else SLEEP_MINUTES
    async with aiohttp.ClientSession() as session:
        thermo, lamp = await asyncio.gather(
            _set_thermostat(session, SLEEP_TEMP),
            _set_lamp(session),
        )
    music = await _start_music(mins)

    lines = [thermo, lamp, music]
    failed = [l for l in lines if l.startswith("✗")]
    header = ("Sleep mode set, sir." if not failed
              else f"Sleep mode partially set — {len(failed)} step(s) failed.")
    return header + "\n" + "\n".join("  " + l for l in lines)
