"""Workout tools — talk to Apollo, the personal-trainer agent."""

import json
import os
from datetime import datetime

import paho.mqtt.publish as mqtt_publish
import redis
from langchain_core.tools import tool

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
    return "\n".join(lines)


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
    return "\n".join(lines)
