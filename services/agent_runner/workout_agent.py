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

Recomp plan (user:plan:recomp) — when present it OWNS the week: its
week_template fixes which days lift (Mon/Tue/Thu/Fri), recover (Wed), take a
class (Sat) or rest (Sun), and its rolling split (Arnold A/B/C) is assigned to
the lift days in order. The rotation is ROLLING, not weekly: it continues from
the last lift day instead of restarting every Monday. Position = lift days
scheduled since the plan's start + workout:rotation_offset, which
skip_workout_day nudges back by one when a session is missed.

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
RECOMP_KEY = "user:plan:recomp"
OFFSET_KEY = "workout:rotation_offset"
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

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


_RECOMP_PROMPT = """You are Apollo, JARVIS's personal-trainer agent — a sharp, encouraging \
strength coach. Program ONE week for a user on a 12-week body-recomposition plan \
(goal: tighter waist, visible abs, fuller chest and shoulders, keep arm size).

Hard rules:
- The DAY SCHEDULE below is fixed. Do not move, add or drop sessions, and keep \
each lift day's Arnold workout letter and focus exactly as given.
- Arnold A = chest + back (pair them: press/row, fly/pulldown). Arnold B = \
shoulders + arms (biceps/triceps). Arnold C = legs (quads, hamstrings, glutes, calves).
- Lift days: 5-6 exercises, compound first, sets x reps (e.g. "4x8"); progressive \
overload on the main lifts. End each lift day with 1 short ab exercise.
- Recovery day (light cardio + sauna): one easy cardio entry (e.g. 30 min zone-2 \
incline walk), no exercises. Class day: name the class, no exercises. Rest day: empty.
- The user also walks 10,000 steps daily — no extra cardio finishers on lift days.
- Respect recovery data: low readiness, poor sleep or elevated resting HR → cut \
volume ~20% and say so; good recovery → normal or slightly progressive load.
- Keep every "note" under 8 words (form cue or omit — empty string is fine).

Return ONLY valid JSON — no markdown fences, no commentary:
{"days": [{"day": "Monday", "workout": "A", "focus": "Arnold A — chest and back",
  "exercises": [{"name": "Incline dumbbell press", "sets_reps": "4x8", "note": ""}],
  "cardio": "", "class": ""}],
 "weekly_note": "one paragraph: coaching note referencing recovery, the plan week and goal"}"""


def _lift_days(template: dict) -> set[str]:
    return {d for d, v in template.items() if str(v).strip().lower() == "lift"}


def rotation_for_week(r, plan: dict, monday) -> list[dict]:
    """Day-by-day schedule for the week starting `monday` (a date), with the
    rolling split letter assigned to each lift day."""
    from datetime import date as _date
    template = plan.get("week_template") or {}
    workouts = (plan.get("split") or {}).get("workouts") or []
    lift = _lift_days(template)
    try:
        start = _date.fromisoformat(plan["start_date"])
    except Exception:
        start = monday
    try:
        offset = int(r.get(OFFSET_KEY) or 0)
    except Exception:
        offset = 0
    # Lift days scheduled from the start up to (not including) this Monday
    n = 0
    d = start
    while d < monday:
        if d.strftime("%A") in lift:
            n += 1
        d += timedelta(days=1)
    out = []
    for i, name in enumerate(_WEEKDAYS):
        day = monday + timedelta(days=i)
        entry = {"day": name, "date": day.isoformat()}
        if day < start:
            entry["kind"] = "before plan start"
        elif name in lift and workouts:
            w = workouts[(n + offset) % len(workouts)]
            entry.update(kind="lift", workout=w["id"],
                         focus=f"Arnold {w['id']} — {w['focus'].lower()}")
            n += 1
        else:
            entry["kind"] = str(template.get(name, "rest"))
        out.append(entry)
    return out


def _load_json(r, key, default):
    try:
        raw = r.get(key)
        return json.loads(raw) if raw else default
    except Exception:
        return default


def _plan_monday():
    """Monday of the week to program, in the USER's timezone. The scheduled run
    is Sunday evening, which belongs to the week AHEAD — computing this from
    UTC "now" labelled (and rotated) the plan a week behind."""
    import os as _os
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(_os.environ.get("USER_TZ", "America/Chicago"))).date()
    if today.weekday() == 6:   # Sunday → next week
        return today + timedelta(days=1)
    return today - timedelta(days=today.weekday())


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
        readiness = _load_json(self.r, "user:readiness:today", {})

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
                # Fused readiness score (0-100) — the headline recovery signal;
                # scale today's training volume/intensity to it.
                "readiness_score": readiness.get("score"),
                "readiness_band": readiness.get("band"),
                "sleep_hours": health.get("sleep_hours"),
                "hrv_ms": health.get("hrv_ms") or health.get("hrv"),
                "resting_hr": health.get("resting_hr") or health.get("resting_heart_rate"),
                "active_energy_kcal": health.get("active_energy_kcal"),
            },
            "booked_classes": upcoming,
            "class_suggestions": top_suggestions,
            "week_of": _plan_monday().isoformat(),
        }

    async def _plan_week(self) -> str:
        ctx = self._gather_context()
        recomp = _load_json(self.r, RECOMP_KEY, {})
        if recomp.get("week_template"):
            return await self._plan_recomp_week(ctx, recomp)
        user_msg = (
            f"User profile: {json.dumps(ctx['profile'])}\n"
            f"Lifting split (weekday order): {ctx['split']}\n"
            f"Recovery (readiness score is the headline signal — scale volume to it): "
            f"{json.dumps(ctx['recovery'])}\n"
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

    async def _plan_recomp_week(self, ctx: dict, recomp: dict) -> str:
        from datetime import date as _date
        monday = _date.fromisoformat(ctx["week_of"])
        schedule = rotation_for_week(self.r, recomp, monday)
        try:
            week_no = (monday - _date.fromisoformat(recomp["start_date"])).days // 7 + 1
        except Exception:
            week_no = None
        phase = next((p for p in recomp.get("phases", [])
                      if week_no and p["weeks"][0] <= week_no <= p["weeks"][1]), {})
        sched_lines = []
        for e in schedule:
            if e["kind"] == "lift":
                sched_lines.append(f"{e['day']} {e['date']}: {e['focus']} (workout {e['workout']})")
            else:
                sched_lines.append(f"{e['day']} {e['date']}: {e['kind']}")
        booked = ctx["booked_classes"]
        user_msg = (
            f"User: {json.dumps(ctx['profile'])}\n"
            f"Plan week {week_no} of {recomp.get('weeks', 12)}"
            + (f" — phase '{phase.get('name')}': {phase.get('focus')}" if phase else "") + "\n"
            f"DAY SCHEDULE (fixed):\n" + "\n".join(sched_lines) + "\n"
            f"Recovery: {json.dumps(ctx['recovery'])}\n"
            f"Booked classes (from Kai): {booked or 'none booked yet'}\n"
            "Program the week now."
        )
        response = await complete(_RECOMP_PROMPT, user_msg, max_tokens=3500, temperature=0.4)
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
        # The rotation is ours, not the model's: force letters/focus to match.
        by_day = {e["day"]: e for e in schedule}
        for d in plan["days"]:
            e = by_day.get(d.get("day"))
            if e and e["kind"] == "lift":
                d["workout"], d["focus"] = e["workout"], e["focus"]
            elif e:
                d.pop("workout", None)
                d["focus"] = e["kind"]
                if "barry" in e["kind"].lower() and not d.get("class"):
                    d["class"] = e["kind"]
        plan["generated_at"] = datetime.now(timezone.utc).isoformat()
        plan["week_of"] = ctx["week_of"]
        plan["plan_week"] = week_no
        self.r.set(PLAN_KEY, json.dumps(plan))
        lines = [f"Training week {week_no} (week of {ctx['week_of']}):"]
        for d in plan["days"]:
            bits = [f"{d.get('day')}: {d.get('class') or d.get('focus', '')}"]
            ex = d.get("exercises") or []
            if ex:
                bits.append(" — " + "; ".join(
                    f"{x.get('name')} {x.get('sets_reps', '')}".strip() for x in ex[:6]))
            if d.get("cardio"):
                bits.append(f" + {d['cardio']}")
            lines.append("".join(bits))
        if plan.get("weekly_note"):
            lines.append(f"\nCoach's note: {plan['weekly_note']}")
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
