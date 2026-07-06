"""Ring camera tools — on-demand looks through the user's Ring cameras.

Data comes from the ring-mqtt bridge via Redis (the agent_runner caches
snapshots and events). Proactive alerting is Sentry's job; these tools are
for "check on Finley" / "what's happening outside" style requests.
"""

import json
import os
from datetime import datetime, timezone

import redis
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

_r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)


def _cameras() -> dict:
    """{device_id: {'name':…, 'location':…, 'last_seen':…}} from the registry."""
    cams = {}
    names = _r.hgetall("ring:camera_names")
    for device, raw in (_r.hgetall("ring:cameras") or {}).items():
        try:
            meta = json.loads(raw)
        except Exception:
            meta = {}
        meta["name"] = names.get(device, device)
        cams[device] = meta
    return cams


def _age_minutes(iso_ts: str) -> float | None:
    try:
        dt = datetime.fromisoformat(iso_ts)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        return None


@tool
def list_ring_cameras() -> str:
    """List the user's Ring cameras and recent camera events.

    Use for "what cameras do I have?", "any activity today?", or to find the
    right camera name before check_ring_camera.
    """
    cams = _cameras()
    if not cams:
        return ("No Ring cameras discovered yet. The ring-mqtt bridge may not "
                "be running or logged in — check `docker logs jarvis-ring-mqtt`.")
    lines = [f"{len(cams)} Ring camera(s):"]
    for device, meta in cams.items():
        ts = _r.get(f"ring:camera:{device}:snapshot_ts") or ""
        age = _age_minutes(ts)
        snap = f"snapshot {age:.0f} min old" if age is not None else "no snapshot yet"
        lines.append(f"  • {meta['name']} ({device}) — {snap}")

    events = _r.lrange("ring:events:assessed", 0, 4)
    if events:
        lines.append("\nRecent assessed events:")
        for raw in events:
            try:
                e = json.loads(raw)
                flag = "❗" if e.get("notable") else "·"
                lines.append(f"  {flag} [{e.get('ts','')[:16]}] {e.get('camera')}: "
                             f"{e.get('summary','')}")
            except Exception:
                continue
    return "\n".join(lines)


def _refresh_snapshot(device: str) -> bool:
    """Force a fresh RTSP frame grab via the gateway (10-20s). True on success."""
    import urllib.request as _rq
    import urllib.parse as _p
    base = os.environ.get("GATEWAY_INTERNAL_URL", "http://mobile_gateway:8080")
    key = _p.quote(os.environ.get("MOBILE_API_KEY", ""))
    try:
        req = _rq.Request(f"{base}/ring/snapshot/{device}/refresh?k={key}",
                          data=b"", method="POST")
        _rq.urlopen(req, timeout=50)
        return True
    except Exception:
        return False


def _push_snapshot_card(device: str, name: str, caption: str) -> None:
    """Send the snapshot image to the user's app/glasses as an image card."""
    base = os.environ.get("GATEWAY_PUBLIC_URL", "").rstrip("/")
    if not base:
        return
    key = os.environ.get("MOBILE_API_KEY", "")
    try:
        import paho.mqtt.publish as mqtt_pub
        mqtt_pub.single(
            "jarvis/surfaces/iphone/push",
            json.dumps({
                "text": caption[:180],
                "title": name,
                "media_url": f"{base}/ring/snapshot/{device}.jpg?k={key}",
            }),
            hostname=os.environ.get("MQTT_HOST", "localhost"),
            port=int(os.environ.get("MQTT_PORT", "1883")),
        )
    except Exception:
        pass


@tool
async def check_ring_camera(camera: str, question: str = "What do you see? Anything notable?",
                            send_to_app: bool = True) -> str:
    """Look through a Ring camera right now and describe what's visible.

    Use for "check on Finley", "is anyone outside?", "did my package arrive?",
    "send me a snapshot of the living room". Automatically grabs a FRESH frame
    when the cached one is stale (battery cameras like doorbells only update
    on motion otherwise), analyzes it with vision AI, and pushes the picture
    to the user's app/glasses.

    Args:
        camera: Camera name (e.g. 'Living Room', 'Front Door') or device id —
            fuzzy matched. Use list_ring_cameras if unsure.
        question: What to look for.
        send_to_app: Also push the snapshot image to the user's devices
            (default true — tell the user the picture is on their phone).
    """
    cams = _cameras()
    if not cams:
        return "No Ring cameras discovered yet — is the ring-mqtt bridge logged in?"

    want = camera.strip().lower()
    device = next(
        (d for d, m in cams.items()
         if want in m["name"].lower() or want in d.lower()),
        None,
    )
    if not device:
        options = ", ".join(m["name"] for m in cams.values())
        return f"No camera matching '{camera}'. Available: {options}"

    meta = cams[device]

    # Fresh-frame policy: refresh if the cached snapshot is missing or >2 min old
    ts = _r.get(f"ring:camera:{device}:snapshot_ts") or ""
    age = _age_minutes(ts)
    refreshed = False
    if age is None or age > 2:
        refreshed = _refresh_snapshot(device)
        ts = _r.get(f"ring:camera:{device}:snapshot_ts") or ts
        age = _age_minutes(ts)

    snap_b64 = _r.get(f"ring:camera:{device}:snapshot")
    if not snap_b64:
        return (f"'{meta['name']}' has no snapshot and the live frame grab "
                "failed — the camera may be offline.")

    if refreshed:
        age_note = " (live frame, just captured)"
    elif age is not None and age > 5:
        age_note = f" (snapshot {age:.0f} min old — fresh grab failed)"
    else:
        age_note = ""

    from llm_factory import build_llm
    llm = build_llm(temperature=0)
    response = await llm.ainvoke([
        SystemMessage(content=f"You are analysing a snapshot from the user's "
                              f"'{meta['name']}' Ring camera."),
        HumanMessage(content=[
            {"type": "text", "text": question},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{snap_b64}"}},
        ]),
    ])
    answer = response.content if isinstance(response.content, str) else str(response.content)

    sent_note = ""
    if send_to_app:
        _push_snapshot_card(device, meta["name"], answer)
        sent_note = " — snapshot sent to your devices"
    return f"[{meta['name']}{age_note}{sent_note}] {answer}"


@tool
def ring_privacy(action: str = "on", duration_minutes: int = 60) -> str:
    """Pause or resume Ring camera monitoring ("give me some privacy").

    While privacy mode is on, Jarvis drops ALL Ring camera data — no snapshot
    caching, no event logging, no Sentry assessments, no alerts. It auto-
    resumes when the timer expires (or say "resume camera monitoring").

    Use when the user says "give me some privacy", "stop watching the
    cameras", "privacy mode for 2 hours", or "turn the cameras back on".

    Args:
        action: 'on' (pause monitoring), 'off' (resume now), or 'status'.
        duration_minutes: How long privacy lasts before auto-resume
            (default 60; used only with action='on').
    """
    action = action.strip().lower()
    if action == "status":
        ttl = _r.ttl("sentry:privacy")
        if ttl and ttl > 0:
            return f"Privacy mode is ON — camera monitoring resumes in {ttl // 60} min."
        return "Privacy mode is off — Sentry is watching normally."
    if action == "off":
        _r.delete("sentry:privacy")
        return "Privacy mode off, sir — camera monitoring has resumed."
    # action == "on"
    mins = max(1, min(int(duration_minutes), 24 * 60))
    _r.set("sentry:privacy", "1", ex=mins * 60)
    return (f"Of course, sir — privacy mode is on. All camera monitoring is "
            f"paused for {mins} minutes and will resume automatically.")


@tool
def ring_live_view(camera: str) -> str:
    """Start a live video stream from a Ring camera and push it to the user's
    devices (Jarvis app / Meta glasses HUD).

    Use when the user says "show me the live view", "show me the video",
    "stream the front door", or asks for video after a snapshot alert.
    The stream runs for ~5 minutes then stops automatically.

    Args:
        camera: Camera name (e.g. 'Front Door') or device id — fuzzy matched.
    """
    import urllib.request as _rq
    import urllib.parse as _p

    cams = _cameras()
    want = camera.strip().lower()
    device = next((d for d, m in cams.items()
                   if want in m["name"].lower() or want in d.lower()), None)
    if not device:
        options = ", ".join(m["name"] for m in cams.values()) or "none discovered"
        return f"No camera matching '{camera}'. Available: {options}"

    key = os.environ.get("MOBILE_API_KEY", "")
    base_internal = os.environ.get("GATEWAY_INTERNAL_URL", "http://mobile_gateway:8080")
    base_public = os.environ.get("GATEWAY_PUBLIC_URL", "").rstrip("/")

    try:
        req = _rq.Request(
            f"{base_internal}/ring/live/{device}/start?k={_p.quote(key)}",
            data=b"", method="POST")
        resp = json.loads(_rq.urlopen(req, timeout=45).read())
    except Exception as e:
        return (f"Couldn't start the live stream for {cams[device]['name']}: {e}. "
                "The camera may be offline or the bridge restarting.")

    playlist = resp.get("playlist", "")
    public_url = f"{base_public}{playlist}" if base_public else playlist

    # Push a video card to the phone/glasses surfaces
    try:
        import paho.mqtt.publish as mqtt_pub
        mqtt_pub.single(
            "jarvis/surfaces/iphone/push",
            json.dumps({"text": f"Live view — {cams[device]['name']}",
                        "title": "Live View", "media_url": public_url}),
            hostname=os.environ.get("MQTT_HOST", "localhost"),
            port=int(os.environ.get("MQTT_PORT", "1883")),
        )
    except Exception:
        pass

    return (f"Live view of {cams[device]['name']} is streaming, sir — pushed to "
            f"your devices (runs ~5 minutes). Direct link: {public_url}")


@tool
def who_came_by(hours: int = 24) -> str:
    """Summarize recent camera activity — who/what Sentry saw and when.

    Use for "who came by today?", "any visitors?", "what did the cameras
    catch while I was out?".

    Args:
        hours: Look-back window (default 24).
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    events = []
    for raw in _r.lrange("ring:events:assessed", 0, 99):
        try:
            e = json.loads(raw)
            ts = datetime.fromisoformat(e.get("ts", ""))
            if ts >= cutoff:
                events.append(e)
        except Exception:
            continue
    if not events:
        return (f"Nothing assessed in the last {hours}h — either it was quiet "
                "or Sentry hasn't caught a motion event yet.")
    lines = [f"Camera activity, last {hours}h ({len(events)} assessed event(s)):"]
    for e in events[:20]:
        flag = "❗" if e.get("notable") else "·"
        lines.append(f"  {flag} [{e.get('ts','')[11:16]}] {e.get('camera')}: "
                     f"{e.get('summary','')}")
    return "\n".join(lines)
