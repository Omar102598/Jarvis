"""Spotify control via the macOS desktop app — no OAuth / refresh token needed.

Uses Spotify's client_credentials flow to search (no user scope required) and
AppleScript via mac_bridge to play/pause/skip inside the already-running Spotify.app.

Requires only SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env.
"""

import base64
import os
from typing import Optional

import aiohttp
from langchain_core.tools import tool

_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

_HOST = os.environ.get("MAC_BRIDGE_HOST", "host.docker.internal")
_PORT = int(os.environ.get("MAC_BRIDGE_PORT", "7777"))
_BASE = f"http://{_HOST}:{_PORT}"


async def _osa(script: str) -> str:
    """Run AppleScript via mac_bridge — osascript only works on the host Mac."""
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{_BASE}/applescript", json={"script": script}) as r:
            data = await r.json()
            return data.get("result", "") or data.get("error", "") or ""


async def _open_uri(uri: str) -> None:
    """Open a URI (e.g. spotify:search:...) via mac_bridge /open."""
    async with aiohttp.ClientSession() as s:
        await s.post(f"{_BASE}/open", json={"what": uri})


async def _client_token() -> Optional[str]:
    """Get an access token via client_credentials (no user auth needed for search)."""
    if not (_CLIENT_ID and _CLIENT_SECRET):
        return None
    auth = base64.b64encode(f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode()).decode()
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {auth}"},
            data={"grant_type": "client_credentials"},
        ) as resp:
            if resp.status != 200:
                return None
            return (await resp.json()).get("access_token")


@tool
async def spotify_desktop(action: str, query: str = "") -> str:
    """Control the Spotify desktop app — play, pause, skip, or search and play.

    Uses the already-running Spotify app on your Mac (no refresh token needed).
    Actions: play, pause, next, previous, search_play, now_playing.

    Args:
        action: 'play', 'pause', 'next', 'previous', 'search_play', or 'now_playing'
        query: For 'search_play' — artist, song, or album to play (e.g. 'Morgan Wallen')
    """
    action = action.lower().strip()

    # Simple playback controls — pure AppleScript, no API needed
    if action == "play":
        await _osa('tell application "Spotify" to play')
        return "Spotify: playing."
    if action == "pause":
        await _osa('tell application "Spotify" to pause')
        return "Spotify: paused."
    if action == "next":
        await _osa('tell application "Spotify" to next track')
        return "Spotify: skipped to next track."
    if action == "previous":
        await _osa('tell application "Spotify" to previous track')
        return "Spotify: went to previous track."
    if action == "now_playing":
        out = await _osa("""
            tell application "Spotify"
                set t to name of current track
                set a to artist of current track
                set alb to album of current track
                return t & " by " & a & " (" & alb & ")"
            end tell
        """)
        return f"Now playing: {out}" if out else "Nothing playing."

    if action == "search_play":
        if not query:
            return "What would you like me to play?"

        if not (_CLIENT_ID and _CLIENT_SECRET):
            # Fallback: open Spotify search URI (no API)
            import urllib.parse
            encoded = urllib.parse.quote(query)
            await _osa('tell application "Spotify" to activate')
            await _open_uri(f"spotify:search:{encoded}")
            return f"Opened Spotify search for '{query}'. Press play to start."

        token = await _client_token()
        if not token:
            return "Could not reach Spotify API. Check SPOTIFY_CLIENT_ID/SECRET."

        # Search for the best track match
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.spotify.com/v1/search",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": query, "type": "track", "limit": 1},
            ) as resp:
                if resp.status != 200:
                    return f"Spotify search failed (status {resp.status})."
                data = await resp.json()

        items = data.get("tracks", {}).get("items", [])
        if not items:
            return f"No tracks found for '{query}'."

        track  = items[0]
        uri    = track["uri"]
        name   = track["name"]
        artist = track["artists"][0]["name"]

        # Play the track URI via AppleScript through mac_bridge
        await _osa(f'tell application "Spotify" to play track "{uri}"')
        return f"Playing {name} by {artist}."

    return f"Unknown Spotify action '{action}'. Use play, pause, next, previous, search_play, or now_playing."
