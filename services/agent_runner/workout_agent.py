"""Workout / personal-trainer agent — "Apollo".

Programs the user's training week around three signals:
  • Split preference — from Redis user:profile (workout_split), defaulting to
    the user's lift split: back/biceps → chest/triceps → legs/shoulders,
    repeated twice across the weekdays, weekend class day(s).
  • Kai (ClassPass agent) — booked classes and ranked suggestions
    (classpass:booked / classpass:suggestions). A booked Saturday HIIT class
    becomes the plan's Saturday session and shapes Friday's volume.
  • Recovery — HealthKit snapshot (user:health:latest): sleep, HRV, resting
    HR steer intensity (deload cues on poor recovery).

Actions (params["action"]):
  "week"  (default) — generate the 7-day plan → Redis workout:plan + report
  "today"           — return today's session from the stored plan (regenerates
                      the week first if the plan is missing or stale)
"""

import json
from datetime import datetime, timedelta, timezone

from base_agent import BaseAgent
from llm_helper import complete

PLAN_KEY = "workout:plan"

_DEFAULT_SPLIT = [
    "back/biceps", "chest/triceps", "legs/shoulders",
    "back/biceps", "chest/triceps",
]

_PLAN_PROMPT = """You are Apollo, JARVIS's personal-trainer agent — a sharp, encouraging \
strength coach. Program ONE week (Monday-Sunday) for this user.

Hard rules:
- Weekdays are LIFTING days following the user's split (given below) in order. \
Keep the split's muscle groupings but you may improve exercise selection, order, \
and set/rep schemes for the user's goal.
- Weekend: the user takes studio classes (HIIT/spin etc.). If a booked class is \
listed, that IS the session for that day — program around it (e.g. lighter legs \
the day before a HIIT class). If none is booked but suggestions exist, name the \
top suggestion as the recommended class. Leave one weekend day for full rest or \
active recovery unless a class is booked on both.
- Sprinkle 2-3 short cardio finishers (10-15 min) across the weekday lifts, \
placed where they least hurt recovery.
- Respect recovery data: poor sleep or elevated resting HR → cut volume ~20% \
and note it; good recovery → normal or slightly progressive load.
- 4-6 exercises per lift day with sets x reps (e.g. "4x8"), compound first.
- Keep every "note" under 8 words (form cue or omit — empty string is fine).

Return ONLY valid JSON — no markdown fences, no commentary:
{"days": [{"day": "Monday", "focus": "back/biceps",
  "exercises": [{"name": "Barbell row", "sets_reps": "4x8", "note": ""}],
  "cardio": "12 min incline walk" or "",
  "class": "" or "Barry's HIIT 9:40 AM (booked)"}],
 "weekly_note": "one-paragraph coaching note referencing recovery + goal"}"""


def _load_json(r, key, default):
    try:
        raw = r.get(key)
        return json.loads(raw) if raw else default
    except Exception:
        return default


class WorkoutAgent(BaseAgent):
    """Apollo — weekly programming + daily session readout."""

    async def run(self) -> str:
        action = (self.params or {}).get("action", "week")
        if action == "today":
            return await self._today()
        return await self._plan_week()

    # ------------------------------------------------------------------

    def _gather_context(self) -> dict:
        profile = _load_json(self.r, "user:profile", {})
        health = _load_json(self.r, "user:health:latest", {})

        split = profile.get("workout_split") or _DEFAULT_SPLIT
        goal = profile.get("goal", "cutting")

        booked = _load_json(self.r, "classpass:booked", [])
        suggestions = _load_json(self.r, "classpass:suggestions", [])
        # Only classes in the coming 8 days matter for this week's plan
        upcoming = []
        now = datetime.now(timezone.utc)
        for b in booked if isinstance(booked, list) else []:
            when = str(b.get("start_time") or b.get("time") or b.get("day") or "")
            label = b.get("name") or b.get("class_name") or "class"
            studio = b.get("studio") or b.get("venue") or ""
            upcoming.append(f"{label} @ {studio} — {when}")
        top_suggestions = []
        for s in (suggestions if isinstance(suggestions, list) else [])[:3]:
            label = s.get("name") or s.get("class_name") or "class"
            studio = s.get("studio") or s.get("venue") or ""
            when = str(s.get("start_time") or s.get("time") or s.get("day") or "")
            top_suggestions.append(f"{label} @ {studio} — {when}")

        return {
            "profile": {
                "goal": goal,
                "weight_lbs": profile.get("weight_lbs", 200),
                "age": profile.get("age", 25),
                "activity_level": profile.get("activity_level", "moderately_active"),
            },
            "split": split,
            "recovery": {
                "sleep_hours": health.get("sleep_hours"),
                "hrv_ms": health.get("hrv_ms") or health.get("hrv"),
                "resting_hr": health.get("resting_hr") or health.get("resting_heart_rate"),
                "active_energy_kcal": health.get("active_energy_kcal"),
            },
            "booked_classes": upcoming,
            "class_suggestions": top_suggestions,
            "week_of": (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d"),
        }

    async def _plan_week(self) -> str:
        ctx = self._gather_context()
        user_msg = (
            f"User profile: {json.dumps(ctx['profile'])}\n"
            f"Lifting split (weekday order): {ctx['split']}\n"
            f"Recovery (HealthKit): {json.dumps(ctx['recovery'])}\n"
            f"Booked classes this week (from Kai/ClassPass): "
            f"{ctx['booked_classes'] or 'none booked yet'}\n"
            f"Kai's top class suggestions: {ctx['class_suggestions'] or 'none'}\n"
            f"Week of: {ctx['week_of']}\n\n"
            "Program the week now."
        )
        response = await complete(_PLAN_PROMPT, user_msg, max_tokens=3500, temperature=0.4)

        plan = None
        try:
            cleaned = response.replace("```json", "").replace("```", "")
            start, end = cleaned.find("{"), cleaned.rfind("}") + 1
            if start >= 0:
                plan = json.loads(cleaned[start:end])
        except Exception:
            plan = None
        if not plan or not isinstance(plan.get("days"), list):
            return f"Apollo: couldn't produce a structured plan this run. Raw output:\n{response[:800]}"

        plan["generated_at"] = datetime.now(timezone.utc).isoformat()
        plan["week_of"] = ctx["week_of"]
        self.r.set(PLAN_KEY, json.dumps(plan))

        lines = [f"Training week (week of {ctx['week_of']}):"]
        for d in plan["days"]:
            focus = d.get("class") or d.get("focus", "")
            bits = [f"{d.get('day')}: {focus}"]
            ex = d.get("exercises") or []
            if ex:
                bits.append(" — " + "; ".join(
                    f"{e.get('name')} {e.get('sets_reps', '')}".strip() for e in ex[:6]
                ))
            if d.get("cardio"):
                bits.append(f" + {d['cardio']}")
            lines.append("".join(bits))
        note = plan.get("weekly_note", "")
        if note:
            lines.append(f"\nCoach's note: {note}")
        return "\n".join(lines)

    async def _today(self) -> str:
        plan = _load_json(self.r, PLAN_KEY, {})
        generated = plan.get("generated_at", "")
        stale = True
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(generated)
            stale = age > timedelta(days=7)
        except Exception:
            pass
        if not plan.get("days") or stale:
            await self._plan_week()
            plan = _load_json(self.r, PLAN_KEY, {})

        # User-local weekday, not container UTC (evenings differ by one day)
        try:
            from zoneinfo import ZoneInfo
            import os as _os
            today = datetime.now(ZoneInfo(_os.environ.get("USER_TZ", "America/Chicago"))).strftime("%A")
        except Exception:
            today = datetime.now().strftime("%A")
        day = next((d for d in plan.get("days", []) if d.get("day") == today), None)
        if not day:
            return f"Apollo: no session found for {today} — ask me to plan the week."

        lines = [f"Today ({today}): {day.get('class') or day.get('focus', 'session')}"]
        for e in day.get("exercises") or []:
            note = f" — {e['note']}" if e.get("note") else ""
            lines.append(f"  • {e.get('name')} {e.get('sets_reps', '')}{note}")
        if day.get("cardio"):
            lines.append(f"  Finisher: {day['cardio']}")
        return "\n".join(lines)
