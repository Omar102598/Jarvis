"""Per-agent LLM cost & token accounting.

Every background agent's LLM call flows through llm_helper.complete(); this
module records the token usage and dollar cost of each call, attributed to the
agent that made it, so you can see which agents are worth their tokens.

Attribution is automatic: run_agent() stamps the current agent name into a
contextvar before calling agent.run(), and complete() reads it — no need to
thread an ``agent=`` argument through every call site.

Storage (Redis):
    usage:daily:{YYYY-MM-DD}  hash  — {agent}:in / :out / :cost, plus _total:cost
    widget:cost:data          json  — snapshot the dashboard cost widget renders

Prices are USD per 1M tokens (input, output), editable below. They're
best-effort defaults — adjust to your actual contract if it differs.
"""

from __future__ import annotations

import contextvars
import json
from datetime import datetime, timezone

# USD per 1,000,000 tokens: (input, output). Unknown models fall back to _DEFAULT.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8":     (15.00, 75.00),
    "claude-sonnet-5":     (3.00,  15.00),
    "claude-haiku-4-5":    (0.80,   4.00),
    "claude-fable-5":      (3.00,  15.00),
    "gpt-4o-mini":         (0.15,   0.60),
    "gpt-4o":              (2.50,  10.00),
}
_DEFAULT_PRICE = (1.00, 3.00)

_current_agent: contextvars.ContextVar[str] = contextvars.ContextVar("agent", default="")


def set_current_agent(name: str) -> None:
    """Called by run_agent() before agent.run() so complete() can attribute cost."""
    _current_agent.set(name or "")


def current_agent() -> str:
    return _current_agent.get()


def _price_for(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in PRICES.items():
        if key in m:
            return price
    return _DEFAULT_PRICE


def cost_of(model: str, in_tokens: int, out_tokens: int) -> float:
    pin, pout = _price_for(model)
    return (in_tokens / 1_000_000) * pin + (out_tokens / 1_000_000) * pout


def record(r, model: str, in_tokens: int, out_tokens: int,
           agent: str | None = None) -> None:
    """Record one LLM call's usage. Never raises — accounting must not break work."""
    try:
        who = (agent if agent is not None else current_agent()) or "unknown"
        cost = cost_of(model, in_tokens, out_tokens)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"usage:daily:{day}"
        pipe = r.pipeline()
        pipe.hincrby(key, f"{who}:in", int(in_tokens))
        pipe.hincrby(key, f"{who}:out", int(out_tokens))
        pipe.hincrbyfloat(key, f"{who}:cost", float(cost))
        pipe.hincrbyfloat(key, "_total:cost", float(cost))
        pipe.expire(key, 60 * 60 * 24 * 40)   # keep ~40 days of daily rollups
        pipe.execute()
        _snapshot_widget(r, day)
    except Exception:
        pass


def _snapshot_widget(r, day: str) -> None:
    """Rebuild widget:cost:data from today's rollup (cheap; runs per call)."""
    try:
        h = r.hgetall(f"usage:daily:{day}") or {}
        agents: dict[str, dict] = {}
        total = 0.0
        for field, val in h.items():
            if field == "_total:cost":
                total = float(val)
                continue
            name, _, metric = field.rpartition(":")
            if not name or name.startswith("_"):
                continue
            a = agents.setdefault(name, {"name": name, "in": 0, "out": 0, "cost": 0.0})
            if metric == "in":
                a["in"] = int(float(val))
            elif metric == "out":
                a["out"] = int(float(val))
            elif metric == "cost":
                a["cost"] = round(float(val), 4)
        ranked = sorted(agents.values(), key=lambda x: x["cost"], reverse=True)
        r.set("widget:cost:data", json.dumps({
            "date": day,
            "total_cost": round(total, 4),
            "agents": ranked,
            "updated": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        }))
    except Exception:
        pass
