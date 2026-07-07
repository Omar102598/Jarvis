"""Calendar/reminder tools for JARVIS."""

import os
from datetime import datetime, timedelta, timezone

import aiohttp
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")


def _eventkit_fallback(status: int) -> str:
    """Next-event data pushed by the iPhone (CalendarManager) via Redis."""
    try:
        import os as _os, json as _json, redis as _redis
        r = _redis.Redis(host=_os.environ.get("REDIS_HOST", "redis"), decode_responses=True)
        raw = r.get("jarvis:calendar:next_event")
        if raw:
            e = _json.loads(raw)
            return (f"(HA calendar unavailable, {status} — from the iPhone instead) "
                    f"Next event: {e.get('title','?')} at {e.get('start','?')}"
                    + (f", {e['location']}" if e.get("location") else ""))
    except Exception:
        pass
    return ("No calendar events available — Home Assistant has no calendars "
            "configured and no iPhone calendar sync is cached. (Not an error; "
            "opening the Jarvis app syncs the next event.)")


@tool
async def get_calendar_events(calendar: str = "calendar.personal", days: int = 7) -> str:
    """Retrieve upcoming calendar events.

    Args:
        calendar: Calendar entity ID in Home Assistant
        days: Number of days to look ahead (default 7)
    """
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)

    async with aiohttp.ClientSession() as session:
        # Discover HA's real calendar entities rather than trusting the
        # hardcoded default (a fresh HA has no 'calendar.personal' → 404).
        try:
            async with session.get(
                f"{HA_URL}/api/calendars",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
            ) as lresp:
                if lresp.status == 200:
                    cals = [c.get("entity_id") for c in await lresp.json()]
                    if cals and calendar not in cals:
                        calendar = cals[0]
                    elif not cals:
                        return _eventkit_fallback(0)
        except Exception:
            pass

        async with session.get(
            f"{HA_URL}/api/calendars/{calendar}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            params={
                "start": now.isoformat() + "Z",
                "end": end.isoformat() + "Z",
            },
        ) as resp:
            if resp.status != 200:
                # Fresh/absent HA has no calendar entities (404). Fall back to
                # the iPhone's EventKit push (jarvis:calendar:next_event) so
                # calendar queries still work without HA calendars configured.
                return _eventkit_fallback(resp.status)
            events = await resp.json()

    if not events:
        return f"No events in the next {days} days."

    lines = []
    for e in events:
        start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "?"))
        summary = e.get("summary", "No title")
        lines.append(f"- {start}: {summary}")
    return "\n".join(lines)


@tool
async def set_reminder(message: str, when: str) -> str:
    """Set a reminder using Home Assistant's built-in timer/notification system.

    Args:
        message: What to remind about
        when: ISO 8601 datetime or relative like 'in 30 minutes'
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HA_URL}/api/services/notify/persistent_notification",
            headers={
                "Authorization": f"Bearer {HA_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "message": f"Reminder: {message} (scheduled for {when})",
                "title": "JARVIS Reminder",
            },
        ) as resp:
            if resp.status == 200:
                return f"Reminder set: '{message}' at {when}"
            return f"Failed to set reminder: {resp.status}"
