"""get_spending_insights — the unified money view (Spend Guardian + budgets).

Pulls together what the finance agent and Spend Guardian have already computed
into one answer: detected subscriptions (with monthly total + next charge),
recent guardian alerts, 30-day spending vs recommended budgets, and grocery
budget — so "how am I doing on money?" has a single, grounded reply.
"""

from __future__ import annotations

import json
import os

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)


def _j(key, default):
    try:
        raw = _r.get(key)
        return json.loads(raw) if raw else default
    except Exception:
        return default


@tool
def get_spending_insights() -> str:
    """Get a unified money overview: subscriptions, unusual-charge alerts, 30-day
    spending vs budgets, and grocery budget.

    Use for "how's my spending?", "what subscriptions do I have?", "am I over
    budget?", "anything unusual on my accounts?".
    """
    fin = _j("widget:finance:data", {})
    subs = _j("finance:subscriptions", [])
    alerts = _j("finance:alerts", [])
    rec_budgets = _j("finance:budgets", [])
    profile = _j("user:profile", {})

    if not fin and not subs:
        return ("No finance data yet — the finance agent and Spend Guardian need "
                "BankSync configured (BANKSYNC_API_KEY).")

    lines = ["💰 Spending overview"]

    if fin:
        cash = fin.get("total_cash")
        avail = fin.get("available_balance")
        spent = fin.get("total_spent_30d")
        income = fin.get("income_30d")
        if cash is not None:
            lines.append(f"Cash ${cash:,.0f} (available ${avail or cash:,.0f}).")
        if spent is not None:
            io = f" vs ${income:,.0f} in" if income else ""
            lines.append(f"Last 30d: ${spent:,.0f} spent{io}.")

    # Spending vs recommended budgets
    budgets = {b.get("category"): b for b in fin.get("budgets", [])}
    if rec_budgets:
        over = []
        for b in rec_budgets:
            cat = b.get("category")
            rec = float(b.get("recommended_monthly", 0) or 0)
            spent = float((budgets.get(cat) or {}).get("spent", 0) or 0)
            if rec and spent > rec:
                over.append(f"{cat} ${spent:,.0f}/${rec:,.0f}")
        if over:
            lines.append("Over budget: " + "; ".join(over[:5]) + ".")
        else:
            lines.append("On or under budget across tracked categories.")

    # Subscriptions
    if subs:
        monthly = sum(float(s.get("monthly_est", 0) or 0) for s in subs)
        top = sorted(subs, key=lambda s: -float(s.get("monthly_est", 0) or 0))[:6]
        sub_lines = [f"{s['merchant']} ${s.get('amount',0):.2f}/{s.get('cadence','?')} "
                     f"(next ~{s.get('next_est','?')})" for s in top]
        lines.append(f"Subscriptions ~${monthly:,.0f}/mo: " + "; ".join(sub_lines))

    # Grocery budget context
    weekly = profile.get("weekly_budget_usd")
    if weekly:
        lines.append(f"Grocery budget: ${float(weekly):,.0f}/wk (~${float(weekly)*4.3:,.0f}/mo).")

    # Recent guardian alerts
    if alerts:
        recent = [a.get("text", "") for a in alerts[:3]]
        lines.append("Recent flags: " + "; ".join(recent) + ".")

    return "\n".join(lines)
