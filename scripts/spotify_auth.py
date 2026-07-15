#!/usr/bin/env python3
"""One-time Spotify OAuth — mints the SPOTIFY_REFRESH_TOKEN that unlocks
Jarvis's Web-API playback control (device/room targeting, Sonos, volume).

Prereq (once, at https://developer.spotify.com/dashboard → your app → Settings):
  add Redirect URI:  http://127.0.0.1:8899/callback

Run on the Mac:  python3 scripts/spotify_auth.py
It reads SPOTIFY_CLIENT_ID/SECRET from .env, opens the browser for you to
approve, catches the redirect, and prints the line to add to .env.
Then: docker compose up -d llm_agent   (env change only, no rebuild needed...
actually compose re-reads .env on 'up' — run it and you're done).
"""

import base64
import http.server
import json
import os
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8899
REDIRECT = f"http://127.0.0.1:{PORT}/callback"
SCOPES = "user-modify-playback-state user-read-playback-state"


def _env(key: str) -> str:
    try:
        for line in open(os.path.join(REPO, ".env")):
            if line.strip().startswith(f"{key}="):
                return line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    return os.environ.get(key, "")


CLIENT_ID = _env("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = _env("SPOTIFY_CLIENT_SECRET")
if not (CLIENT_ID and CLIENT_SECRET):
    sys.exit("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not found in .env")

_code: list[str] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in q:
            _code.append(q["code"][0])
            body = b"<h2>Done - you can close this tab. Check the terminal.</h2>"
        else:
            body = b"<h2>No code in callback.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence request logging
        pass


def main():
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": SCOPES,
    })
    print("Opening browser for Spotify approval…\n(If it doesn't open, visit:)\n" + auth_url)
    webbrowser.open(auth_url)

    import time
    for _ in range(300):
        if _code:
            break
        time.sleep(1)
    server.shutdown()
    if not _code:
        sys.exit("Timed out waiting for the callback. Is the Redirect URI added to your Spotify app?")

    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": _code[0],
            "redirect_uri": REDIRECT,
        }).encode(),
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        tokens = json.load(resp)
    refresh = tokens.get("refresh_token")
    if not refresh:
        sys.exit(f"No refresh token in response: {tokens}")

    print("\n✅ Success! Add this line to .env:\n")
    print(f"SPOTIFY_REFRESH_TOKEN={refresh}")
    print("\nThen: docker compose up -d llm_agent")


if __name__ == "__main__":
    main()
