"""Echo — the habit-mining agent.

Nightly, Echo reads the last HABIT_WINDOW_DAYS of the durable event stream
(jarvis:events — the synapse substrate Chronicle also reads) plus recent agent
reports, compresses them into rhythm histograms (what happens, at which hour,
on which weekday), and asks the LLM for at most MAX_SUGGESTIONS *new* insights:

    routine  — "you do X at Y o'clock most days; want it automated?"
    nudge    — "you usually do X but haven't this week"

Every suggestion is filed in the Approval Inbox rather than pushed as chat
noise: approving a routine records it in ``habits:routines:approved`` (wiring
an approved routine into ambient triggers / scenes is a follow-up Forge task),
approving a nudge simply acknowledges it. Suggestions are deduped for 90 days
via ``habits:suggested`` so Echo never re-pitches a rejected idea.

Redis:
    habits:suggested          hash  norm(title) → iso ts        (dedupe, 90 d)
    habits:routines:approved  list  approved routine suggestions (via action)
    habits:last_analysis      json  {ts, histogram_summary, suggestions}
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import approvals
from base_agent import BaseAgent
from llm_helper import complete

USER_TZ = os.environ.get("USER_TZ", "America/Chicago")
WINDOW_DAYS = int(os.environ.get("HABIT_WINDOW_DAYS", "14"))
MAX_SUGGESTIONS = int(os.environ.get("HABIT_MAX_SUGGESTIONS", "2"))
DEDUP_DAYS = 90

_SYSTEM = (
    "You are Echo, JARVIS's habit-mining analyst. You receive activity "
    "histograms from a smart-home/assistant event bus (counts of event kinds "
    "by local hour-of-day and weekday over the last {days} days) plus recent "
    "agent-report themes and previously made suggestions.\n\n"
    "Find at most {max_n} NEW, genuinely useful patterns. Two kinds:\n"
    '  "routine" — a strongly repeated time-anchored behavior worth automating '
    "(only if it repeats on most days at a consistent hour).\n"
    '  "nudge" — a clear, recent DEVIATION from the user\'s own baseline worth '
    "a gentle heads-up.\n\n"
    "Be conservative: no pattern, no suggestion — an empty list is a good "
    "answer. Never repeat or rephrase a previous suggestion.\n\n"
    "Reply with ONLY a JSON array (no prose): "
    '[{{"kind": "routine"|"nudge", "title": "<≤8 words>", '
    '"text": "<1-2 sentences, second person, concrete times/counts>", '
    '"trigger_hint": "<for routines: machine-ish hint like '
    "'weekdays 23:00 lights off' — else empty>\"}}]"
)


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


class HabitAgent(BaseAgent):
    async def run(self) -> str:
        tz = ZoneInfo(USER_TZ)
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - WINDOW_DAYS * 86400 * 1000

        # ------------------------------------------------ 1) rhythm histograms
        try:
            rows = self.r.xrange("jarvis:events",
                                 min=f"{start_ms}-0", max=f"{now_ms}-0",
                                 count=20000)
        except Exception:
            rows = []
        if len(rows) < 30:
            return ("Echo: not enough event history yet "
                    f"({len(rows)} events in {WINDOW_DAYS}d) — skipping.")

        # {domain/subject: {(weekday, hour): count}} in LOCAL time
        hist: dict[str, dict[tuple[int, int], int]] = {}
        for _id, fields in rows:
            try:
                ts_ms = int(fields.get("ts") or _id.split("-")[0])
                lt = datetime.fromtimestamp(ts_ms / 1000, tz)
                key = f"{fields.get('domain','?')}/{fields.get('subject','')}"
                hist.setdefault(key, {})[(lt.weekday(), lt.hour)] = \
                    hist.setdefault(key, {}).get((lt.weekday(), lt.hour), 0) + 1
            except Exception:
                continue

        # Compress: per signal, the top hours (aggregated over weekdays) that
        # hold most of its activity — small enough to prompt with.
        lines = []
        for key, buckets in sorted(hist.items(),
                                   key=lambda kv: -sum(kv[1].values()))[:20]:
            total = sum(buckets.values())
            if total < 5:
                continue
            by_hour: dict[int, int] = {}
            weekdays = set()
            for (wd, hr), n in buckets.items():
                by_hour[hr] = by_hour.get(hr, 0) + n
                weekdays.add(wd)
            top_hours = sorted(by_hour.items(), key=lambda x: -x[1])[:3]
            hours_txt = ", ".join(f"{h:02d}h×{n}" for h, n in top_hours)
            lines.append(f"{key}: {total} events, {len(weekdays)}/7 weekdays, "
                         f"peak hours [{hours_txt}]")
        self.log_event("thinking", f"histograms over {len(rows)} events, "
                                   f"{len(lines)} active signals")

        # ------------------------------------------------ 2) agent-report themes
        report_bits = []
        for key in self.r.scan_iter("agent:*:reports"):
            try:
                raw = self.r.lindex(key, 0)
                if raw:
                    rep = json.loads(raw)
                    report_bits.append(
                        f"{rep.get('agent','?')}: {(rep.get('report') or '')[:120]}")
            except Exception:
                continue

        # ------------------------------------------------ 3) previous suggestions
        prior = list((self.r.hgetall("habits:suggested") or {}).keys())

        user_msg = (
            f"Local timezone: {USER_TZ}. Today: "
            f"{datetime.now(ZoneInfo(USER_TZ)).strftime('%A %Y-%m-%d')}.\n\n"
            f"Activity histograms (last {WINDOW_DAYS}d):\n" + "\n".join(lines) +
            "\n\nLatest agent-report themes:\n" + "\n".join(report_bits[:12]) +
            "\n\nPreviously suggested (do NOT repeat): " +
            (", ".join(prior[-40:]) or "none")
        )

        raw_reply = await complete(
            system=_SYSTEM.format(days=WINDOW_DAYS, max_n=MAX_SUGGESTIONS),
            user=user_msg, max_tokens=600, temperature=0.3,
        )
        try:
            match = re.search(r"\[.*\]", raw_reply, re.DOTALL)
            suggestions = json.loads(match.group(0)) if match else []
        except Exception:
            return f"Echo: couldn't parse suggestions ({raw_reply[:120]}…)"

        # ------------------------------------------------ 4) file approvals
        filed = []
        for s in suggestions[:MAX_SUGGESTIONS]:
            title = str(s.get("title", "")).strip()
            text = str(s.get("text", "")).strip()
            kind = s.get("kind", "nudge")
            if not title or not text or self.r.hexists("habits:suggested",
                                                       _norm(title)):
                continue
            action = None
            if kind == "routine":
                action = {"type": "redis_lpush", "key": "habits:routines:approved",
                          "value": {"title": title, "text": text,
                                    "trigger_hint": s.get("trigger_hint", ""),
                                    "approved": datetime.now(timezone.utc).isoformat()}}
            approvals.request_approval(
                self.r, "Echo",
                title if kind == "routine" else f"Nudge: {title}",
                text + (" — Approve to save this as a routine (I'll wire up the "
                        "automation next)." if kind == "routine" else ""),
                action=action, ttl_s=3 * 86400)
            self.r.hset("habits:suggested", _norm(title),
                        datetime.now(timezone.utc).isoformat())
            filed.append(f"{kind}: {title}")
            self.log_event("finding", f"suggested {kind}: {title}")

        # Dedup memory decay: drop entries older than DEDUP_DAYS.
        try:
            cutoff = time.time() - DEDUP_DAYS * 86400
            for norm_title, ts in (self.r.hgetall("habits:suggested") or {}).items():
                try:
                    if datetime.fromisoformat(ts).timestamp() < cutoff:
                        self.r.hdel("habits:suggested", norm_title)
                except Exception:
                    continue
        except Exception:
            pass

        self.r.set("habits:last_analysis", json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "signals": len(lines), "events": len(rows),
            "suggestions": suggestions,
        }))
        if not filed:
            return f"Echo: analyzed {len(rows)} events — no new patterns worth suggesting."
        return ("Echo: filed for approval — " + "; ".join(filed) +
                ". Decide on the dashboard, in the app, or just tell me.")
