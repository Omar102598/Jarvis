"""Recomp plan — the user's body-recomposition goal as structured data.

One plan record that every agent reads, so Sage (nutrition), Apollo (training),
the morning brief and the Sunday check-in all agree on the same numbers instead
of each keeping its own copy of "2,700 kcal".

Redis:
    user:plan:recomp      json  the plan (targets, week template, split, phases,
                                target_history -- every calorie change + why)
    body:log              hash  YYYY-MM-DD -> {"weight_lbs", "waist_in"}
                                (manual entries; HealthKit weight is merged in)
    user:health:history   list  daily HealthKit snapshots (written by gateway)
    nutrition:log:{date}  list  meals (see nutrition.py)
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

import redis
from langchain_core.tools import tool

from . import cards

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
USER_TZ = os.environ.get("USER_TZ", "America/Chicago")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)

PLAN_KEY = "user:plan:recomp"
BODY_KEY = "body:log"


# ---------------------------------------------------------------------------
# Shared helpers (nutrition.py imports these)
# ---------------------------------------------------------------------------

def local_today() -> date:
    return datetime.now(ZoneInfo(USER_TZ)).date()


def load_plan() -> dict:
    try:
        return json.loads(_r.get(PLAN_KEY) or "{}")
    except Exception:
        return {}


def save_plan(plan: dict) -> None:
    _r.set(PLAN_KEY, json.dumps(plan))


def plan_targets() -> dict:
    """Active daily targets: the recomp plan if one exists, else the profile."""
    plan = load_plan()
    t = plan.get("targets") or {}
    if t:
        return {k: t.get(k) for k in ("calories", "protein_g", "carbs_g", "fat_g", "steps")}
    try:
        p = json.loads(_r.get("user:profile") or "{}")
    except Exception:
        p = {}
    weight = float(p.get("weight_lbs", 0) or 0)
    ppl = float(p.get("protein_goal_g_per_lb", 1.0) or 1.0)
    return {"calories": p.get("calorie_target"),
            "protein_g": round(weight * ppl) if weight else None,
            "carbs_g": None, "fat_g": None, "steps": None}


def plan_week(plan: dict, on: date | None = None) -> int | None:
    """1-based plan week for a date, or None before the start / without a plan."""
    try:
        start = date.fromisoformat(plan["start_date"])
    except Exception:
        return None
    d = on or local_today()
    if d < start:
        return None
    return (d - start).days // 7 + 1


def _phase(plan: dict, week: int | None) -> dict:
    if not week:
        return {}
    for ph in plan.get("phases", []):
        lo, hi = ph.get("weeks", [0, 0])
        if lo <= week <= hi:
            return ph
    return {}


def _weights_by_day() -> dict[str, float]:
    """Daily weight: manual body:log entries win over the HealthKit sync."""
    out: dict[str, float] = {}
    for raw in _r.lrange("user:health:history", 0, -1) or []:
        try:
            h = json.loads(raw)
            if h.get("body_mass_lbs") and h.get("ts"):
                d = datetime.fromtimestamp(h["ts"], ZoneInfo(USER_TZ)).date().isoformat()
                out.setdefault(d, round(float(h["body_mass_lbs"]), 1))
        except Exception:
            continue
    for d, raw in (_r.hgetall(BODY_KEY) or {}).items():
        try:
            w = json.loads(raw).get("weight_lbs")
            if w:
                out[d] = float(w)
        except Exception:
            continue
    return out


def _window(days_back_start: int, days_back_end: int) -> list[str]:
    """ISO dates from (today - start) to (today - end), inclusive, oldest first."""
    t = local_today()
    return [(t - timedelta(days=i)).isoformat()
            for i in range(days_back_start, days_back_end - 1, -1)]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def get_recomp_plan() -> str:
    """Show the user's body-recomposition plan: current week and phase, daily
    calorie/macro/step targets, today's training, and the goal.

    Use for "what's my plan?", "what week am I on?", "what are my macros?",
    "what should today look like?", and before giving nutrition or training
    advice so it matches the plan.
    """
    plan = load_plan()
    if not plan:
        return "No recomp plan on file. Ask the user for their targets first."
    today = local_today()
    week = plan_week(plan, today)
    t = plan.get("targets", {})
    lines = [f"{plan.get('name', 'Recomp plan')} — starts {plan.get('start_date')}, "
             f"{plan.get('weeks', 12)} weeks."]
    if week is None:
        lines.append("Not started yet.")
    elif week > plan.get("weeks", 12):
        lines.append(f"Plan finished (week {week}). Time to decide: keep cutting or maintain.")
    else:
        ph = _phase(plan, week)
        lines.append(f"Week {week} of {plan.get('weeks', 12)}"
                     + (f" — {ph.get('name')}: {ph.get('focus')}" if ph else ""))
    lines.append(f"Daily targets: {t.get('calories')} kcal · protein {t.get('protein_g')}g · "
                 f"carbs {t.get('carbs_g')}g · fat {t.get('fat_g')}g · {t.get('steps', 0):,} steps")
    day_name = today.strftime("%A")
    template = plan.get("week_template", {}).get(day_name, "")
    session = ""
    try:
        wp = json.loads(_r.get("workout:plan") or "{}")
        d = next((x for x in wp.get("days", []) if x.get("day") == day_name), None)
        if d:
            session = d.get("class") or d.get("focus", "")
    except Exception:
        pass
    lines.append(f"Today ({day_name}): {session or template or 'see Apollo'}")
    lines.append(f"Goal: {plan.get('goal', '')}")
    try:
        _start = date.fromisoformat(plan["start_date"]).strftime("%a, %b %-d")
    except Exception:
        _start = plan.get("start_date", "?")
    wk_label = (f"Starts {_start}" if week is None
                else f"Week {min(week, plan.get('weeks', 12))} of {plan.get('weeks', 12)}")
    ph = _phase(plan, week)
    card = cards.attach({
        "type": "stats", "title": plan.get("name", "Recomp plan"),
        "subtitle": wk_label + (f" · {ph.get('name')}" if ph else ""),
        "items": [
            {"label": "Calories", "value": f"{t.get('calories', 0):,}", "sub": "daily target"},
            {"label": "Protein", "value": f"{t.get('protein_g')}g", "sub": "muscle retention"},
            {"label": "Carbs", "value": f"{t.get('carbs_g')}g", "sub": "training fuel"},
            {"label": "Fat", "value": f"{t.get('fat_g')}g", "sub": "hormones"},
            {"label": "Steps", "value": f"{t.get('steps', 0):,}", "sub": "daily"},
            {"label": f"Today · {day_name[:3]}", "value": session or template or "—", "sub": ""},
        ]})
    lo, hi = plan.get("pace_lbs_per_week", [0.25, 0.5])
    glo, ghi = plan.get("goal_weight_range_lbs", [None, None])
    lines.append(f"Pace: lose {lo}–{hi} lb/week"
                 + (f"; land around {glo}–{ghi} lb, not lower at the cost of muscle." if glo else "."))
    return "\n".join(lines) + card


@tool
def update_recomp_targets(reason: str, calories: float = 0, protein_g: float = 0,
                          carbs_g: float = 0, fat_g: float = 0, steps: float = 0) -> str:
    """Change the recomp plan's daily targets. Pass only what changes (0 = keep).

    ONLY call this after the user has agreed to the change (e.g. they accepted
    the Sunday check-in's suggestion). Every change is recorded with its reason
    so the plan's history explains itself.

    Args:
        reason: why, e.g. "week 3 check-in: weight flat 2 weeks, -150 kcal from carbs".
    """
    plan = load_plan()
    if not plan:
        return "No recomp plan on file to update."
    t = plan.setdefault("targets", {})
    changed = {}
    for k, v in (("calories", calories), ("protein_g", protein_g),
                 ("carbs_g", carbs_g), ("fat_g", fat_g), ("steps", steps)):
        if v:
            changed[k] = round(float(v))
            t[k] = changed[k]
    if not changed:
        return "Nothing to change — pass at least one new target."
    plan.setdefault("target_history", []).append(
        {"date": local_today().isoformat(), **t, "reason": reason})
    save_plan(plan)
    implied = t.get("protein_g", 0) * 4 + t.get("carbs_g", 0) * 4 + t.get("fat_g", 0) * 9
    note = ""
    if t.get("calories") and abs(implied - t["calories"]) > 75:
        note = (f" Heads up: macros add up to {implied} kcal, not {t['calories']} — "
                "adjust carbs or fat so they match.")
    return f"Targets updated ({', '.join(f'{k}={v}' for k, v in changed.items())}).{note}"


@tool
def log_body_metrics(weight_lbs: float = 0, waist_in: float = 0, day: str = "") -> str:
    """Record a body measurement: morning weight (lb) and/or waist (inches).

    Use for "I weighed 203.4 this morning", "waist is 34.5". Weight also syncs
    from HealthKit automatically; waist only comes from the user.

    Args:
        day: YYYY-MM-DD or "yesterday"; default today.
    """
    if not weight_lbs and not waist_in:
        return "Give me a weight or a waist measurement to log."
    d = local_today()
    if day == "yesterday":
        d -= timedelta(days=1)
    elif day:
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return f"Couldn't read the date '{day}' — use YYYY-MM-DD."
    key = d.isoformat()
    try:
        entry = json.loads(_r.hget(BODY_KEY, key) or "{}")
    except Exception:
        entry = {}
    if weight_lbs:
        entry["weight_lbs"] = round(float(weight_lbs), 1)
    if waist_in:
        entry["waist_in"] = round(float(waist_in), 2)
    _r.hset(BODY_KEY, key, json.dumps(entry))
    bits = [f"{entry['weight_lbs']} lb" if weight_lbs else "",
            f'waist {entry["waist_in"]}"' if waist_in else ""]
    return f"Logged for {key}: {', '.join(b for b in bits if b)}."


@tool
def get_recomp_checkin() -> str:
    """Gather the weekly recomp check-in numbers: 7-day average weight vs the
    week before, waist trend, workouts, steps and nutrition adherence, plus a
    rule-based calorie suggestion.

    Use for the Sunday check-in and for "how's my progress?". Present the
    suggestion and ASK before changing anything; apply an accepted change with
    update_recomp_targets.
    """
    plan = load_plan()
    if not plan:
        return "No recomp plan on file."
    t = plan.get("targets", {})
    week = plan_week(plan)
    this_wk, last_wk = _window(6, 0), _window(13, 7)
    weights = _weights_by_day()

    def avg(days):
        vals = [weights[d] for d in days if d in weights]
        return (round(mean(vals), 1), len(vals)) if vals else (None, 0)

    w_now, n_now = avg(this_wk)
    w_prev, n_prev = avg(last_wk)
    lines = [f"Recomp check-in — week {week or '?'} of {plan.get('weeks', 12)}"]
    if w_now is not None:
        lines.append(f"Avg weight this week: {w_now} lb ({n_now} weigh-ins)")
    else:
        lines.append("No weigh-ins this week.")
    change = None
    if w_now is not None and w_prev is not None and n_now >= 3 and n_prev >= 3:
        change = round(w_now - w_prev, 1)
        lines.append(f"Last week: {w_prev} lb → change {change:+} lb")
    elif w_prev is not None:
        lines.append(f"Last week: {w_prev} lb ({n_prev} weigh-ins) — too few to compare reliably")

    waists = []
    for d, raw in sorted((_r.hgetall(BODY_KEY) or {}).items()):
        try:
            if json.loads(raw).get("waist_in"):
                waists.append((d, json.loads(raw)["waist_in"]))
        except Exception:
            continue
    if waists:
        first, last = waists[0], waists[-1]
        lines.append(f'Waist: {last[1]}" on {last[0]}'
                     + (f' (started {first[1]}" on {first[0]}, {last[1] - first[1]:+.2f}")'
                        if first != last else ""))
    else:
        lines.append("Waist: never measured — ask for one (navel height, relaxed, morning).")

    # Workouts + steps from HealthKit's daily snapshots
    workouts, steps = 0, []
    for raw in _r.lrange("user:health:history", 0, -1) or []:
        try:
            h = json.loads(raw)
            d = datetime.fromtimestamp(h["ts"], ZoneInfo(USER_TZ)).date().isoformat()
        except Exception:
            continue
        if d in this_wk:
            if (h.get("workouts_today") or 0) > 0:
                workouts += 1
            if h.get("steps"):
                steps.append(h["steps"])
    planned = sum(1 for v in plan.get("week_template", {}).values()
                  if "rest" not in str(v).lower())
    lines.append(f"Workout days logged by HealthKit: {workouts}/{planned} planned")
    if steps:
        lines.append(f"Avg steps: {round(mean(steps)):,} (target {t.get('steps', 10000):,}, "
                     f"{len(steps)} days synced)")

    # Nutrition adherence
    logged = []
    for d in this_wk:
        meals = _r.lrange(f"nutrition:log:{d}", 0, -1) or []
        if meals:
            tot = {"calories": 0, "protein_g": 0}
            for m in meals:
                try:
                    mm = json.loads(m)
                    tot["calories"] += mm.get("calories", 0)
                    tot["protein_g"] += mm.get("protein_g", 0)
                except Exception:
                    continue
            logged.append(tot)
    if logged:
        ac = round(mean(x["calories"] for x in logged))
        ap = round(mean(x["protein_g"] for x in logged))
        lines.append(f"Food logged {len(logged)}/7 days — avg {ac} kcal (target {t.get('calories')}), "
                     f"protein {ap}g (target {t.get('protein_g')}g)")
    else:
        lines.append("Food logged 0/7 days.")

    # Suggestion — conservative, and never on thin data
    lo, hi = plan.get("pace_lbs_per_week", [0.25, 0.5])
    if change is None:
        sug = "Not enough weigh-ins to judge the trend yet — hold targets, aim for 4+ weigh-ins a week."
    elif len(logged) < 5:
        sug = ("Hold targets. Fewer than 5 days of food logged, so the scale can't tell us "
               "whether the targets or the tracking are off — log more consistently first.")
    elif -change < lo:
        sug = (f"Loss is under {lo} lb/week: suggest -100 to -150 kcal, taken from carbs "
               "(protein stays; fat stays at or above ~70g).")
    elif -change > hi + 0.5:
        sug = (f"Losing faster than {hi} lb/week risks muscle: suggest +100 to +150 kcal from carbs.")
    else:
        sug = "On pace — hold targets."
    lines.append(f"Suggestion: {sug}")

    items = [{"label": "Avg weight", "value": f"{w_now} lb" if w_now is not None else "—",
              "sub": f"{n_now} weigh-ins"},
             {"label": "vs last week", "value": f"{change:+} lb" if change is not None else "—",
              "sub": f"goal −{lo} to −{hi}"},
             {"label": "Waist", "value": f'{waists[-1][1]}"' if waists else "—",
              "sub": waists[-1][0] if waists else "not measured"},
             {"label": "Workouts", "value": f"{workouts}/{planned}", "sub": "HealthKit"},
             {"label": "Avg steps", "value": f"{round(mean(steps)):,}" if steps else "—",
              "sub": f"target {t.get('steps', 10000):,}"},
             {"label": "Food logged", "value": f"{len(logged)}/7",
              "sub": (f"avg {round(mean(x['calories'] for x in logged)):,} kcal" if logged else "days")}]
    out = cards.attach({"type": "stats", "title": f"Week {week or '?'} check-in",
                        "subtitle": sug, "items": items})
    pts = [{"x": d[5:], "y": weights[d]} for d in _window(13, 0) if d in weights]
    if len(pts) >= 2:
        out += cards.attach({"type": "chart", "title": "Weight · 14 days", "kind": "line",
                             "unit": "lb", "points": pts,
                             "target": (plan.get("goal_weight_range_lbs") or [0, 0])[1] or 0,
                             "footer": "Daily weigh-ins; the weekly average is what counts."})
    return "\n".join(lines) + out
