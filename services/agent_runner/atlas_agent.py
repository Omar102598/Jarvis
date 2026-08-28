"""Atlas — unified health overview.

Apollo (training), Sage (nutrition), the readiness score, and the HealthKit
pipeline each keep their own slice of Redis; nobody fuses them. Atlas reads
them all, computes deterministic week-over-week trends in Python (no LLM math),
then asks the LLM for a short synthesis + ONE recommendation grounded in those
numbers.

Outputs:
    health:atlas:summary   json {ts, summary, metrics}   (brain tool reads this)
    widget:health:data     json for the dashboard Health widget

Weekly Monday morning + on-demand ("Atlas, how's my health trending?" →
get_health_overview → trigger if stale).

Inputs (all optional — Atlas reports on whatever exists):
    user:health:history        HealthKit snapshots (sleep_hours, steps, hrv_ms,
                               resting_hr, workout_minutes_today, ts)
    user:readiness:today       fused readiness score (gateway computes)
    workout:plan               Apollo's current week
    nutrition:totals:{day}     Sage's daily macro rollups
    classpass:booked           Kai's booked classes
    user:profile               goal / protein target for context
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from base_agent import BaseAgent
from llm_helper import complete

USER_TZ = os.environ.get("USER_TZ", "America/Chicago")

_SYSTEM = (
    "You are Atlas, JARVIS's health analyst, writing a short weekly health "
    "overview for the user. You get pre-computed trend metrics (this week vs "
    "last week) — do NOT recompute or invent numbers. Write 3-5 sentences, "
    "second person, warm but direct: the overall picture, the one trend most "
    "worth attention (good or bad), and ONE specific recommendation grounded "
    "in the numbers you were given. No bullet lists, no preamble."
)


def _avg(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


class AtlasAgent(BaseAgent):
    async def run(self) -> str:
        tz = ZoneInfo(USER_TZ)
        now = datetime.now(timezone.utc)

        # -------------------------------------------- HealthKit history buckets
        this_week: list[dict] = []
        last_week: list[dict] = []
        for raw in (self.r.lrange("user:health:history", 0, 59) or []):
            try:
                h = json.loads(raw)
                ts = float(h.get("ts", 0) or 0)
                age_d = (now.timestamp() - ts) / 86400
                if age_d <= 7:
                    this_week.append(h)
                elif age_d <= 14:
                    last_week.append(h)
            except Exception:
                continue

        def metric(field):
            cur = _avg([h.get(field) for h in this_week])
            prev = _avg([h.get(field) for h in last_week])
            delta = round(cur - prev, 1) if (cur is not None and prev is not None) else None
            return cur, delta

        sleep_avg, sleep_delta = metric("sleep_hours")
        steps_avg, steps_delta = metric("steps")
        hrv_avg, hrv_delta = metric("hrv_ms")
        rhr_avg, rhr_delta = metric("resting_hr")
        training_days = sum(1 for h in this_week
                            if float(h.get("workout_minutes_today", 0) or 0) >= 20)

        # -------------------------------------------- Sage nutrition (7 days)
        protein_days: list[float] = []
        logged_days = 0
        for i in range(7):
            day = (datetime.now(tz).date() - timedelta(days=i)).isoformat()
            totals = self.r.hgetall(f"nutrition:totals:{day}") or {}
            if totals:
                logged_days += 1
                try:
                    protein_days.append(float(totals.get("protein_g", 0) or 0))
                except Exception:
                    pass
        protein_avg = _avg(protein_days)

        # -------------------------------------------- context signals
        try:
            readiness = json.loads(self.r.get("user:readiness:today") or "{}")
            readiness_score = readiness.get("score")
        except Exception:
            readiness_score = None
        profile = {}
        try:
            profile = json.loads(self.r.get("user:profile") or "{}")
        except Exception:
            pass
        booked = self.r.llen("classpass:booked") or 0
        has_plan = bool(self.r.get("workout:plan"))

        metrics = {
            "sleep_avg_h": sleep_avg, "sleep_delta_h": sleep_delta,
            "steps_avg": steps_avg, "steps_delta": steps_delta,
            "hrv_avg_ms": hrv_avg, "hrv_delta_ms": hrv_delta,
            "resting_hr_avg": rhr_avg, "resting_hr_delta": rhr_delta,
            "training_days_7d": training_days,
            "nutrition_logged_days_7d": logged_days,
            "protein_avg_g": protein_avg,
            "readiness_today": readiness_score,
            "classes_booked": booked,
            "workout_plan_set": has_plan,
        }
        have_data = any(v is not None and v not in (0, False)
                        for v in metrics.values())
        if not have_data:
            return ("Atlas: no health data yet — open the Jarvis iOS app so "
                    "HealthKit syncs, and log meals with Sage.")

        goal = profile.get("goal", "")
        protein_target = None
        try:
            protein_target = float(profile.get("weight_lbs", 0) or 0) * \
                float(profile.get("protein_goal_g_per_lb", 1.0) or 1.0)
        except Exception:
            pass

        lines = [f"{k}: {v}" for k, v in metrics.items() if v is not None]
        if goal:
            lines.append(f"stated goal: {goal}")
        if protein_target:
            lines.append(f"protein target: {round(protein_target)}g/day")
        self.log_event("thinking",
                       f"fused {len(this_week)} snapshots, {logged_days} nutrition days")

        summary = (await complete(
            system=_SYSTEM,
            user="This week vs last week:\n" + "\n".join(lines),
            max_tokens=350, temperature=0.4,
        )).strip()

        self.r.set("health:atlas:summary", json.dumps({
            "ts": now.isoformat(), "summary": summary, "metrics": metrics,
        }))
        self.r.set("widget:health:data", json.dumps({
            **metrics,
            "updated": now.strftime("%b %d, %H:%M UTC"),
        }))
        return f"Atlas health overview: {summary}"
