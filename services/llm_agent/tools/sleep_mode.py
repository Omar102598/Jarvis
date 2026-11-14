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
from typing import Optional

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
# Govee's own "Sleeping" timer lives only in the Govee app (it isn't one of the
# lamp's effects in HA), so Jarvis does the same fade itself: this colour at
# SLEEP_LAMP_FADE_START %, dimming each minute to off over FADE_MINUTES.
# FADE_MINUTES=0 goes back to the static SLEEP_LAMP_EFFECT.
LAMP_FADE_MIN = int(os.environ.get("SLEEP_LAMP_FADE_MINUTES", "20"))
LAMP_FADE_START = int(os.environ.get("SLEEP_LAMP_FADE_START", "30"))   # percent
LAMP_RGB = [int(x) for x in os.environ.get("SLEEP_LAMP_RGB", "45,115,185").split(",")]
SLEEP_TEMP = int(os.environ.get("SLEEP_TEMP_F", "69"))
SLEEP_MINUTES = int(os.environ.get("SLEEP_MUSIC_MINUTES", "45"))
SLEEP_TRACK = os.environ.get("SLEEP_TRACK", "")        # set this to your loop song
SLEEP_DEVICE = os.environ.get("SLEEP_SPOTIFY_DEVICE", "MacBook")
SLEEP_VOLUME = int(os.environ.get("SLEEP_VOLUME", "25"))
# The bedroom Apple TV. Sleep mode wakes it and opens Spotify on it so the
# music plays there; "" turns that off and plays on SLEEP_SPOTIFY_DEVICE.
TV_ENTITY = os.environ.get("SLEEP_TV_ENTITY", "media_player.bedroom_bedroom").strip()
TV_SPOTIFY_NAME = os.environ.get("SLEEP_TV_SPOTIFY_DEVICE", "").strip()
# Spoken aloud, so never read the raw spotify: URI back to the user.
SLEEP_TRACK_NAME = os.environ.get("SLEEP_TRACK_NAME", "") or SLEEP_TRACK

_STOP_KEY = "jarvis:sleep:stop_at"
_FADE_KEY = "jarvis:sleep:fade"   # present while the lamp fade runs; deleting it stops the fade
_TV_KEY = "jarvis:sleep:tv"      # set while the timer should also turn the TV off

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
        if LAMP_FADE_MIN > 0:
            return await _start_fade(session)
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


def _pct(p: float) -> int:
    return max(1, min(255, round(p / 100 * 255)))


async def _start_fade(session: aiohttp.ClientSession) -> str:
    """Set the sleep colour, then dim to off in the background."""
    async with session.post(f"{HA_URL}/api/services/light/turn_on", headers=_ha_headers(),
                            json={"entity_id": BEDROOM_LAMP, "rgb_color": LAMP_RGB,
                                  "brightness": _pct(LAMP_FADE_START)},
                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status != 200:
            return f"✗ lamp: HTTP {resp.status}"
    try:
        _r.set(_FADE_KEY, "1", ex=LAMP_FADE_MIN * 60 + 600)
    except Exception:
        pass

    async def _fade() -> None:
        steps = LAMP_FADE_MIN          # one step a minute: well inside Govee's rate limit
        try:
            async with aiohttp.ClientSession() as s2:
                for i in range(1, steps + 1):
                    await asyncio.sleep(60)
                    if not _r.get(_FADE_KEY):
                        return                 # sleep mode cancelled
                    async with s2.get(f"{HA_URL}/api/states/{BEDROOM_LAMP}", headers=_ha_headers(),
                                      timeout=aiohttp.ClientTimeout(total=15)) as chk:
                        if chk.status == 200 and (await chk.json()).get("state") == "off":
                            return             # someone turned it off already
                    if i == steps:
                        await s2.post(f"{HA_URL}/api/services/light/turn_off", headers=_ha_headers(),
                                      json={"entity_id": BEDROOM_LAMP},
                                      timeout=aiohttp.ClientTimeout(total=20))
                    else:
                        await s2.post(f"{HA_URL}/api/services/light/turn_on", headers=_ha_headers(),
                                      json={"entity_id": BEDROOM_LAMP,
                                            "brightness": _pct(LAMP_FADE_START * (1 - i / steps))},
                                      timeout=aiohttp.ClientTimeout(total=20))
        except Exception:
            pass
        finally:
            try:
                _r.delete(_FADE_KEY)
            except Exception:
                pass

    asyncio.get_running_loop().create_task(_fade())
    return f"✓ lamp → {LAMP_FADE_START}%, fading to off over {LAMP_FADE_MIN} min"


async def _ha_call(session: aiohttp.ClientSession, service: str, data: dict) -> int:
    domain, name = service.split(".", 1)
    async with session.post(f"{HA_URL}/api/services/{domain}/{name}",
                            headers=_ha_headers(), json=data,
                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
        return resp.status


async def _wake_tv_for_spotify(session: aiohttp.ClientSession,
                               sp_headers: dict) -> tuple[Optional[dict], str]:
    """Wake the bedroom Apple TV and open Spotify on it, so it shows up as a
    Spotify Connect device. Returns (device, note).

    A sleeping Apple TV is not a Connect device at all: Spotify only lists it
    while the Spotify app is open on it. So: power it on (HDMI-CEC brings the
    TV up with it), launch the Spotify app through HA's select_source, then
    wait for the device to appear.

    Both Apple TVs call themselves "Apple TV" in Spotify, so the bedroom one
    can't be found by name. It is the tv-type device that wasn't listed before
    we woke it. SLEEP_TV_SPOTIFY_DEVICE (a name) is used only if it was
    already listed, e.g. Spotify was left open on it.
    """
    from .spotify import _get_devices, _match_device

    before = {d.get("id") for d in await _get_devices(session, sp_headers)}
    if await _ha_call(session, "media_player.turn_on", {"entity_id": TV_ENTITY}) != 200:
        return None, f"couldn't wake {TV_ENTITY}"
    await asyncio.sleep(3)
    await _ha_call(session, "media_player.select_source",
                   {"entity_id": TV_ENTITY, "source": "Spotify"})

    for _ in range(15):                       # up to ~30s for the app to register
        await asyncio.sleep(2)
        devices = await _get_devices(session, sp_headers)
        new = [d for d in devices if d.get("id") not in before
               and (d.get("type") or "").lower() == "tv"]
        if new:
            return new[0], "bedroom TV"
        named = _match_device(devices, TV_SPOTIFY_NAME) if TV_SPOTIFY_NAME else None
        if named:
            return named, "bedroom TV"
    return None, "the bedroom TV woke but Spotify never showed up on it"


async def _start_music(minutes: int) -> str:
    """Pick the device (bedroom TV, else the Mac), loop the track, arm the timer."""
    from .spotify import _access_token, _get_devices, _match_device

    if not SLEEP_TRACK.strip():
        return ("✗ music: no SLEEP_TRACK configured — set SLEEP_TRACK in .env to "
                "the song you want looped")

    async with aiohttp.ClientSession() as session:
        token = await _access_token(session)
        if not token:
            return "✗ music: Spotify auth failed"
        headers = {"Authorization": f"Bearer {token}"}

        target, where, note = None, "", ""
        if TV_ENTITY:
            target, why = await _wake_tv_for_spotify(session, headers)
            if target:
                where = why
            else:
                note = f" ({why}, so it's on the Mac instead)"
        if target is None:
            devices = await _get_devices(session, headers)
            target = _match_device(devices, SLEEP_DEVICE) if SLEEP_DEVICE else None
            if target is None:
                names = ", ".join(d.get("name", "?") for d in devices) or "none"
                return (f"✗ music: no Spotify device matching '{SLEEP_DEVICE}' "
                        f"(available: {names}){note}")
            where = target.get("name", SLEEP_DEVICE)

        r1 = await _play(session, headers, target["id"])
        if r1:
            return f"✗ music: {r1}{note}"
        await asyncio.sleep(2)
        # Some Spotify clients (the Apple TV app among them) refuse remote
        # volume; that's not worth failing bedtime over.
        await _player_put(session, headers, "volume",
                          {"volume_percent": SLEEP_VOLUME, "device_id": target["id"]})
        await _player_put(session, headers, "repeat",
                          {"state": "track", "device_id": target["id"]})

    _arm_timer(minutes, tv=bool(where == "bedroom TV"))
    return (f"✓ music → '{SLEEP_TRACK_NAME}' looping on the {where}{note}, "
            f"stops in {minutes} min")


async def _player_put(session, headers, path: str, params: dict) -> int:
    from .spotify import _API
    try:
        async with session.put(f"{_API}/me/player/{path}", headers=headers, params=params,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return resp.status
    except Exception:
        return 0


async def _play(session, headers, device_id: str) -> str:
    """Start SLEEP_TRACK on the device. Returns '' on success, else the error.

    A spotify: URI is played exactly. "twilight zone" returns two near-identical
    Ariana Grande entries from different album editions, so a search would pick
    whichever ranked first that night. A pinned URI cannot drift.
    """
    from .spotify import _API, _set_media_flag
    uri = SLEEP_TRACK.strip()
    if not uri.startswith("spotify:"):
        async with session.get(f"{_API}/search", headers=headers,
                               params={"q": uri, "type": "track", "limit": 1}) as resp:
            items = ((await resp.json()).get("tracks", {}).get("items", [])
                     if resp.status == 200 else [])
        if not items:
            return f"no Spotify track found for '{uri}'"
        uri = items[0]["uri"]
    body = ({"context_uri": uri} if ":playlist:" in uri or ":album:" in uri
            else {"uris": [uri]})
    async with session.put(f"{_API}/me/player/play", headers=headers,
                           params={"device_id": device_id}, json=body,
                           timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status in (200, 204):
            _set_media_flag(True)
            return ""
        return f"play failed (HTTP {resp.status})"


async def _tv_off() -> None:
    """Put the bedroom Apple TV (and, over CEC, the TV) back to sleep."""
    if not TV_ENTITY:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await _ha_call(session, "media_player.turn_off", {"entity_id": TV_ENTITY})
    except Exception:
        pass


def _arm_timer(minutes: int, tv: bool = False) -> None:
    """Record the deadline, then schedule the precise in-process stop.

    The Redis key is the durable half: watchdog.sh pauses Spotify once the
    deadline passes, so a container restart cannot leave music playing all
    night. The asyncio task is the accurate half -- it fires on the minute
    rather than at the next watchdog tick.
    """
    deadline = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    try:
        _r.set(_STOP_KEY, deadline.isoformat(), ex=minutes * 60 + 3600)
        if tv:
            _r.set(_TV_KEY, "1", ex=minutes * 60 + 3600)
        else:
            _r.delete(_TV_KEY)
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
                tv_on = bool(_r.get(_TV_KEY))
                _r.delete(_STOP_KEY, _TV_KEY)
            except Exception:
                tv_on = tv
            if tv_on:
                await _tv_off()
        except asyncio.CancelledError:
            pass

    try:
        asyncio.get_running_loop().create_task(_stop_later())
    except RuntimeError:
        pass   # no loop (sync caller) -- watchdog remains the backstop


@tool
async def sleep_mode(action: str = "on", minutes: int = 0) -> str:
    """Run the bedtime routine: cool the house, fade the bedroom lamp to off
    (20 min, like Govee's Sleeping timer), wake the
    bedroom TV and loop the sleep track on it (the Mac if the TV can't be
    reached), then stop the music and turn the TV off automatically. It does
    the TV wake-up itself, so don't call spotify_control or control_device
    first.

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
            tv_on = bool(_r.get(_TV_KEY))
            _r.delete(_STOP_KEY, _TV_KEY, _FADE_KEY)
        except Exception:
            tv_on = False
        from .spotify import spotify_control
        call = getattr(spotify_control, "func", None) or getattr(
            spotify_control, "coroutine", None) or spotify_control
        res = await call(action="pause")
        if tv_on:
            await _tv_off()
            res += " Bedroom TV off."
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
