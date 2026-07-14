"""Durable, replayable event log for the JARVIS bus.

MQTT is ephemeral — a message relayed to zero listeners is gone. This module
mirrors meaningful bus traffic into a Redis Stream (``jarvis:events``) that is:

  * durable      — survives restarts (persisted with the rest of Redis),
  * capped        — MAXLEN keeps it bounded (approx, cheap trimming),
  * replayable    — XRANGE by time powers Chronicle's daily summary, Synapse's
                    correlation window, and "what happened while I was out".

The stream is the shared substrate the whole intelligence layer reads from, so
it lives in Redis (already shared by every container) rather than inside one
service. Anything can ``XRANGE`` it; only Synapse writes to it.

We deliberately DO NOT store:
  * TTS speech topics (jarvis/tts/…) — high-frequency, no lasting signal,
  * binary blobs (Ring snapshot JPEGs) — huge, and the snapshot URL already
    rides on the event that references it,
  * oversized payloads (> MAX_PAYLOAD bytes) — truncated to a marker.
"""

from __future__ import annotations

import json
import time

STREAM_KEY = "jarvis:events"
STREAM_MAXLEN = 50_000          # ~ last N events; approximate trimming (cheap)
MAX_PAYLOAD = 8_192             # bytes; larger payloads are truncated

# Topic prefixes we never persist (noise / binary).
_SKIP_PREFIXES = ("jarvis/tts/",)
_SKIP_SUBSTRINGS = ("/image", "snapshot/image", "/rtsp", "/stream")


def should_store(topic: str, raw: bytes) -> bool:
    """True if this bus message is worth keeping in the durable log."""
    if any(topic.startswith(p) for p in _SKIP_PREFIXES):
        return False
    if any(s in topic for s in _SKIP_SUBSTRINGS):
        return False
    # Binary payloads (JPEG etc.) aren't valid UTF-8 — skip them.
    try:
        raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return False
    return True


def classify(topic: str) -> tuple[str, str]:
    """Derive (domain, subject) from a topic for cheap querying/filtering.

    jarvis/agents/finance/report  -> ("agent", "finance")
    jarvis/presence/home          -> ("presence", "home")
    ring/front/motion             -> ("camera", "front")
    """
    parts = [p for p in topic.split("/") if p]
    if not parts:
        return ("unknown", "")
    head = parts[0]
    if head == "jarvis" and len(parts) >= 2:
        section = parts[1]
        subject = parts[2] if len(parts) >= 3 else ""
        return (section, subject)
    if head == "ring":
        subject = parts[1] if len(parts) >= 2 else ""
        return ("camera", subject)
    return (head, parts[1] if len(parts) >= 2 else "")


def record(r, topic: str, raw: bytes) -> bool:
    """Append one bus event to the durable stream. Never raises.

    Returns True if stored, False if filtered/failed. The Redis Stream entry ID
    is millisecond-based, so time-range replay works directly off the IDs.
    """
    try:
        if not should_store(topic, raw):
            return False
        payload = raw.decode("utf-8")
        if len(payload) > MAX_PAYLOAD:
            payload = payload[:MAX_PAYLOAD] + "…[truncated]"
        domain, subject = classify(topic)
        r.xadd(
            STREAM_KEY,
            {
                "topic": topic,
                "domain": domain,
                "subject": subject,
                "payload": payload,
                "ts": str(int(time.time() * 1000)),
            },
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Replay helpers (used by Chronicle, Synapse, "what did I miss")
# ---------------------------------------------------------------------------


def _decode(entry) -> dict:
    """Flatten a (id, fields) stream entry into a plain dict."""
    entry_id, fields = entry
    out = {k.decode() if isinstance(k, bytes) else k:
           v.decode() if isinstance(v, bytes) else v
           for k, v in fields.items()}
    out["id"] = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
    return out


def between(r, start_ms: int, end_ms: int | None = None,
            domains: list[str] | None = None, limit: int = 5000) -> list[dict]:
    """Replay events in [start_ms, end_ms]. Redis Stream IDs are ms-based, so
    ``f"{start_ms}-0"`` .. ``f"{end_ms}-0"`` is an exact time window."""
    end = f"{end_ms}-0" if end_ms is not None else "+"
    try:
        rows = r.xrange(STREAM_KEY, min=f"{start_ms}-0", max=end, count=limit)
    except Exception:
        return []
    events = [_decode(e) for e in rows]
    if domains:
        wanted = set(domains)
        events = [e for e in events if e.get("domain") in wanted]
    return events


def since(r, ago_seconds: int, **kw) -> list[dict]:
    """Events from the last ``ago_seconds`` seconds."""
    start = int((time.time() - ago_seconds) * 1000)
    return between(r, start, **kw)


def parse_payload(event: dict) -> dict:
    """Best-effort JSON decode of an event's payload field."""
    try:
        return json.loads(event.get("payload", "") or "{}")
    except Exception:
        return {}
