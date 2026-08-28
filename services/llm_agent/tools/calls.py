"""make_call — place an outbound phone call that speaks a message, via Twilio.

Useful for reminder calls, simple notifications, or leaving a spoken message.
(Full two-way conversational calls — negotiating a booking — need a realtime
media stream; this covers the "call X and say Y" case.)

Creds-gated on TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER;
reports clearly if unset. ALWAYS confirm the number + message with the user
before calling — this dials a real phone.
"""

from __future__ import annotations

import os
from xml.sax.saxutils import escape

import aiohttp
from langchain_core.tools import tool

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "").strip()


@tool
async def make_call(to: str, message: str) -> str:
    """Place an outbound phone call that speaks a message (Twilio).

    Confirm the number and message with the user FIRST — this rings a real phone.

    Args:
        to: destination number in E.164 (e.g. +15551234567).
        message: what to say on the call.
    """
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM):
        return ("Outbound calling isn't set up — add TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN, and TWILIO_FROM_NUMBER to enable it.")
    to = to.strip()
    if not to.startswith("+") or not message.strip():
        return "I need an E.164 number (like +15551234567) and a message to speak."

    twiml = f"<Response><Say voice=\"Polly.Brian\">{escape(message.strip())}</Say></Response>"
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Calls.json"
    try:
        async with aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(TWILIO_SID, TWILIO_TOKEN)
        ) as s:
            async with s.post(
                url,
                data={"To": to, "From": TWILIO_FROM, "Twiml": twiml},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    return f"Call failed: {body.get('message', resp.status)}"
    except Exception as exc:
        return f"Couldn't place the call: {exc}"
    return f"Calling {to} now — it'll speak your message when answered."
