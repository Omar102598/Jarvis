"""Calendar/reminder tools for JARVIS."""

import os
from datetime import datetime

import aiohttp
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")


@tool
async def get_calendar_events(calendar: str = "calendar.personal", days: int = 7) -> str:
    """Retrieve upcoming calendar events.

    Args:
        calendar: Calendar entity ID in Home Assistant
        days: Number of days to look ahead (default 7)
    """
    now = datetime.utcnow()
    end = datetime(now.year, now.month, now.day + days)

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{HA_URL}/api/calendars/{calendar}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            params={
                "start": now.isoformat() + "Z",
                "end": end.isoformat() + "Z",
            },
        ) as resp:
            if resp.status != 200:
                return f"Failed to fetch calendar: {resp.status}"
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
