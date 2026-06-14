"""Communication tools for JARVIS — email (SMTP) and SMS (Twilio).

Email uses standard SMTP (works with Gmail app passwords, Fastmail, etc.):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_DEFAULT_TO

SMS uses Twilio:
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, SMS_DEFAULT_TO
"""

import asyncio
import os
import smtplib
from email.message import EmailMessage

import aiohttp
from langchain_core.tools import tool

# --- Email config ---
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_DEFAULT_TO = os.environ.get("SMTP_DEFAULT_TO", SMTP_USER)

# --- SMS config ---
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")
SMS_DEFAULT_TO = os.environ.get("SMS_DEFAULT_TO", "")


def _send_email_sync(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    return f"Email sent to {to}: \"{subject}\""


@tool
async def send_email(subject: str, body: str, to: str = "") -> str:
    """Send an email.

    Use to email summaries, notes, or messages. If 'to' is omitted, sends to
    the user's default address (SMTP_DEFAULT_TO).

    Args:
        subject: The email subject line.
        body: The email body text.
        to: Recipient address. Defaults to the user's own address.
    """
    if not SMTP_HOST:
        return "Email is not configured. Set SMTP_HOST/SMTP_USER/SMTP_PASSWORD."
    recipient = to.strip() or SMTP_DEFAULT_TO
    if not recipient:
        return "No recipient given and no SMTP_DEFAULT_TO configured."
    try:
        return await asyncio.to_thread(_send_email_sync, recipient, subject, body)
    except Exception as exc:
        return f"Failed to send email: {exc}"


@tool
async def send_sms(message: str, to: str = "") -> str:
    """Send a text message (SMS) via Twilio.

    Use for quick alerts like "text John I'm running late". If 'to' is
    omitted, sends to the user's default number (SMS_DEFAULT_TO).

    Args:
        message: The SMS body.
        to: Recipient phone number in E.164 format (e.g. +14155551234).
    """
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM):
        return "SMS is not configured. Set TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM."
    recipient = to.strip() or SMS_DEFAULT_TO
    if not recipient:
        return "No recipient given and no SMS_DEFAULT_TO configured."

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                auth=aiohttp.BasicAuth(TWILIO_SID, TWILIO_TOKEN),
                data={"From": TWILIO_FROM, "To": recipient, "Body": message},
            ) as resp:
                if resp.status in (200, 201):
                    return f"Text sent to {recipient}."
                err = await resp.text()
                return f"Failed to send SMS: HTTP {resp.status} — {err[:200]}"
    except Exception as exc:
        return f"Failed to send SMS: {exc}"
