"""Sage — nutrition logging from what JARVIS hears or sees.

The brain already receives glasses/camera photos as real image blocks, so it can
look at a meal (or hear "two eggs and toast") and estimate macros directly.
These tools PERSIST that estimate so it feeds the day's totals, the recomp
check-in, Apollo's recovery math and Remy's grocery targets.

Targets come from the recomp plan (recomp.py) when one exists.

Redis:
    nutrition:log:{YYYY-MM-DD}     list of logged meals (newest-first), kept
                                   permanently -- a 12-week plan needs months
    nutrition:totals:{YYYY-MM-DD}  hash: calories/protein_g/carbs_g/fat_g,
                                   always RECOMPUTED from the log, so an edit or
                                   delete can't leave it drifting
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import redis
from langchain_core.tools import tool

from .recomp import local_today, plan_targets

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)

_MACROS = ("calories", "protein_g", "carbs_g", "fat_g")


def _resolve_day(day: str) -> str | None:
    """'' → today, 'yesterday', or YYYY-MM-DD. None if unreadable."""
    d = local_today()
    if not day or day == "today":
        return d.isoformat()
    if day == "yesterday":
        return (d - timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError:
        return None


def _meals(day: str) -> list[dict]:
    """Meals for a day, OLDEST first (the order they were eaten)."""
    out = []
    for raw in reversed(_r.lrange(f"nutrition:log:{day}", 0, -1) or []):
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out


def _write_meals(day: str, meals: list[dict]) -> dict:
    """Replace a day's log and recompute its totals. Returns the totals."""
    key = f"nutrition:log:{day}"
    totals = {k: sum(m.get(k, 0) for m in meals) for k in _MACROS}
    pipe = _r.pipeline()
    pipe.delete(key, f"nutrition:totals:{day}")
    for m in meals:
        pipe.lpush(key, json.dumps(m))   # newest-first, as before
    if meals:
        pipe.hset(f"nutrition:totals:{day}", mapping=totals)
    pipe.execute()
    return totals


def _summary(day: str, totals: dict) -> str:
    """'Today: 1,050/2,700 kcal · P 85/205g · ... Remaining: ...'"""
    t = plan_targets()
    label = "Today" if day == local_today().isoformat() else day

    def part(k, unit, short):
        have = round(totals.get(k, 0))
        tgt = t.get(k)
        return f"{short} {have}" + (f"/{tgt}" if tgt else "") + unit

    line = (f"{label}: {part('calories', ' kcal', '').strip()} · {part('protein_g', 'g', 'P')} · "
            f"{part('carbs_g', 'g', 'C')} · {part('fat_g', 'g', 'F')}")
    rem = []
    for k, short, unit in (("calories", "", " kcal"), ("protein_g", "P ", "g"),
                           ("carbs_g", "C ", "g"), ("fat_g", "F ", "g")):
        if t.get(k):
            rem.append(f"{short}{round(t[k] - totals.get(k, 0))}{unit}")
    if rem:
        line += f". Remaining: {' · '.join(rem)}"
    return line + "."


def _find(meals: list[dict], which: str) -> int | None:
    """Index of a meal by 1-based number, 'last', or description substring."""
    w = (which or "").strip().lower()
    if not meals:
        return None
    if w in ("", "last", "latest"):
        return len(meals) - 1
    if w.isdigit() and 1 <= int(w) <= len(meals):
        return int(w) - 1
    for i in range(len(meals) - 1, -1, -1):   # newest match wins
        if w in meals[i].get("description", "").lower():
            return i
    return None


@tool
def log_meal(description: str, calories: float = 0, protein_g: float = 0,
             carbs_g: float = 0, fat_g: float = 0, day: str = "") -> str:
    """Log a meal/snack to the nutrition tally.

    When the user describes a meal ("two eggs, toast and a shake") or shares a
    food photo, estimate calories and grams of protein/carbs/fat and call this.
    Estimate realistic portions; if a portion is genuinely ambiguous and changes
    the numbers a lot (e.g. "some rice"), assume a typical serving and say so.
    Log each distinct meal once -- not each ingredient separately.

    Args:
        description: what the meal was, with portions if known.
        calories: estimated kcal.
        protein_g / carbs_g / fat_g: estimated grams.
        day: "" for today, "yesterday", or YYYY-MM-DD.
    """
    if not description.strip():
        return "What was the meal? I need a description to log it."
    d = _resolve_day(day)
    if not d:
        return f"Couldn't read the date '{day}' — use YYYY-MM-DD."
    entry = {"description": description.strip(),
             **{k: round(float(v or 0)) for k, v in
                (("calories", calories), ("protein_g", protein_g),
                 ("carbs_g", carbs_g), ("fat_g", fat_g))},
             "ts": datetime.now(timezone.utc).isoformat()}
    try:
        meals = _meals(d) + [entry]
        totals = _write_meals(d, meals)
    except Exception as exc:
        return f"Couldn't log the meal: {exc}"
    return (f"Logged #{len(meals)}: {entry['description']} (~{entry['calories']} kcal, "
            f"P {entry['protein_g']}g, C {entry['carbs_g']}g, F {entry['fat_g']}g). "
            + _summary(d, totals))


@tool
def edit_meal(which: str = "last", description: str = "", calories: float = -1,
              protein_g: float = -1, carbs_g: float = -1, fat_g: float = -1,
              day: str = "") -> str:
    """Correct a logged meal ("that was a large, not a medium", "the shake was
    40g protein"). Pass only the fields that change (-1 / "" = keep).

    Args:
        which: meal number from get_nutrition_today, "last", or words from its
               description (e.g. "shake").
        day: "" for today, "yesterday", or YYYY-MM-DD.
    """
    d = _resolve_day(day)
    if not d:
        return f"Couldn't read the date '{day}'."
    meals = _meals(d)
    i = _find(meals, which)
    if i is None:
        return f"No logged meal matches '{which}' on {d}."
    m = meals[i]
    if description.strip():
        m["description"] = description.strip()
    for k, v in (("calories", calories), ("protein_g", protein_g),
                 ("carbs_g", carbs_g), ("fat_g", fat_g)):
        if v is not None and v >= 0:
            m[k] = round(float(v))
    totals = _write_meals(d, meals)
    return (f"Updated #{i + 1}: {m['description']} (~{m['calories']} kcal, P {m['protein_g']}g, "
            f"C {m['carbs_g']}g, F {m['fat_g']}g). " + _summary(d, totals))


@tool
def delete_meal(which: str = "last", day: str = "") -> str:
    """Remove a logged meal ("delete the shake", "undo that").

    Args:
        which: meal number, "last", or words from its description.
        day: "" for today, "yesterday", or YYYY-MM-DD.
    """
    d = _resolve_day(day)
    if not d:
        return f"Couldn't read the date '{day}'."
    meals = _meals(d)
    i = _find(meals, which)
    if i is None:
        return f"No logged meal matches '{which}' on {d}."
    gone = meals.pop(i)
    totals = _write_meals(d, meals)
    return f"Removed: {gone['description']}. " + _summary(d, totals)


@tool
def get_nutrition_today(day: str = "") -> str:
    """Show a day's logged meals (numbered) and macro totals vs targets, with
    what's remaining.

    Use for "what have I eaten today?", "how's my protein?", "how many calories
    left?", and before suggesting what to eat next -- pick something that fits
    the remaining macros, preferring the user's favorite meals.

    Args:
        day: "" for today, "yesterday", or YYYY-MM-DD.
    """
    d = _resolve_day(day)
    if not d:
        return f"Couldn't read the date '{day}'."
    meals = _meals(d)
    if not meals:
        return f"Nothing logged for {d} yet. " + _summary(d, {})
    lines = [f"🍽️ Nutrition for {d}:"]
    for n, m in enumerate(meals, 1):
        lines.append(f"  {n}. {m['description']} — {m['calories']} kcal, P {m['protein_g']}g, "
                     f"C {m.get('carbs_g', 0)}g, F {m.get('fat_g', 0)}g")
    totals = {k: sum(m.get(k, 0) for m in meals) for k in _MACROS}
    lines.append(_summary(d, totals))
    return "\n".join(lines)


@tool
def get_nutrition_history(days: int = 7) -> str:
    """Daily calorie/macro totals for the last N days vs targets, with averages.

    Use for "how was my week?", "am I hitting protein?", trend questions.
    """
    days = max(1, min(int(days or 7), 90))
    t = plan_targets()
    today = local_today()
    rows, logged = [], []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        meals = _meals(d)
        if not meals:
            rows.append(f"  {d}: —")
            continue
        tot = {k: sum(m.get(k, 0) for m in meals) for k in _MACROS}
        logged.append(tot)
        rows.append(f"  {d}: {tot['calories']} kcal · P {tot['protein_g']}g · "
                    f"C {tot['carbs_g']}g · F {tot['fat_g']}g")
    head = (f"Last {days} days (targets: {t.get('calories')} kcal · P {t.get('protein_g')}g · "
            f"C {t.get('carbs_g')}g · F {t.get('fat_g')}g)")
    tail = f"Logged {len(logged)}/{days} days."
    if logged:
        avg = {k: round(sum(x[k] for x in logged) / len(logged)) for k in _MACROS}
        tail += (f" Avg on logged days: {avg['calories']} kcal · P {avg['protein_g']}g · "
                 f"C {avg['carbs_g']}g · F {avg['fat_g']}g")
    return "\n".join([head, *rows, tail])
