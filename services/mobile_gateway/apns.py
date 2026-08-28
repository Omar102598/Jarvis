"""APNs sender — real iOS push notifications for proactive cards.

Local notifications only cover the brief window before iOS suspends the app's
WebSocket; this delivers Sentry cards, agent reports, and approval requests
even when the app has been backgrounded for hours.

ENTIRELY GATED on configuration — a silent no-op until Omar joins the Apple
Developer Program and drops in an APNs auth key:

    1. developer.apple.com → Certificates → Keys → create an APNs key (.p8)
    2. save it under data/apns/ (gitignored), then set in .env / compose:
         APNS_KEY_PATH=/data/apns/AuthKey_XXXXXXXXXX.p8
         APNS_KEY_ID=XXXXXXXXXX
         APNS_TEAM_ID=<10-char team id>
         APNS_TOPIC=<app bundle id, e.g. com.omar.JarvisApp>
         APNS_ENV=sandbox   # 'production' for TestFlight/App Store builds
    3. iOS app: registerForRemoteNotifications + POST the device token to
       /apns/register (Swift side is a queued task — see docs/BUILD_TRACKER.md)

Auth: JWT (ES256) signed with the .p8, cached ~45 min (Apple allows 20-60).
Transport: HTTP/2 via httpx. Tokens live in Redis set ``apns:tokens``;
permanently-invalid tokens (410 / BadDeviceToken) are pruned automatically.
"""

from __future__ import annotations

import json
import os
import threading
import time

APNS_KEY_PATH = os.environ.get("APNS_KEY_PATH", "")
APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "")
APNS_TOPIC = os.environ.get("APNS_TOPIC", "")
APNS_ENV = os.environ.get("APNS_ENV", "sandbox").lower()

TOKENS_KEY = "apns:tokens"
_HOSTS = {
    "sandbox": "https://api.sandbox.push.apple.com",
    "production": "https://api.push.apple.com",
}

_jwt_cache: tuple[str, float] | None = None
_jwt_lock = threading.Lock()
_warned = False


def configured() -> bool:
    global _warned
    ok = bool(APNS_KEY_PATH and APNS_KEY_ID and APNS_TEAM_ID and APNS_TOPIC
              and os.path.exists(APNS_KEY_PATH))
    if not ok and not _warned:
        _warned = True
        print("[APNs] not configured (APNS_KEY_PATH/KEY_ID/TEAM_ID/TOPIC) — "
              "pushes stay WebSocket+local-notification only.")
    return ok


def _auth_token() -> str:
    """Signed provider JWT, cached for 45 minutes."""
    global _jwt_cache
    with _jwt_lock:
        if _jwt_cache and time.time() - _jwt_cache[1] < 45 * 60:
            return _jwt_cache[0]
        import jwt  # PyJWT + cryptography
        with open(APNS_KEY_PATH) as f:
            secret = f.read()
        token = jwt.encode(
            {"iss": APNS_TEAM_ID, "iat": int(time.time())},
            secret, algorithm="ES256",
            headers={"kid": APNS_KEY_ID},
        )
        _jwt_cache = (token, time.time())
        return token


def send_alert(r, title: str, body: str, *, thread_id: str = "jarvis",
               payload_extra: dict | None = None) -> int:
    """Send an alert push to every registered device. Returns delivered count.
    Synchronous — call from a worker thread (the MQTT callback thread is fine),
    never from the event loop."""
    if not configured():
        return 0
    tokens = list(r.smembers(TOKENS_KEY) or [])
    if not tokens:
        return 0
    import httpx

    apns_payload = {
        "aps": {
            "alert": {"title": title[:120], "body": body[:900]},
            "sound": "default",
            "thread-id": thread_id,
            "mutable-content": 1,
        }
    }
    if payload_extra:
        apns_payload.update(payload_extra)
    host = _HOSTS.get(APNS_ENV, _HOSTS["sandbox"])
    headers = {
        "authorization": f"bearer {_auth_token()}",
        "apns-topic": APNS_TOPIC,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }
    delivered = 0
    with httpx.Client(http2=True, timeout=10) as client:
        for token in tokens:
            try:
                resp = client.post(f"{host}/3/device/{token}",
                                   headers=headers, json=apns_payload)
                if resp.status_code == 200:
                    delivered += 1
                elif resp.status_code in (400, 410):
                    reason = ""
                    try:
                        reason = resp.json().get("reason", "")
                    except Exception:
                        pass
                    if resp.status_code == 410 or reason == "BadDeviceToken":
                        r.srem(TOKENS_KEY, token)
                        print(f"[APNs] pruned dead token …{token[-8:]}")
                    else:
                        print(f"[APNs] send failed ({reason or resp.status_code})")
                else:
                    print(f"[APNs] send failed: HTTP {resp.status_code}")
            except Exception as exc:
                print(f"[APNs] send error: {exc}")
    return delivered
