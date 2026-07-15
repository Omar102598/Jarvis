"""Spotify control tool for JARVIS — play/pause/skip/search/volume via Web API,
with DEVICE TARGETING so music can be sent to a specific room's speaker.

Any Spotify Connect device (the Mac app, Sonos speakers, TVs…) shows up in
GET /me/player/devices by name — so "play jazz in the living room" resolves the
device by fuzzy name match and targets playback there. This is the multi-room
path: when Sonos speakers arrive they appear here automatically, no new code.

Requires a Spotify app and a stored refresh token (playback control needs
Spotify Premium):
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
"""

import base64
import os
from typing import Optional

import aiohttp
from langchain_core.tools import tool

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")

_API = "https://api.spotify.com/v1"


async def _access_token(session: aiohttp.ClientSession) -> Optional[str]:
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    async with session.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN},
    ) as resp:
        if resp.status != 200:
            return None
        return (await resp.json()).get("access_token")


async def _get_devices(session, headers) -> list[dict]:
    """All Spotify Connect devices visible to the account (Mac, Sonos, TV…)."""
    try:
        async with session.get(f"{_API}/me/player/devices", headers=headers) as resp:
            if resp.status != 200:
                return []
            return (await resp.json()).get("devices", []) or []
    except Exception:
        return []


def _match_device(devices: list[dict], name: str) -> Optional[dict]:
    """Fuzzy-match a device by name ('living room' → 'Living Room Era 100')."""
    n = name.lower().strip()
    if not n:
        return None
    for d in devices:
        if d.get("name", "").lower() == n:
            return d
    for d in devices:
        if n in d.get("name", "").lower():
            return d
    # loose word-overlap ("living room speaker" matches "Living Room")
    words = set(n.split())
    for d in devices:
        if words & set(d.get("name", "").lower().split()):
            return d
    return None


def _fmt_devices(devices: list[dict]) -> str:
    if not devices:
        return ("No Spotify Connect devices found — open Spotify on a device "
                "(or power on a speaker) so it registers.")
    lines = []
    for d in devices:
        mark = "▶" if d.get("is_active") else "•"
        vol = d.get("volume_percent")
        lines.append(f"{mark} {d.get('name','?')} ({d.get('type','?').lower()}"
                     + (f", vol {vol}%" if vol is not None else "") + ")")
    return "Spotify devices:\n" + "\n".join(lines)


@tool
async def spotify_control(action: str, query: str = "", device: str = "") -> str:
    """Control Spotify playback, optionally on a specific device/room speaker.

    Args:
        action: 'play', 'pause', 'next', 'previous', 'search_play', 'devices'
            (list available speakers/devices), 'transfer' (move current playback
            to a device), or 'volume' (set volume; put the percent in query).
        query: For 'search_play': what to play — a track/artist ('Daft Punk'),
            or include the word 'playlist' to search playlists ('workout
            playlist'). For 'volume': the percent, e.g. '40'.
        device: Optional device/room name to target ('living room', 'Mac',
            'kitchen') — fuzzy-matched against available Spotify Connect
            devices. Empty = whatever device is currently active.
    """
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        return "Spotify is not configured. Set SPOTIFY_CLIENT_ID/SECRET/REFRESH_TOKEN."

    action = action.lower().strip()
    async with aiohttp.ClientSession() as session:
        token = await _access_token(session)
        if not token:
            return "Could not authenticate with Spotify (check refresh token)."
        headers = {"Authorization": f"Bearer {token}"}

        # Resolve the target device once, if one was named.
        target = None
        if device.strip() or action in ("devices", "transfer"):
            devices = await _get_devices(session, headers)
            if action == "devices":
                return _fmt_devices(devices)
            if device.strip():
                target = _match_device(devices, device)
                if target is None:
                    names = ", ".join(d.get("name", "?") for d in devices) or "none"
                    return (f"No Spotify device matching '{device}'. "
                            f"Available: {names}.")
        dev_params = {"device_id": target["id"]} if target else {}

        try:
            if action == "transfer":
                if not target:
                    return "Which device should I move the music to?"
                async with session.put(
                    f"{_API}/me/player", headers=headers,
                    json={"device_ids": [target["id"]], "play": True},
                ) as resp:
                    if resp.status in (200, 204):
                        return f"Moved playback to {target['name']}."
                    return f"Couldn't transfer playback ({resp.status})."

            if action == "volume":
                try:
                    pct = max(0, min(100, int(float(query.strip().rstrip("%")))))
                except (ValueError, AttributeError):
                    return "Give me a volume percent, e.g. '40'."
                async with session.put(
                    f"{_API}/me/player/volume", headers=headers,
                    params={"volume_percent": pct, **dev_params},
                ) as resp:
                    if resp.status in (200, 204):
                        where = f" on {target['name']}" if target else ""
                        return f"Volume set to {pct}%{where}."
                    return f"Couldn't set volume ({resp.status})."

            if action == "play":
                async with session.put(f"{_API}/me/player/play", headers=headers,
                                       params=dev_params) as resp:
                    where = f" on {target['name']}" if target else ""
                    return (f"Resumed playback{where}." if resp.status in (200, 204)
                            else f"Couldn't resume ({resp.status}) — is a device active?")
            if action == "pause":
                await session.put(f"{_API}/me/player/pause", headers=headers,
                                  params=dev_params)
                return "Paused."
            if action == "next":
                await session.post(f"{_API}/me/player/next", headers=headers,
                                   params=dev_params)
                return "Skipped to next track."
            if action == "previous":
                await session.post(f"{_API}/me/player/previous", headers=headers,
                                   params=dev_params)
                return "Went to previous track."

            if action == "search_play":
                if not query:
                    return "What should I play?"
                want_playlist = "playlist" in query.lower()
                stype = "playlist" if want_playlist else "track"
                q = query.lower().replace("playlist", "").strip() or query
                async with session.get(
                    f"{_API}/search", headers=headers,
                    params={"q": q, "type": stype, "limit": 3},
                ) as resp:
                    data = await resp.json()
                items = [i for i in
                         data.get(f"{stype}s", {}).get("items", []) if i]
                if not items:
                    return f"Couldn't find anything for '{query}'."
                item = items[0]
                body = ({"context_uri": item["uri"]} if want_playlist
                        else {"uris": [item["uri"]]})
                async with session.put(
                    f"{_API}/me/player/play", headers=headers,
                    params=dev_params, json=body,
                ) as resp:
                    if resp.status in (200, 204):
                        where = f" on {target['name']}" if target else ""
                        if want_playlist:
                            return f"Playing the playlist {item['name']}{where}."
                        artist = item["artists"][0]["name"]
                        return f"Playing {item['name']} by {artist}{where}."
                    if resp.status == 404:
                        return ("Found it, but no active device — say the room "
                                "name ('…in the living room') or open Spotify "
                                "somewhere first.")
                    return f"Found it, but playback failed ({resp.status})."

            return (f"Unknown action '{action}'. Use play, pause, next, previous, "
                    "search_play, devices, transfer, volume.")
        except Exception as exc:
            return f"Spotify control failed: {exc}"
