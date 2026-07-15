"""Chronicle — the daily memory agent.

Every night Chronicle reads the day's lived signals — the durable event stream
(jarvis:events), each agent's reports, camera events, health, spending — and
writes a concise journal entry so JARVIS actually remembers your days:
"what did I do last Tuesday?", "when did the plumber come?", "how many gym days
in June?".

Design notes:
  • Lightweight: lives in agent_runner (no Chroma/embeddings here). Entries are
    written to Redis; the brain's ``recall_journal`` tool lazily indexes them
    into Chroma for semantic search — heavy deps stay where they already are.
  • Catch-up: it summarizes every day since the last entry (up to CATCHUP_MAX),
    so a night with the Mac asleep is filled in on the next run rather than lost.
  • Local-or-API: summarization uses CHRONICLE_LLM_MODEL if set (e.g. a local
    Ollama model via OPENAI_BASE_URL) — flip one env var to go fully on-device
    and $0/night once the local tier lands. Defaults to the normal API model.

Redis keys written:
    chronicle:day:{YYYY-MM-DD}   json {date, summary, signal_count, ts}
    chronicle:entries            list, newest-first, capped (brain indexes these)
    chronicle:last_day           YYYY-MM-DD of the most recent entry
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from base_agent import BaseAgent
from llm_helper import complete

USER_TZ = os.environ.get("USER_TZ", "America/Chicago")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
CHRONICLE_LLM_MODEL = os.environ.get("CHRONICLE_LLM_MODEL", "").strip()

_REVIEW_SYSTEM = (
    "You are JARVIS delivering a warm, concise weekly review to your user. From "
    "the week's journal entries and trend signals, write 4-7 sentences: what the "
    "week held, 1-2 notable trends (sleep, training, spending, habits), a genuine "
    "highlight, and ONE specific suggestion for next week. Second person, no bullet "
    "lists, no preamble."
)
CATCHUP_MAX = int(os.environ.get("CHRONICLE_CATCHUP_MAX", "3"))
ENTRIES_KEEP = 180

_SYSTEM = (
    "You are JARVIS keeping a private daily journal for your user. Given the "
    "day's raw signals (agent activity, camera events, health metrics, spending, "
    "calendar), write a SHORT factual journal entry — 3 to 6 sentences, past "
    "tense, third person ('He …' / 'The house …'). Note what actually happened "
    "and anything notable or worth recalling later. No preamble, no bullet "
    "points, no speculation. If the day was quiet, say so briefly."
)


class ChronicleAgent(BaseAgent):
    async def run(self) -> str:
        if (self.params or {}).get("action") == "weekly_review":
            return await self._weekly_review()
        tz = ZoneInfo(USER_TZ)
        today = datetime.now(tz).date()

        last_raw = self.r.get("chronicle:last_day")
        try:
            last = datetime.strptime(last_raw, "%Y-%m-%d").date() if last_raw else None
        except Exception:
            last = None

        # Days to (re)summarize: everything since the last entry through today,
        # capped so a long gap doesn't fan out into many LLM calls.
        if last is None:
            days = [today]
        else:
            days = []
            d = last
            while d <= today:
                if d >= last:
                    days.append(d)
                d += timedelta(days=1)
            days = days[-CATCHUP_MAX:]

        written = []
        for day in days:
            try:
                summary, n = await self._summarize_day(day, tz)
            except Exception as exc:
                self.log_event("finding", f"chronicle {day} failed: {exc}")
                continue
            if summary:
                self._store(day, summary, n)
                written.append(str(day))

        self.r.set("chronicle:last_day", today.strftime("%Y-%m-%d"))
        if not written:
            return "Chronicle: nothing to journal."
        return f"Chronicle: journaled {', '.join(written)}."

    # ------------------------------------------------------------------ signals

    async def _summarize_day(self, day, tz) -> tuple[str, int]:
        start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
        start_ms = int(start_utc.timestamp() * 1000)
        end_ms = int(end_utc.timestamp() * 1000)

        parts: list[str] = []
        n_signals = 0

        # 1) Event stream — aggregate by domain, surface notable events.
        try:
            rows = self.r.xrange("jarvis:events",
                                 min=f"{start_ms}-0", max=f"{end_ms}-0", count=3000)
        except Exception:
            rows = []
        domain_counts: dict[str, int] = {}
        for _id, fields in rows:
            n_signals += 1
            dom = fields.get("domain", "?")
            domain_counts[dom] = domain_counts.get(dom, 0) + 1
        if domain_counts:
            parts.append("Bus activity: " + ", ".join(
                f"{k}×{v}" for k, v in sorted(domain_counts.items(),
                                              key=lambda x: -x[1])))

        # 2) Agent reports timestamped within the day.
        for key in self.r.scan_iter("agent:*:reports"):
            try:
                for raw in self.r.lrange(key, 0, 9):
                    rep = json.loads(raw)
                    ts = rep.get("timestamp", "")
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if start_utc <= t < end_utc:
                        who = rep.get("agent", "")
                        text = (rep.get("report") or "")[:220]
                        parts.append(f"{who}: {text}")
                        n_signals += 1
            except Exception:
                continue

        # 3) Camera events (Sentry / ring:events).
        try:
            cam = []
            for raw in self.r.lrange("ring:events", 0, 49):
                ev = json.loads(raw)
                ts = ev.get("ts") or ev.get("timestamp") or ""
                t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) \
                    if ts else None
                if t and start_utc <= t < end_utc:
                    cam.append(ev.get("summary") or ev.get("event") or "motion")
            if cam:
                parts.append(f"Camera ({len(cam)}): " + "; ".join(cam[:6]))
                n_signals += len(cam)
        except Exception:
            pass

        # 4) Health snapshot for the day (from history).
        try:
            for raw in self.r.lrange("user:health:history", 0, 29):
                h = json.loads(raw)
                hts = h.get("ts")
                if hts and start_utc.timestamp() <= float(hts) < end_utc.timestamp():
                    bits = []
                    if h.get("sleep_hours"): bits.append(f"{h['sleep_hours']:g}h sleep")
                    if h.get("steps"): bits.append(f"{int(h['steps'])} steps")
                    if h.get("hrv_ms"): bits.append(f"HRV {round(h['hrv_ms'])}ms")
                    if h.get("workout_minutes_today"):
                        bits.append(f"{int(h['workout_minutes_today'])}min training")
                    if bits:
                        parts.append("Health: " + ", ".join(bits))
                        n_signals += 1
                    break
        except Exception:
            pass

        if n_signals == 0:
            return ("A quiet day — no notable activity recorded.", 0)

        signals = "\n".join(parts)[:4000]
        summary = await complete(
            system=_SYSTEM,
            user=f"Date: {day.isoformat()}\n\nSignals:\n{signals}",
            max_tokens=350,
            temperature=0.4,
            model=CHRONICLE_LLM_MODEL,   # "" → default API model
        )
        return (summary.strip(), n_signals)

    # -------------------------------------------------------------------- store

    async def _weekly_review(self) -> str:
        """Compose a weekly review from the last 7 journal entries + trend signals,
        and deliver it as a standalone card."""
        entries = []
        for raw in (self.r.lrange("chronicle:entries", 0, 6) or []):
            try:
                e = json.loads(raw)
                entries.append(f"{e.get('date')}: {e.get('summary','')}")
            except Exception:
                continue

        sleeps: list[float] = []
        workout_days = 0
        for raw in (self.r.lrange("user:health:history", 0, 6) or []):
            try:
                h = json.loads(raw)
                if h.get("sleep_hours"):
                    sleeps.append(float(h["sleep_hours"]))
                if h.get("workout_minutes_today"):
                    workout_days += 1
            except Exception:
                continue
        signals = []
        if sleeps:
            signals.append(f"avg sleep {round(sum(sleeps)/len(sleeps),1)}h over {len(sleeps)} days")
        if workout_days:
            signals.append(f"{workout_days} training day(s)")
        try:
            fin = json.loads(self.r.get("widget:finance:data") or "{}")
            if fin.get("total_spent_30d") is not None:
                signals.append(f"30-day spend ${fin['total_spent_30d']:,.0f}")
        except Exception:
            pass
        try:
            subs = json.loads(self.r.get("finance:subscriptions") or "[]")
            if subs:
                monthly = sum(float(s.get("monthly_est", 0) or 0) for s in subs)
                signals.append(f"subscriptions ~${monthly:,.0f}/mo")
        except Exception:
            pass

        if not entries and not signals:
            return "Chronicle: not enough history for a weekly review yet."

        user = ("Journal entries this week:\n" + "\n".join(entries or ["(none)"]) +
                "\n\nTrend signals: " + (", ".join(signals) or "none"))
        try:
            review = (await complete(system=_REVIEW_SYSTEM, user=user,
                                     max_tokens=500, temperature=0.5,
                                     model=CHRONICLE_LLM_MODEL)).strip()
        except Exception as exc:
            return f"Chronicle weekly review failed: {exc}"

        try:
            import paho.mqtt.publish as mqtt_publish
            mqtt_publish.single(
                "jarvis/surfaces/iphone/push",
                json.dumps({"title": "📅 Your Week in Review", "text": review}),
                hostname=MQTT_HOST, port=MQTT_PORT,
            )
        except Exception as exc:
            self.log_event("finding", f"weekly review push failed: {exc}")
        self.r.set("chronicle:weekly:last", json.dumps({
            "review": review,
            "ts": datetime.now(timezone.utc).isoformat(),
        }))
        return "Weekly review delivered."

    def _store(self, day, summary: str, n: int) -> None:
        date_str = day.isoformat()
        entry = {
            "date": date_str,
            "summary": summary,
            "signal_count": n,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.r.set(f"chronicle:day:{date_str}", json.dumps(entry))
            # De-dup the entries list by date (replace head if same day).
            head = self.r.lindex("chronicle:entries", 0)
            head_day = ""
            if head:
                try:
                    head_day = json.loads(head).get("date", "")
                except Exception:
                    pass
            if head_day == date_str:
                self.r.lset("chronicle:entries", 0, json.dumps(entry))
            else:
                self.r.lpush("chronicle:entries", json.dumps(entry))
            self.r.ltrim("chronicle:entries", 0, ENTRIES_KEEP - 1)
        except Exception as exc:
            self.log_event("finding", f"chronicle store {date_str} failed: {exc}")
