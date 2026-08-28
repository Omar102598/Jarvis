"""Sage — nutrition logging from what JARVIS sees.

The brain already receives glasses/camera photos as real image blocks, so it can
look at a meal and estimate macros directly. These tools PERSIST that estimate
so it feeds the day's totals (and, later, Apollo's recovery math + Remy's
grocery targets). No separate vision call needed — the brain does the seeing;
log_meal does the remembering.

Redis:
    nutrition:log:{YYYY-MM-DD}     list of logged meals (newest-first)
    nutrition:totals:{YYYY-MM-DD}  hash: calories/protein_g/carbs_g/fat_g
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
USER_TZ = os.environ.get("USER_TZ", "America/Chicago")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)


def _today() -> str:
    return datetime.now(ZoneInfo(USER_TZ)).strftime("%Y-%m-%d")


def _targets() -> dict:
    """Protein/calorie targets from the user's profile (best-effort)."""
    try:
        p = json.loads(_r.get("user:profile") or "{}")
    except Exception:
        p = {}
    weight = float(p.get("weight_lbs", 0) or 0)
    ppl = float(p.get("protein_goal_g_per_lb", 1.0) or 1.0)
    return {
        "protein_g": round(weight * ppl) if weight else None,
        "calories": p.get("calorie_target"),
    }


@tool
def log_meal(description: str, calories: float = 0, protein_g: float = 0,
            carbs_g: float = 0, fat_g: float = 0) -> str:
    """Log a meal/snack to today's nutrition tally.

    When the user shares a photo of food (or describes a meal), estimate its
    macros from what you see and call this to record it. Use your best estimate
    for calories and grams of protein/carbs/fat.

    Args:
        description: what the meal was (e.g. "grilled chicken bowl with rice").
        calories: estimated kcal.
        protein_g / carbs_g / fat_g: estimated grams.
    """
    if not description.strip():
        return "What was the meal? I need a description to log it."
    day = _today()
    entry = {
        "description": description.strip(),
        "calories": round(float(calories or 0)),
        "protein_g": round(float(protein_g or 0)),
        "carbs_g": round(float(carbs_g or 0)),
        "fat_g": round(float(fat_g or 0)),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _r.lpush(f"nutrition:log:{day}", json.dumps(entry))
        _r.expire(f"nutrition:log:{day}", 60 * 60 * 24 * 40)
        tk = f"nutrition:totals:{day}"
        pipe = _r.pipeline()
        for k in ("calories", "protein_g", "carbs_g", "fat_g"):
            pipe.hincrbyfloat(tk, k, entry[k])
        pipe.expire(tk, 60 * 60 * 24 * 40)
        pipe.execute()
        totals = _r.hgetall(tk)
    except Exception as exc:
        return f"Couldn't log the meal: {exc}"

    t = _targets()
    prot = round(float(totals.get("protein_g", 0)))
    cal = round(float(totals.get("calories", 0)))
    prot_line = f"{prot}g" + (f"/{t['protein_g']}g" if t.get("protein_g") else "")
    cal_line = f"{cal}" + (f"/{t['calories']}" if t.get("calories") else "") + " kcal"
    return (f"Logged: {description.strip()} (~{entry['calories']} kcal, "
            f"{entry['protein_g']}g protein). Today: {cal_line}, protein {prot_line}.")


@tool
def get_nutrition_today() -> str:
    """Show today's logged meals and running macro totals vs targets.

    Use for "what have I eaten today?", "how's my protein?", "how many calories
    so far?".
    """
    day = _today()
    try:
        meals = _r.lrange(f"nutrition:log:{day}", 0, -1) or []
        totals = _r.hgetall(f"nutrition:totals:{day}") or {}
    except Exception:
        meals, totals = [], {}
    if not meals:
        return "Nothing logged today yet. Show me a meal or tell me what you ate."
    t = _targets()
    lines = ["🍽️ Today's nutrition:"]
    for raw in reversed(meals):
        try:
            m = json.loads(raw)
            lines.append(f"  • {m['description']} — {m['calories']} kcal, {m['protein_g']}g P")
        except Exception:
            continue
    cal = round(float(totals.get("calories", 0)))
    prot = round(float(totals.get("protein_g", 0)))
    carb = round(float(totals.get("carbs_g", 0)))
    fat = round(float(totals.get("fat_g", 0)))
    tp = f"/{t['protein_g']}g" if t.get("protein_g") else ""
    tc = f"/{t['calories']}" if t.get("calories") else ""
    lines.append(f"Totals: {cal}{tc} kcal · protein {prot}{tp} · carbs {carb}g · fat {fat}g")
    return "\n".join(lines)
