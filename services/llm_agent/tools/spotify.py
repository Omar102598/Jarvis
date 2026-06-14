"""Spotify control tool for JARVIS — play/pause/skip/search via Web API.

Requires a Spotify app and a stored refresh token (playback control needs
Spotify Premium):
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
"""

import base64
import os

import aiohttp
from langchain_core.tools import tool

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")

_API = "https://api.spotify.com/v1"


async def _access_token(session: aiohttp.ClientSession) -> str | None:
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    async with session.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN},
    ) as resp:
        if resp.status != 200:
            return None
        return (await resp.json()).get("access_token")


@tool
async def spotify_control(action: str, query: str = "") -> str:
    """Control Spotify playback.

    Args:
        action: One of 'play', 'pause', 'next', 'previous', or 'search_play'.
        query: For 'search_play', what to play (track/artist/album), e.g.
            'Daft Punk', 'lofi beats'. Ignored for other actions.
    """
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        return "Spotify is not configured. Set SPOTIFY_CLIENT_ID/SECRET/REFRESH_TOKEN."

    action = action.lower().strip()
    async with aiohttp.ClientSession() as session:
        token = await _access_token(session)
        if not token:
            return "Could not authenticate with Spotify (check refresh token)."
        headers = {"Authorization": f"Bearer {token}"}

        try:
            if action == "play":
                await session.put(f"{_API}/me/player/play", headers=headers)
                return "Resumed playback."
            if action == "pause":
                await session.put(f"{_API}/me/player/pause", headers=headers)
                return "Paused."
            if action == "next":
                await session.post(f"{_API}/me/player/next", headers=headers)
                return "Skipped to next track."
            if action == "previous":
                await session.post(f"{_API}/me/player/previous", headers=headers)
                return "Went to previous track."
            if action == "search_play":
                if not query:
                    return "What should I play?"
                async with session.get(
                    f"{_API}/search",
                    headers=headers,
                    params={"q": query, "type": "track", "limit": 1},
                ) as resp:
                    data = await resp.json()
                items = data.get("tracks", {}).get("items", [])
                if not items:
                    return f"Couldn't find anything for '{query}'."
                track = items[0]
                async with session.put(
                    f"{_API}/me/player/play",
                    headers=headers,
                    json={"uris": [track["uri"]]},
                ) as resp:
                    if resp.status in (200, 204):
                        artist = track["artists"][0]["name"]
                        return f"Playing {track['name']} by {artist}."
                    return "Found it, but couldn't start playback (is a device active?)."
            return f"Unknown action '{action}'. Use play, pause, next, previous, search_play."
        except Exception as exc:
            return f"Spotify control failed: {exc}"
