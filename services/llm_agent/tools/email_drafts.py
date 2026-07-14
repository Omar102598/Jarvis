"""get_email_drafts — review the reply drafts Hermes prepared.

Hermes (the email agent) drafts replies for action-needed mail and stores them
at ``email:drafts``. This surfaces them so JARVIS can read them out and, on the
user's OK, send one with the existing send_email tool.
"""

from __future__ import annotations

import json
import os

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)


@tool
def get_email_drafts() -> str:
    """Show the reply drafts Hermes prepared for mail that needs a response.

    Use for "show my email drafts", "any emails I need to reply to?". To send
    one, confirm with the user, then call send_email with the draft's to /
    subject / body.
    """
    raw = _r.get("email:drafts")
    if not raw:
        return "No reply drafts right now — Hermes prepares them during inbox triage."
    try:
        drafts = json.loads(raw)
    except Exception:
        return "Draft data is unreadable right now."
    if not drafts:
        return "No reply drafts right now."
    out = [f"You have {len(drafts)} reply draft(s):"]
    for i, d in enumerate(drafts, 1):
        out.append(
            f"\n{i}. To: {d.get('to','')}  |  {d.get('subject','')}"
            f"\n   Why: {d.get('reason','')}"
            f"\n   Draft: {d.get('body','')}"
        )
    out.append("\nSay which to send (I'll confirm the recipient first).")
    return "\n".join(out)
