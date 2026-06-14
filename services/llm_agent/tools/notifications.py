"""Notification tool for JARVIS — sends notifications via Home Assistant."""

import os

import aiohttp
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")


@tool
async def send_notification(
    message: str,
    title: str = "JARVIS",
    target: str = "notify.mobile_app",
) -> str:
    """Send a push notification to a phone or device.

    Args:
        message: The notification message
        title: Notification title (default: 'JARVIS')
        target: HA notify service, e.g. 'notify.mobile_app' or 'notify.all_devices'
    """
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    # Extract the service name from the target
    service = target.replace("notify.", "") if target.startswith("notify.") else target

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HA_URL}/api/services/notify/{service}",
            headers=headers,
            json={
                "message": message,
                "title": title,
            },
        ) as resp:
            if resp.status == 200:
                return f"Notification sent: '{message}'"
            else:
                error = await resp.text()
                return f"Failed to send notification: {resp.status} - {error}"
