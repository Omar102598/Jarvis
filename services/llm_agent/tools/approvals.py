"""Approval Inbox tool — list/approve/deny pending agent approvals in chat.

The Approval Inbox (Redis ``jarvis:approvals:pending``) is where background
agents file actions that need a human yes/no (a proposed routine from Echo, a
grocery order, a booking). Every surface can decide; the decision itself is
executed once, by the agent_runner's ``jarvis/approvals/resolve`` listener —
this tool only reads the queue and publishes the decision to the bus.
"""

from __future__ import annotations

import json
import os
import time

import paho.mqtt.publish as mqtt_publish
import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

_r = redis.Redis(host=REDIS_HOST, decode_responses=True)

PENDING_KEY = "jarvis:approvals:pending"


def _pending() -> list[dict]:
    records = []
    now = time.time()
    for raw in (_r.hgetall(PENDING_KEY) or {}).values():
        try:
            rec = json.loads(raw)
            if float(rec.get("expires", 0)) >= now:
                records.append(rec)
        except Exception:
            continue
    records.sort(key=lambda rec: rec.get("created", ""))
    return records


@tool
def manage_approvals(action: str = "list", approval_id: str = "") -> str:
    """List or decide pending approvals from background agents.

    Use when the user asks "what's pending / waiting on me?", or says
    "approve that", "approve the grocery order", "deny Echo's suggestion".

    Args:
        action: "list" (default), "approve", or "deny".
        approval_id: id from the list. For approve/deny you may leave it empty
            ONLY when exactly one approval is pending; otherwise list first and
            confirm with the user which one they mean.
    """
    action = action.strip().lower()
    pending = _pending()

    if action == "list":
        if not pending:
            return "Nothing is waiting for approval."
        lines = []
        for rec in pending:
            lines.append(f"[{rec['id']}] {rec.get('source','?')} — "
                         f"{rec.get('title','')}: {rec.get('text','')[:160]}")
        return "Pending approvals (oldest first):\n" + "\n".join(lines)

    if action not in ("approve", "deny"):
        return "Unknown action — use list, approve, or deny."

    target = None
    if approval_id:
        target = next((rec for rec in pending if rec["id"] == approval_id), None)
        if target is None:
            status = _r.get(f"jarvis:approvals:status:{approval_id}") or "unknown"
            return f"Approval {approval_id} isn't pending (status: {status})."
    elif len(pending) == 1:
        target = pending[0]
    elif not pending:
        return "Nothing is waiting for approval."
    else:
        return ("Multiple approvals are pending — list them and ask the user "
                "which one to decide.")

    try:
        mqtt_publish.single("jarvis/approvals/resolve", json.dumps({
            "id": target["id"], "decision": action, "by": "brain",
        }), hostname=MQTT_HOST, port=MQTT_PORT)
    except Exception as exc:
        return f"Couldn't reach the approvals bus: {exc}"
    verb = "Approved" if action == "approve" else "Denied"
    return (f"{verb}: {target.get('source','?')} — {target.get('title','')}. "
            "The action (if any) runs momentarily.")
