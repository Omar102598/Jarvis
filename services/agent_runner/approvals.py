"""Approval Inbox — one queue for every action an agent wants a human yes/no on.

Agents keep proposing more as autonomy grows (grocery orders, bookings, new
routines); before this, every approval was buried in a chat thread. Now any
agent files an approval request, every surface (dashboard, iOS app, the brain
in conversation) can list and resolve it, and the decision is executed in
exactly ONE place — the agent_runner's resolve listener — so surfaces never
race each other.

Data model (Redis):
    jarvis:approvals:pending      hash  id → json record (see request_approval)
    jarvis:approvals:status:{id}  str   "approved" | "denied"  (24 h TTL)
    jarvis:approvals:log          list  resolved records, newest first, capped

Bus topics (MQTT):
    jarvis/approvals/resolve      any surface → executor: {id, decision, by}
    jarvis/approvals/resolved     executor → world (synapse persists = audit)

An approval's optional ``action`` runs ONLY on approve, and is a plain
descriptor rather than a callback so it survives restarts:
    {"type": "mqtt",        "topic": "...", "payload": {...}}
    {"type": "redis_lpush", "key": "...",   "value": "..."}
    {"type": "redis_set",   "key": "...",   "value": "...", "ex": 3600}

Fire-and-forget producers just call request_approval(); producers that must
block on the answer (an agent mid-run) use wait_for_approval().
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone

PENDING_KEY = "jarvis:approvals:pending"
LOG_KEY = "jarvis:approvals:log"
STATUS_TTL_S = 24 * 3600
LOG_KEEP = 100
DEFAULT_TTL_S = int(os.environ.get("APPROVAL_TTL_S", str(24 * 3600)))

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_approval(r, source: str, title: str, text: str, *,
                     action: dict | None = None, media_url: str = "",
                     ttl_s: int = DEFAULT_TTL_S, notify: bool = True) -> str:
    """File a pending approval and (by default) notify the user. Returns id."""
    approval_id = uuid.uuid4().hex[:12]
    record = {
        "id": approval_id,
        "source": source,
        "title": title,
        "text": text,
        "action": action or None,
        "media_url": media_url or None,
        "created": _now_iso(),
        "expires": time.time() + ttl_s,
    }
    r.hset(PENDING_KEY, approval_id, json.dumps(record))
    if notify:
        try:
            from notify import route_notification
            route_notification(source, text[:280], title=f"🟠 Approval — {title}",
                               media_url=media_url, urgency="urgent",
                               dedup_key=f"approval:{approval_id}")
        except Exception:
            pass
    return approval_id


def prune_expired(r) -> int:
    """Lazily drop pending approvals past their expiry. Returns count removed."""
    removed = 0
    try:
        for aid, raw in (r.hgetall(PENDING_KEY) or {}).items():
            try:
                if float(json.loads(raw).get("expires", 0)) < time.time():
                    r.hdel(PENDING_KEY, aid)
                    removed += 1
            except Exception:
                continue
    except Exception:
        pass
    return removed


def list_pending(r) -> list[dict]:
    """All pending approvals, oldest first (the order they should be decided)."""
    prune_expired(r)
    records = []
    for raw in (r.hgetall(PENDING_KEY) or {}).values():
        try:
            records.append(json.loads(raw))
        except Exception:
            continue
    records.sort(key=lambda rec: rec.get("created", ""))
    return records


def get_status(r, approval_id: str) -> str:
    """"approved" | "denied" | "pending" | "unknown" (expired/never existed)."""
    status = r.get(f"jarvis:approvals:status:{approval_id}")
    if status:
        return status
    if r.hexists(PENDING_KEY, approval_id):
        return "pending"
    return "unknown"


async def wait_for_approval(r, approval_id: str, timeout_s: int = 900,
                            poll_s: float = 2.0) -> str:
    """Block (async) until the approval is resolved. Returns the final status —
    "pending" on timeout so callers can treat it as not-approved."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = get_status(r, approval_id)
        if status in ("approved", "denied", "unknown"):
            return status
        await asyncio.sleep(poll_s)
    return "pending"


def _execute_action(action: dict) -> str:
    """Run an approved action descriptor. Returns a short outcome string."""
    kind = (action or {}).get("type", "")
    if kind == "mqtt":
        import paho.mqtt.publish as mqtt_pub
        payload = action.get("payload", {})
        mqtt_pub.single(action["topic"],
                        payload if isinstance(payload, str) else json.dumps(payload),
                        hostname=MQTT_HOST, port=MQTT_PORT)
        return f"published to {action['topic']}"
    if kind in ("redis_lpush", "redis_set"):
        import redis as _redis_mod
        rr = _redis_mod.Redis(host=os.environ.get("REDIS_HOST", "redis"),
                              decode_responses=True)
        value = action.get("value", "")
        if not isinstance(value, str):
            value = json.dumps(value)
        if kind == "redis_lpush":
            rr.lpush(action["key"], value)
        else:
            rr.set(action["key"], value, ex=action.get("ex"))
        return f"{kind} {action['key']}"
    return "no action"


def resolve(r, approval_id: str, decision: str, by: str = "user") -> dict:
    """Executor path — apply a decision. Called ONLY by the agent_runner's
    resolve listener so actions run exactly once (hdel is the atomic claim).

    Returns {ok, status, detail}.
    """
    decision = "approved" if decision in ("approve", "approved", "yes") else "denied"
    raw = r.hget(PENDING_KEY, approval_id)
    if not raw or not r.hdel(PENDING_KEY, approval_id):
        return {"ok": False, "status": get_status(r, approval_id),
                "detail": "not pending (already resolved or expired)"}
    record = json.loads(raw)
    outcome = ""
    if decision == "approved" and record.get("action"):
        try:
            outcome = _execute_action(record["action"])
        except Exception as exc:
            outcome = f"action FAILED: {exc}"
    record.update({"status": decision, "resolved": _now_iso(),
                   "resolved_by": by, "outcome": outcome})
    pipe = r.pipeline()
    pipe.set(f"jarvis:approvals:status:{approval_id}", decision, ex=STATUS_TTL_S)
    pipe.lpush(LOG_KEY, json.dumps(record))
    pipe.ltrim(LOG_KEY, 0, LOG_KEEP - 1)
    pipe.execute()
    try:
        import paho.mqtt.publish as mqtt_pub
        mqtt_pub.single("jarvis/approvals/resolved", json.dumps(record),
                        hostname=MQTT_HOST, port=MQTT_PORT)
    except Exception:
        pass
    return {"ok": True, "status": decision, "detail": outcome or "resolved"}
