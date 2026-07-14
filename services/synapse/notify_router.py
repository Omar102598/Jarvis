"""Notification router — one funnel for everything that wants your attention.

Before this, every agent published straight to ``jarvis/surfaces/iphone/push``,
so a busy morning meant a dozen separate pings. The router puts a policy layer
in front of that single channel:

    agents/Synapse ──publish──> jarvis/notify ──router──┬─ urgent  → surface push NOW
                                                        └─ normal/low → digest queue
                                                                        (flushed as ONE card)

Payload on ``jarvis/notify``:
    {
      "title": str,
      "text":  str,
      "media_url": str | null,     # image/video card (Ring snapshot, live view)
      "urgency": "urgent" | "normal" | "low",   # default "normal"
      "source": str,               # agent/persona name, for de-dup + digest grouping
      "dedup_key": str | null      # optional; suppress repeats within DEDUP_TTL
    }

Backward compatible: anything still publishing directly to
``jarvis/surfaces/iphone/push`` keeps working untouched — the router is
additive. Agents opt in via agent_runner/notify.py.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

SURFACE_TOPIC = "jarvis/surfaces/iphone/push"
DIGEST_QUEUE = "jarvis:digest:queue"
DIGEST_MAX = int(os.environ.get("DIGEST_MAX", "8"))          # flush when queue hits this
DIGEST_INTERVAL_S = int(os.environ.get("DIGEST_INTERVAL_S", "3600"))  # or every hour
DEDUP_TTL_S = int(os.environ.get("NOTIFY_DEDUP_TTL_S", "1800"))
# Media-bearing pushes (Ring snapshots/live views) always go straight through —
# a camera card batched into an hourly digest is useless.
_ALWAYS_IMMEDIATE_WITH_MEDIA = True


def _focus_active(r) -> bool:
    """True while the user is in Focus/deep-work mode — hold non-urgent notifs."""
    try:
        return bool(r.exists("jarvis:focus"))
    except Exception:
        return False


def _publish_surface(mqtt_client, title: str, text: str, media_url: str = "") -> None:
    body = {"title": title, "text": text}
    if media_url:
        body["media_url"] = media_url
    mqtt_client.publish(SURFACE_TOPIC, json.dumps(body))


def handle(r, mqtt_client, raw: bytes) -> None:
    """Route one jarvis/notify message. Never raises into the MQTT loop."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return
    text = (data.get("text") or "").strip()
    if not text:
        return
    title = data.get("title") or "Jarvis"
    media_url = data.get("media_url") or ""
    urgency = (data.get("urgency") or "normal").lower()
    source = data.get("source") or ""

    # De-dup: suppress identical alerts within the TTL window.
    dedup_key = data.get("dedup_key") or f"{source}:{title}:{text[:60]}"
    try:
        if not r.set(f"jarvis:notify:seen:{dedup_key}", "1",
                     nx=True, ex=DEDUP_TTL_S):
            return  # a matching alert already went out recently
    except Exception:
        pass

    immediate = urgency == "urgent" or (media_url and _ALWAYS_IMMEDIATE_WITH_MEDIA)
    if immediate:
        _publish_surface(mqtt_client, title, text, media_url)
        return

    # Batch: enqueue for the next digest flush.
    try:
        r.lpush(DIGEST_QUEUE, json.dumps({
            "title": title, "text": text, "source": source,
            "urgency": urgency,
            "ts": datetime.now(timezone.utc).isoformat(),
        }))
        r.ltrim(DIGEST_QUEUE, 0, 199)
        # Don't flush on size while in Focus mode — hold the batch until it ends.
        if r.llen(DIGEST_QUEUE) >= DIGEST_MAX and not _focus_active(r):
            flush(r, mqtt_client)
    except Exception:
        # If Redis is unhappy, fail open — deliver it now rather than lose it.
        _publish_surface(mqtt_client, title, text, media_url)


def flush(r, mqtt_client, force: bool = False) -> int:
    """Compose the queued low-priority items into ONE digest card and send it.

    Returns the number of items delivered (0 = nothing queued). ``force`` is
    used by the manual flush trigger and the morning brief; the periodic timer
    calls it plainly.
    """
    try:
        items = r.lrange(DIGEST_QUEUE, 0, -1)
        if not items:
            return 0
        r.delete(DIGEST_QUEUE)
    except Exception:
        return 0

    parsed = []
    for it in reversed(items):        # oldest first
        try:
            parsed.append(json.loads(it))
        except Exception:
            continue
    if not parsed:
        return 0

    n = len(parsed)
    lines = []
    for it in parsed:
        who = it.get("source", "")
        prefix = f"• {who}: " if who else "• "
        lines.append(prefix + it.get("text", "")[:180])
    title = f"🗒️ Jarvis digest — {n} update{'s' if n != 1 else ''}"
    body = "\n".join(lines)
    _publish_surface(mqtt_client, title, body)
    return n


def maybe_flush_on_timer(r, mqtt_client) -> None:
    """Called by Synapse's timer loop. Flush if enough time has elapsed since
    the last flush AND the queue is non-empty."""
    if _focus_active(r):
        return   # stay silent during deep work; the batch waits
    try:
        last = float(r.get("jarvis:digest:last_flush") or 0)
    except Exception:
        last = 0.0
    if time.time() - last < DIGEST_INTERVAL_S:
        return
    try:
        if r.llen(DIGEST_QUEUE) == 0:
            return
    except Exception:
        return
    if flush(r, mqtt_client) > 0:
        try:
            r.set("jarvis:digest:last_flush", str(time.time()))
        except Exception:
            pass
