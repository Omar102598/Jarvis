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


# ===========================================================================
# Rule set
# ---------------------------------------------------------------------------
# Real cross-domain rules are added in the Synapse step once the exact Redis
# keys (health snapshot, finance widget, calendar) are verified live, so nothing
# fires off a guessed key. The engine above is complete and tested; rules plug
# in with the @rule decorator, e.g.:
#
#   @rule("low_cash_before_rent")
#   def _low_cash_before_rent(r) -> Insight | None:
#       fin = json.loads(r.get("widget:finance:data") or "{}")
#       ...
#       return Insight(title="💸 Heads up", text="Rent's due Friday and cash is low.")
# ===========================================================================
