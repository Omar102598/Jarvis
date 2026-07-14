"""Email triage agent — "Hermes".

Connects to an IMAP inbox (Gmail by default) via the Python stdlib, pulls the
most recent messages, and asks an LLM to produce a concise morning-brief triage:
important / action-needed mail, package & delivery updates, newsletters & promos,
and a count of everything else. No new pip dependencies.
"""

import email
import imaplib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime, parseaddr

from base_agent import BaseAgent
from llm_helper import complete

_DRAFT_SYSTEM = (
    "You are Hermes drafting reply suggestions. From the inbox messages, pick "
    "ONLY those that genuinely need a personal reply from the user (skip "
    "newsletters, promos, automated/no-reply, notifications). For each, write a "
    "concise, professional draft reply in the user's voice — ready to send with "
    "light edits. Return ONLY valid JSON, no markdown:\n"
    '{"drafts": [{"to": "sender@example.com", "subject": "Re: ...", '
    '"reason": "why it needs a reply", "body": "the draft reply"}]}\n'
    "Return an empty list if nothing needs a reply."
)

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")

_SYSTEM_PROMPT = (
    "You are Hermes, JARVIS's email triage agent — a British AI assistant. "
    "Triage the user's recent inbox into a crisp morning brief. Structure it as:\n"
    "1. A 2-sentence morning-brief summary at the very top (lead with this).\n"
    "2. IMPORTANT / ACTION NEEDED — bullet each with a one-line 'why'.\n"
    "3. Package & delivery updates — extract tracking numbers and carriers if present.\n"
    "4. Newsletters & promos — one line each, grouped.\n"
    "5. Everything else — a count only.\n"
    "Be informative but not verbose. Keep the whole report under 400 words."
)


def _decode(value: str) -> str:
    """Decode an RFC 2047 encoded header into a plain string, ignoring errors."""
    if not value:
        return ""
    parts = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            parts.append(text.decode(enc or "utf-8", errors="ignore"))
        else:
            parts.append(text)
    return "".join(parts)


def _body_snippet(msg: email.message.Message) -> str:
    """Return the first 500 chars of the plain-text body, handling multipart."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get(
                "Content-Disposition"
            ):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="ignore")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="ignore")
    return " ".join(body.split())[:500]


class EmailAgent(BaseAgent):
    """Fetches recent inbox mail over IMAP and produces a triage brief."""

    async def run(self) -> str:
        if not IMAP_USER or not IMAP_PASSWORD:
            return (
                "email not configured — add IMAP_USER and IMAP_PASSWORD "
                "(Gmail app password) to .env"
            )

        lookback_hours: int = self.params.get("lookback_hours", 24)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        messages: list[str] = []
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST)
            imap.login(IMAP_USER, IMAP_PASSWORD)
            imap.select("INBOX", readonly=True)

            # Search server-side by date (IMAP SINCE granularity is per-day).
            since = cutoff.strftime("%d-%b-%Y")
            typ, data = imap.search(None, f'(SINCE "{since}")')
            if typ != "OK":
                imap.logout()
                return "Email agent: IMAP search failed."

            ids = data[0].split()
            # Most recent first, cap at 40 messages.
            ids = ids[-40:]
            ids.reverse()

            for msg_id in ids:
                typ, msg_data = imap.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                # Filter precisely by the message Date header against the cutoff.
                date_hdr = msg.get("Date", "")
                try:
                    parsed = parsedate_to_datetime(date_hdr)
                    if parsed is not None:
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        if parsed < cutoff:
                            continue
                except (TypeError, ValueError):
                    pass

                sender = _decode(msg.get("From", ""))
                subject = _decode(msg.get("Subject", "(no subject)"))
                snippet = _body_snippet(msg)
                messages.append(
                    f"From: {sender}\nDate: {date_hdr}\nSubject: {subject}\n"
                    f"Snippet: {snippet}"
                )

            imap.logout()
        except Exception as exc:
            return f"Email agent: could not read inbox ({exc})."

        if not messages:
            return (
                f"Hermes: No new mail in the last {lookback_hours} hours. "
                "Your inbox is clear, sir."
            )

        mail_text = "\n\n---\n\n".join(messages)

        report = await complete(
            system=_SYSTEM_PROMPT,
            user=(
                f"Here are the {len(messages)} most recent inbox messages from the "
                f"last {lookback_hours} hours:\n\n{mail_text}"
            ),
            max_tokens=700,
        )

        # Draft reply suggestions for mail that needs one (stored for review/send).
        if self.params.get("draft_replies", True):
            try:
                n = await self._draft_replies(mail_text)
                if n:
                    report += (f"\n\n✍️ I've drafted {n} reply suggestion"
                               f"{'s' if n != 1 else ''} — say \"show my email drafts\".")
            except Exception as exc:
                self.log_event("finding", f"draft generation failed: {exc}")
        return report

    async def _draft_replies(self, mail_text: str) -> int:
        """Generate reply drafts for action-needed mail; store to Redis email:drafts."""
        resp = await complete(system=_DRAFT_SYSTEM, user=mail_text, max_tokens=900)
        m = re.search(r"\{.*\}", resp or "", re.DOTALL)
        if not m:
            return 0
        drafts = (json.loads(m.group()) or {}).get("drafts", [])
        clean = []
        for d in drafts:
            to = parseaddr(d.get("to", ""))[1] or d.get("to", "")
            if not to or "@" not in to or not d.get("body"):
                continue
            clean.append({
                "to": to,
                "subject": d.get("subject", "Re:"),
                "reason": d.get("reason", ""),
                "body": d.get("body", ""),
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        if clean:
            self.r.set("email:drafts", json.dumps(clean), ex=60 * 60 * 24 * 3)
        return len(clean)
