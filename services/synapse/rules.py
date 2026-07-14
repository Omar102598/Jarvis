"""Synapse correlation rules — the cross-agent intelligence layer.

Each agent writes to Redis/the bus and stops. Rules here JOIN those signals
across domains and surface an insight no single agent could:

    "HRV down 3 days + a 7am flight tomorrow → Apollo, make Thursday a recovery day."
    "Rent due in 3 days + cash below your usual buffer."
    "4 ClassPass classes booked + $340 takeout this week → want a meal-prep list?"

A rule is a function ``(r) -> Insight | None``. It reads state (Redis keys,
the event stream) and returns an insight to surface, or None. The engine runs
every rule on a timer, enforces a per-rule cooldown so an insight isn't
repeated, and routes anything that fires through the notification router (normal
urgency → it lands in your digest, not as an interrupt).

Rules must be conservative and side-effect-free: read state, decide, return.
They never act on your behalf — they surface, you decide.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

USER_ID = os.environ.get("JARVIS_USER_ID", "default")

# Per-rule cooldown so a standing condition (e.g. low cash all week) surfaces
# once, not every timer tick.
DEFAULT_COOLDOWN_S = int(os.environ.get("SYNAPSE_RULE_COOLDOWN_S", "43200"))  # 12h


@dataclass
class Insight:
    title: str
    text: str
    urgency: str = "normal"          # "urgent" surfaces immediately
    dedup_key: str = ""              # defaults to the rule name
    cooldown_s: int = DEFAULT_COOLDOWN_S


@dataclass
class Rule:
    name: str
    fn: "callable"
    enabled: bool = True
    tags: list[str] = field(default_factory=list)


_REGISTRY: list[Rule] = []


def rule(name: str, enabled: bool = True, tags: list[str] | None = None):
    """Decorator: register a correlation rule."""
    def _wrap(fn):
        _REGISTRY.append(Rule(name=name, fn=fn, enabled=enabled, tags=tags or []))
        return fn
    return _wrap


def _profile(r) -> dict:
    try:
        return json.loads(r.get("user:profile") or "{}")
    except Exception:
        return {}


def _on_cooldown(r, name: str) -> bool:
    try:
        return bool(r.exists(f"synapse:rule:cooldown:{name}"))
    except Exception:
        return False


def _arm_cooldown(r, name: str, seconds: int) -> None:
    try:
        r.set(f"synapse:rule:cooldown:{name}", "1", ex=max(60, seconds))
    except Exception:
        pass


def evaluate_all(r) -> list[Insight]:
    """Run every enabled, off-cooldown rule. Return the insights that fired.

    The caller (main loop) is responsible for routing each insight through the
    notification router; the cooldown is armed here so a slow router can't cause
    a double-fire.
    """
    fired: list[Insight] = []
    for rl in _REGISTRY:
        if not rl.enabled or _on_cooldown(r, rl.name):
            continue
        try:
            insight = rl.fn(r)
        except Exception:
            insight = None
        if insight is None:
            continue
        insight.dedup_key = insight.dedup_key or f"synapse:{rl.name}"
        _arm_cooldown(r, rl.name, insight.cooldown_s)
        fired.append(insight)
    return fired


# ---------------------------------------------------------------------------
# Shared state readers (keys verified live against the running system)
#   user:health:latest    {hrv_ms, resting_heart_rate, sleep_hours, ts, …}
#   user:health:history   list, newest-first, ~30 daily snapshots
#   jarvis:calendar:next_event  {title, start (ISO), location}
#   widget:finance:data   {total_cash, available_balance, total_spent_30d,
#                          budgets:[{category, spent}], updated}
#   workout:plan          Apollo's current week plan
#   grocery:pending_order / grocery:meal_plan  Remy's latest shopping state
# ---------------------------------------------------------------------------


def _json(r, key: str, default):
    try:
        raw = r.get(key)
        return json.loads(raw) if raw else default
    except Exception:
        return default


def _health_history(r, limit: int = 14) -> list[dict]:
    try:
        rows = r.lrange("user:health:history", 0, limit - 1) or []
    except Exception:
        return []
    out = []
    for row in rows:
        try:
            out.append(json.loads(row))
        except Exception:
            continue
    return out


def _median(vals: list[float]) -> float | None:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


# ===========================================================================
# Rule set — conservative, side-effect-free cross-domain joins.
# Each reads verified keys, guards missing data, and returns normal-urgency
# insights (they land in the digest, not as interrupts) with long cooldowns.
# ===========================================================================


@rule("recovery_vs_training")
def _recovery_vs_training(r) -> Insight | None:
    """HealthKit recovery is poor AND Apollo has a workout planned today →
    suggest easing off. Join: HealthKit × Apollo."""
    latest = _json(r, "user:health:latest", {})
    if not latest:
        return None
    if not r.exists("workout:plan"):
        return None   # nothing planned to ease off from

    hist = _health_history(r, 14)[1:]   # exclude today's head for the baseline
    hrv_base = _median([h.get("hrv_ms") for h in hist])
    rhr_base = _median([h.get("resting_heart_rate") for h in hist])

    hrv = latest.get("hrv_ms")
    rhr = latest.get("resting_heart_rate")
    sleep = latest.get("sleep_hours")

    flags = []
    if hrv is not None and hrv_base and hrv < 0.80 * hrv_base:
        flags.append(f"HRV {round(hrv)}ms (↓ vs your ~{round(hrv_base)}ms baseline)")
    if rhr is not None and rhr_base and rhr > 1.10 * rhr_base:
        flags.append(f"resting HR {round(rhr)} (↑ vs ~{round(rhr_base)})")
    if sleep is not None and sleep < 6:
        flags.append(f"{sleep:g}h sleep")
    if len(flags) < 1:
        return None

    return Insight(
        title="🫀 Recovery is running low",
        text=("Your recovery signals are down (" + "; ".join(flags) +
              ") but a workout's on Apollo's plan today. Want an active-recovery "
              "day instead? Say \"Apollo, make today easy\"."),
        cooldown_s=20 * 3600,   # once per day at most
    )


@rule("short_sleep_before_early_start")
def _short_sleep_before_early_start(r) -> Insight | None:
    """Slept short AND the next event starts early → a gentle wind-down nudge.
    Join: HealthKit × calendar."""
    from datetime import datetime, timezone

    latest = _json(r, "user:health:latest", {})
    sleep = latest.get("sleep_hours")
    if sleep is None or sleep >= 6.5:
        return None
    evt = _json(r, "jarvis:calendar:next_event", {})
    start = evt.get("start")
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except Exception:
        return None
    now = datetime.now(dt.tzinfo or timezone.utc)
    hours_away = (dt - now).total_seconds() / 3600
    if not (0 < hours_away < 18):
        return None
    if dt.hour >= 8:      # only "early" starts
        return None
    title = evt.get("title", "an event")
    return Insight(
        title="😴 Short night, early start",
        text=(f"You logged {sleep:g}h last night and \"{title}\" starts at "
              f"{dt.strftime('%-I:%M %p')}. Might be a night to wind down early."),
        cooldown_s=16 * 3600,
    )


@rule("dining_spend_vs_groceries")
def _dining_spend_vs_groceries(r) -> Insight | None:
    """High recent dining/takeout spend AND no fresh grocery plan → suggest Remy
    build a meal-prep list. Join: finance × grocery."""
    fin = _json(r, "widget:finance:data", {})
    budgets = fin.get("budgets") or []
    dining = 0.0
    for b in budgets:
        cat = (b.get("category") or "").lower()
        if any(k in cat for k in ("dining", "restaurant", "takeout", "food & drink", "fast food")):
            dining += float(b.get("spent") or 0)
    profile = _profile(r)
    threshold = float(profile.get("dining_alert_usd", 250))
    if dining < threshold:
        return None
    # Only nudge if there isn't already a fresh grocery plan in flight.
    if r.exists("grocery:pending_order"):
        return None
    return Insight(
        title="🍽️ Takeout is adding up",
        text=(f"You've spent about ${dining:,.0f} on dining/takeout in the last 30 "
              "days. Want Remy to build a meal-prep grocery list to cut that down? "
              "Say \"Remy, plan meal prep\"."),
        cooldown_s=6 * 24 * 3600,   # weekly at most
    )


@rule("supplement_reminder")
def _supplement_reminder(r) -> Insight | None:
    """A daily supplement/medication nudge at the configured hour.
    Set via profile: supplements ("creatine, vitamin d") + supplement_hour (0-23)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    p = _profile(r)
    supps = p.get("supplements")
    if not supps:
        return None
    try:
        hour = int(p.get("supplement_hour", 8))
    except Exception:
        hour = 8
    try:
        now = datetime.now(ZoneInfo(os.environ.get("USER_TZ", "America/Chicago")))
    except Exception:
        now = datetime.now()
    if now.hour != hour:
        return None
    names = supps if isinstance(supps, list) else [s.strip() for s in str(supps).split(",")]
    names = [n for n in names if n]
    if not names:
        return None
    return Insight(
        title="💊 Supplements",
        text="Time for your supplements: " + ", ".join(names) + ".",
        cooldown_s=20 * 3600,   # once per day
    )


@rule("stale_next_actions")
def _stale_next_actions(r) -> Insight | None:
    """Next-actions that have sat untouched for a while → a gentle resurfacing.
    Join: the GTD task loop × time."""
    from datetime import datetime, timezone

    try:
        raw = r.hgetall(f"{USER_ID}:jarvis:tasks") or {}
    except Exception:
        return None
    now = datetime.now(timezone.utc)
    stale = []
    for val in raw.values():
        try:
            t = json.loads(val)
        except Exception:
            continue
        if t.get("status") != "next":
            continue
        try:
            age_days = (now - datetime.fromisoformat(t["created"])).days
        except Exception:
            continue
        if age_days >= 4:
            stale.append((age_days, t.get("text", "")))
    if not stale:
        return None
    stale.sort(reverse=True)
    top = "; ".join(txt for _, txt in stale[:4])
    return Insight(
        title="📌 Stalled next-actions",
        text=(f"{len(stale)} next-action(s) have been sitting a while: {top}. "
              "Knock one out, or say \"reschedule\" / \"drop it\"."),
        cooldown_s=3 * 24 * 3600,
    )
