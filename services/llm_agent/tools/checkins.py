"""Recurring check-ins — schedule a repeating prompt to the brain from chat.

"Check in with me every Sunday about my recomp", "every weekday at 5 remind me
to pack my gym bag", "first of the month, review my subscriptions". The brain
turns the request into a cron (user's local time) + a prompt; at each firing
the agent_runner sends that prompt back to the brain as a proactive request,
which can use every tool (get_recomp_checkin, spending, calendar...).

Two ways in, mirroring ChatGPT's "Tap to set up":
  • the user ASKED for it        → confirmed=True, created immediately
  • Jarvis is SUGGESTING it      → confirmed=False, filed to the Approval Inbox;
                                   one tap (or "yes" in chat) creates it

The agent_runner owns the schedule: it writes ``jarvis:recurring`` on
jarvis/recurring/add (so the approval executor's plain MQTT action works) and
re-syncs its jobs on jarvis/recurring/changed.

Redis:
    jarvis:recurring   hash  id -> {"id","title","cron","prompt","room","created"}
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import paho.mqtt.publish as mqtt_publish
import redis
from langchain_core.tools import tool

from . import cards

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
USER_TZ = os.environ.get("USER_TZ", "America/Chicago")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)

RECURRING_KEY = "jarvis:recurring"
APPROVALS_KEY = "jarvis:approvals:pending"

_FIELD = r"(\*|\d+(-\d+)?)(/\d+)?(,(\d+(-\d+)?)(/\d+)?)*"
_DOW = r"(\*|[0-7a-zA-Z]{1,3}(-[0-7a-zA-Z]{1,3})?)(/\d+)?(,([0-7a-zA-Z]{1,3}(-[0-7a-zA-Z]{1,3})?))*"
_CRON_RE = re.compile(rf"^{_FIELD} {_FIELD} {_FIELD} {_FIELD} {_DOW}$")

_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _describe(cron: str) -> str:
    """Rough English for the common shapes; falls back to the cron itself."""
    try:
        minute, hour, dom, mon, dow = cron.split()
        if minute.isdigit() and hour.isdigit() and dom == "*" and mon == "*":
            h, m = int(hour), int(minute)
            when = f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
            if dow == "*":
                return f"every day at {when}"
            if dow.isdigit():
                return f"every {_DAYS[int(dow)]} at {when}"
            if dow in ("1-5", "mon-fri"):
                return f"every weekday at {when}"
    except Exception:
        pass
    return f"cron '{cron}' ({USER_TZ})"


def _default_room() -> str:
    try:
        return json.loads(_r.get("user:profile") or "{}").get("sentry_greeting_room") or "office"
    except Exception:
        return "office"


@tool
def schedule_recurring_checkin(title: str, cron: str, prompt: str,
                               confirmed: bool = False, room: str = "") -> str:
    """Set up a recurring check-in: at each scheduled time Jarvis receives
    `prompt` and proactively runs it (speaks it and posts it to the phone).

    Args:
        title: short name, e.g. "Weekly recomp check-in".
        cron: 5-field cron in the USER'S LOCAL time ("0 10 * * 0" = Sundays
              10 AM; "0 17 * * 1-5" = weekdays 5 PM; "0 9 1 * *" = 1st of month).
        prompt: what future-you should do at that moment, written as an
                instruction to yourself, naming the tools to call (e.g. "Run the
                weekly recomp check-in: call get_recomp_checkin, summarize the
                trend in 4-5 sentences, propose any calorie change and ask before
                applying it.").
        confirmed: True when the USER explicitly asked for this schedule.
                   False when YOU are suggesting it -- it then goes to the
                   Approval Inbox for a one-tap yes, and you should tell the user
                   it's waiting there (or that they can just say yes).
        room: speaker room to announce in (default: the user's usual room).
    """
    cron = " ".join((cron or "").split())
    if not _CRON_RE.match(cron):
        return (f"'{cron}' isn't a 5-field cron (minute hour day month weekday). "
                "Example: '0 10 * * 0' for Sundays at 10 AM.")
    if not title.strip() or not prompt.strip():
        return "A check-in needs a title and a prompt."
    for raw in (_r.hgetall(RECURRING_KEY) or {}).values():
        try:
            if json.loads(raw).get("title", "").lower() == title.strip().lower():
                return f"A check-in called '{title}' already exists — cancel it first to change it."
        except Exception:
            continue

    rec = {"id": uuid.uuid4().hex[:8], "title": title.strip(), "cron": cron,
           "prompt": prompt.strip(), "room": room or _default_room(),
           "created": datetime.now(timezone.utc).isoformat()}
    when = _describe(cron)

    if confirmed:
        try:
            mqtt_publish.single("jarvis/recurring/add", json.dumps(rec),
                                hostname=MQTT_HOST, port=MQTT_PORT)
        except Exception as exc:
            return f"Couldn't reach the scheduler: {exc}"
        return (f"Done — '{rec['title']}' is set for {when}. Say 'cancel {rec['title']}' to stop it."
                + cards.attach({"type": "action", "title": rec["title"], "subtitle": when,
                                "icon": "calendar.badge.clock", "status": "done",
                                "footer": "Scheduled"}))

    approval_id = uuid.uuid4().hex[:12]
    _r.hset(APPROVALS_KEY, approval_id, json.dumps({
        "id": approval_id, "source": "jarvis", "title": f"Set up: {rec['title']}",
        "text": f"{rec['title']} — {when}. {rec['prompt'][:200]}",
        "action": {"type": "mqtt", "topic": "jarvis/recurring/add", "payload": rec},
        "media_url": None, "created": datetime.now(timezone.utc).isoformat(),
        "expires": time.time() + 7 * 24 * 3600,
    }))
    return (f"Suggested '{rec['title']}' ({when}) — it's in the Approval Inbox "
            f"(id {approval_id}). Tap approve, or the user can just say yes and you "
            "call manage_approvals to approve it."
            + cards.attach({"type": "action", "title": rec["title"], "subtitle": when,
                            "icon": "calendar.badge.clock", "approval_id": approval_id,
                            "status": "pending", "footer": "Tap to set up"}))


@tool
def list_recurring_checkins() -> str:
    """List the recurring check-ins Jarvis runs (from schedule_recurring_checkin)."""
    recs = []
    for raw in (_r.hgetall(RECURRING_KEY) or {}).values():
        try:
            recs.append(json.loads(raw))
        except Exception:
            continue
    if not recs:
        return "No recurring check-ins set up."
    lines = ["Recurring check-ins:"]
    for rec in sorted(recs, key=lambda x: x.get("created", "")):
        last = rec.get("last_run", "")
        lines.append(f"  • {rec['title']} — {_describe(rec['cron'])} (id {rec['id']})"
                     + (f", last ran {last[:16]}" if last else ""))
    return "\n".join(lines)


@tool
def cancel_recurring_checkin(which: str) -> str:
    """Stop a recurring check-in. `which` is its id or (part of) its title."""
    w = (which or "").strip().lower()
    for rid, raw in (_r.hgetall(RECURRING_KEY) or {}).items():
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if w and (w == rid.lower() or w in rec.get("title", "").lower()):
            _r.hdel(RECURRING_KEY, rid)
            try:
                mqtt_publish.single("jarvis/recurring/changed", "{}",
                                    hostname=MQTT_HOST, port=MQTT_PORT)
            except Exception:
                pass
            return f"Cancelled '{rec['title']}'."
    return f"No recurring check-in matches '{which}'."
