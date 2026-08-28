"""Brain-side LLM cost metering + daily budget alert.

The agent_runner has metered background-agent spend since the cost widget
landed (services/agent_runner/usage.py), but the BRAIN — the biggest spender —
was invisible. This records every _call_model invocation into the SAME Redis
schema, attributed as ``brain`` (with the model captured for pricing), so the
dashboard cost widget finally shows the whole picture.

Budget alert: when today's total (brain + agents share usage:daily:{day})
crosses DAILY_LLM_BUDGET_USD (env, default 15), ONE urgent notification per
day goes through the notify router. Mirrored in agent_runner/usage.py so
whichever recorder crosses the line fires it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import redis

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
BUDGET_USD = float(os.environ.get("DAILY_LLM_BUDGET_USD", "15"))

# USD per 1M tokens (input, output) — keep in sync with agent_runner/usage.py.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8":     (15.00, 75.00),
    "claude-sonnet-5":     (3.00,  15.00),
    "claude-haiku-4-5":    (0.80,   4.00),
    "claude-fable-5":      (3.00,  15.00),
    "gpt-4o-mini":         (0.15,   0.60),
    "gpt-4o":              (2.50,  10.00),
}
_DEFAULT_PRICE = (1.00, 3.00)

_r = redis.Redis(host=REDIS_HOST, decode_responses=True)


def _price_for(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in PRICES.items():
        if key in m:
            return price
    return _DEFAULT_PRICE


def record_response(response) -> None:
    """Record one LangChain AIMessage's usage. Never raises."""
    try:
        usage = getattr(response, "usage_metadata", None) or {}
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        if not (in_tok or out_tok):
            return
        meta = getattr(response, "response_metadata", None) or {}
        model = meta.get("model") or meta.get("model_name") or ""
        pin, pout = _price_for(model)
        cost = (in_tok / 1_000_000) * pin + (out_tok / 1_000_000) * pout
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"usage:daily:{day}"
        pipe = _r.pipeline()
        pipe.hincrby(key, "brain:in", in_tok)
        pipe.hincrby(key, "brain:out", out_tok)
        pipe.hincrbyfloat(key, "brain:cost", float(cost))
        pipe.hincrbyfloat(key, "_total:cost", float(cost))
        pipe.expire(key, 60 * 60 * 24 * 40)
        results = pipe.execute()
        check_budget(_r, float(results[3]), day)
        _snapshot_widget(day)
    except Exception:
        pass


def _snapshot_widget(day: str) -> None:
    """Rebuild widget:cost:data from today's rollup — same shape as the
    agent_runner recorder so the dashboard cost widget stays fresh even when
    only the brain is spending."""
    try:
        h = _r.hgetall(f"usage:daily:{day}") or {}
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
        _r.set("widget:cost:data", json.dumps({
            "date": day,
            "total_cost": round(total, 4),
            "agents": ranked,
            "updated": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        }))
    except Exception:
        pass


def check_budget(r, total_cost: float, day: str) -> None:
    """Fire one urgent notification per day when the budget line is crossed."""
    try:
        if BUDGET_USD <= 0 or total_cost < BUDGET_USD:
            return
        if not r.set(f"jarvis:budget_alerted:{day}", "1", nx=True,
                     ex=60 * 60 * 36):
            return
        import paho.mqtt.publish as mqtt_pub
        mqtt_pub.single("jarvis/notify", json.dumps({
            "title": "💸 Daily LLM budget crossed",
            "text": (f"Today's model spend hit ${total_cost:.2f} "
                     f"(budget ${BUDGET_USD:.0f}). Biggest spenders are on the "
                     "dashboard cost widget."),
            "urgency": "urgent", "source": "usage",
        }), hostname=MQTT_HOST, port=MQTT_PORT)
    except Exception:
        pass
