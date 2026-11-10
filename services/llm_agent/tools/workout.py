"""Workout tools — talk to Apollo, the personal-trainer agent."""

import json
import os
from datetime import datetime

import paho.mqtt.publish as mqtt_publish
import redis
from langchain_core.tools import tool

from . import cards

_r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

_PLAN_KEY = "workout:plan"


def _trigger(action: str) -> str | None:
    try:
        mqtt_publish.single(
            "jarvis/agents/workout/trigger",
            json.dumps({"params": {"action": action}}),
            hostname=MQTT_HOST,
            port=MQTT_PORT,
        )
        return None
    except Exception as exc:
        return f"Could not reach Apollo: {exc}"


@tool
def get_todays_workout() -> str:
    """Get today's workout session from Apollo's weekly plan.

    Use when the user asks "what's today's workout?", "what am I training
    today?", or "what did Apollo program for me?". Reads the stored plan;
    if it's missing or stale, tells the user to ask Apollo to plan the week.
    """
    raw = _r.get(_PLAN_KEY)
    if not raw:
        err = _trigger("today")
        return err or (
            "No plan on file yet, sir — I've asked Apollo to program one now. "
            "Ask me again in a minute."
        )
    try:
        plan = json.loads(raw)
    except Exception:
        return "The stored plan looks corrupted — ask Apollo to re-plan the week."

    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(os.environ.get("USER_TZ", "America/Chicago"))).strftime("%A")
    except Exception:
        today = datetime.now().strftime("%A")
    day = next((d for d in plan.get("days", []) if d.get("day") == today), None)
    if not day:
        return f"Apollo's plan has no session for {today}. Say 'plan my workout week' to refresh."

    lines = [f"{today} — {day.get('class') or day.get('focus', 'session')} "
             f"(week of {plan.get('week_of', '?')})"]
    for e in day.get("exercises") or []:
        note = f" — {e['note']}" if e.get("note") else ""
        lines.append(f"  • {e.get('name')} {e.get('sets_reps', '')}{note}")
    if day.get("cardio"):
        lines.append(f"  Finisher: {day['cardio']}")
    rows = [[e.get("name", ""), e.get("sets_reps", ""), e.get("note", "")]
            for e in day.get("exercises") or []]
    if day.get("cardio"):
        rows.append(["Cardio", day["cardio"], ""])
    card = ""
    if rows:
        card = cards.attach({"type": "table",
                             "title": f"{today} · {day.get('class') or day.get('focus', '')}",
                             "columns": ["Exercise", "Sets × reps", "Cue"], "rows": rows})
    return "\n".join(lines) + card


@tool
def plan_workout_week() -> str:
    """Ask Apollo to (re)program the full training week now.

    Use when the user says "plan my workout week", "update my program",
    "Apollo, build my split", or after their schedule/recovery changes.
    Apollo weighs the user's split preference, booked ClassPass classes
    (from Kai), and HealthKit recovery, then stores the plan.
    """
    err = _trigger("week")
    return err or (
        "Apollo is programming your week now, sir — split, booked classes, and "
        "recovery all considered. Ask for the plan or today's workout in a minute."
    )


@tool
def get_workout_plan() -> str:
    """Show Apollo's full weekly training plan.

    Use when the user asks "what's my workout plan?", "show me the week",
    or "what does my split look like?".
    """
    raw = _r.get(_PLAN_KEY)
    if not raw:
        return "No plan on file yet, sir. Say 'plan my workout week' and Apollo will build one."
    try:
        plan = json.loads(raw)
    except Exception:
        return "The stored plan looks corrupted — ask Apollo to re-plan the week."

    lines = [f"Training week (week of {plan.get('week_of', '?')}):"]
    for d in plan.get("days", []):
        head = d.get("class") or d.get("focus", "")
        lines.append(f"\n{d.get('day')}: {head}")
        for e in d.get("exercises") or []:
            lines.append(f"  • {e.get('name')} {e.get('sets_reps', '')}")
        if d.get("cardio"):
            lines.append(f"  Finisher: {d['cardio']}")
    if plan.get("weekly_note"):
        lines.append(f"\nCoach's note: {plan['weekly_note']}")
    rows = []
    for d in plan.get("days", []):
        n = len(d.get("exercises") or [])
        rows.append([d.get("day", "")[:3], d.get("class") or d.get("focus", ""),
                     f"{n} exercises" if n else (d.get("cardio") or "")])
    title = (f"Week {plan['plan_week']} training" if plan.get("plan_week")
             else f"Week of {plan.get('week_of', '?')}")
    card = cards.attach({"type": "table", "title": title, "columns": ["Day", "Training", ""],
                         "rows": rows, "footer": "Rolling split — continues where you left off."
                         if plan.get("plan_week") else ""})
    return "\n".join(lines) + card


@tool
def skip_workout_day() -> str:
    """The user missed (or will miss) today's lifting session: shift the rolling
    split back one step so the NEXT lift day does the workout they skipped,
    then re-plan the week.

    Use for "I skipped the gym today", "I'm not lifting today, push it back".
    The split is rolling (A → B → C) and never restarts on Monday, so a
    missed day must move the rotation rather than drop a workout.
    """
    try:
        offset = _r.decr("workout:rotation_offset")
    except Exception as exc:
        return f"Couldn't shift the rotation: {exc}"
    err = _trigger("week")
    return err or (f"Rotation shifted (offset now {offset}) — your next lift day picks up "
                   "the workout you missed. Apollo is re-planning the week.")


@tool
def set_workout_split(workouts: str = "", order: str = "", start_with: str = "",
                      name: str = "", reason: str = "") -> str:
    """Change the user's rolling lifting split (what each workout letter trains,
    the rotation order, or which workout the next lift day does), then have
    Apollo re-plan the week. Lift/rest/class DAYS don't change — only what the
    lift days train.

    Use for "change my split to back and triceps / chest and biceps / legs and
    shoulders", "swap B and C", "start with legs next session".

    Args:
        workouts: letters with their muscle groups, e.g.
                  "A=back and triceps; B=chest and biceps; C=legs and shoulders".
                  Edits existing letters; new letters are appended.
        order: the full rotation order, e.g. "A, C, B" (letters left out are
               dropped). Empty = keep the current order.
        start_with: letter the NEXT lift day should do (e.g. "A" to start the
                    new split fresh). Empty = continue the rotation in place.
        name: optional name for the split, e.g. "Push/pull/legs".
        reason: why it changed (kept in the plan's history).
    """
    from datetime import date
    from . import recomp
    from .workout_split import (apply_split_edit, describe, lift_days, next_lift_day,
                                offset_for, parse_order, parse_workouts)

    plan = recomp.load_plan()
    split = plan.get("split") or {}
    current = split.get("workouts") or []
    template = plan.get("week_template") or {}
    if not current or not template:
        return ("There's no rolling split on the plan to edit — the user's training "
                "isn't on the recomp plan yet.")
    if not (workouts or order or start_with or name):
        return f"Current split ({split.get('name', 'rolling split')}): {describe(current)}."
    try:
        new = apply_split_edit(current, parse_workouts(workouts), parse_order(order))
    except ValueError as exc:
        return f"Couldn't change the split: {exc}"

    lift = lift_days(template)
    try:
        start = date.fromisoformat(plan["start_date"])
    except Exception:
        start = recomp.local_today()
    next_day = next_lift_day(max(recomp.local_today(), start), lift)

    ids = [w["id"] for w in new]
    offset_note = ""
    if start_with:
        letter = start_with.strip().upper()[:1]
        if letter not in ids:
            return f"'{start_with}' isn't in the new split ({', '.join(ids)})."
        if next_day:
            off = offset_for(next_day, start, lift, ids.index(letter), len(new))
            _r.set("workout:rotation_offset", off)
            offset_note = f" {next_day.strftime('%A')} does {letter}, then it keeps rolling."

    old_desc = describe(current)
    split["workouts"] = new
    if name.strip():
        split["name"] = name.strip()
    elif workouts and split.get("name", "").lower().startswith("arnold"):
        split["name"] = "Custom split"   # it's no longer Arnold's grouping
    plan["split"] = split
    plan.setdefault("split_history", []).append({
        "date": recomp.local_today().isoformat(), "from": old_desc,
        "to": describe(new), "reason": reason or ""})
    recomp.save_plan(plan)

    # The profile's free-text split feeds Apollo's non-recomp prompt and the
    # brain's context; keep it saying the same thing.
    try:
        profile = json.loads(_r.get("user:profile") or "{}")
        profile["workout_split"] = describe(new)
        _r.set("user:profile", json.dumps(profile))
    except Exception:
        pass

    err = _trigger("week")
    return (f"Split is now {split.get('name', 'rolling split')}: {describe(new)}.{offset_note} "
            + (err or "Apollo is re-planning the week — get_workout_plan shows it in a minute."))
