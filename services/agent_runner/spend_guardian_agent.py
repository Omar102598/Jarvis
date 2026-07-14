"""Spend Guardian — Brad's watchful side.

The finance agent paints the dashboard; the guardian watches the transaction
feed for things worth flagging, using the same BankSync client:

  • Subscriptions   — detects recurring charges (merchant repeats on a monthly/
                      weekly/annual cadence), estimates the monthly total, and
                      predicts the next hit. New ones are surfaced.
  • Unusual charges — a recent one-off notably larger than your typical spend.
  • Low cash        — available balance under your buffer (profile cash_buffer_usd).

Everything is routed through the notification router (mostly digest urgency, so
it lands in your daily brief, not as interrupts). Results are also written to
Redis for the brain's get_spending_insights tool.
"""

from __future__ import annotations

import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone

import aiohttp

from base_agent import BaseAgent
from finance_agent import _BankSync, _is_liability, _NON_SPEND
from notify import route_notification

SUBS_KEY = "finance:subscriptions"
ALERTS_KEY = "finance:alerts"          # recent guardian alerts (list, newest-first)
LOOKBACK_DAYS = 95                     # enough to see ~3 monthly cycles
_MERCHANT_STRIP = re.compile(r"[0-9#*]+|\b\d{2}/\d{2}\b|\s{2,}")


def _norm_merchant(name: str) -> str:
    n = (name or "").lower()
    n = _MERCHANT_STRIP.sub(" ", n)
    return " ".join(n.split())[:40].strip()


class SpendGuardianAgent(BaseAgent):
    async def run(self) -> str:
        if not os.environ.get("BANKSYNC_API_KEY", "").strip():
            return "Spend Guardian: BankSync not configured."

        async with aiohttp.ClientSession() as session:
            bs = _BankSync(session)
            if not await bs.initialize():
                return "Spend Guardian: BankSync handshake failed."

            banks = await bs.call("list_banks", {}) or []
            if isinstance(banks, dict):
                banks = banks.get("banks", [])

            available = 0.0
            txns: list[dict] = []
            for b in banks:
                bid = b.get("id", "")
                accts = await bs.call("list_accounts", {"bankId": bid}) or []
                if isinstance(accts, dict):
                    accts = accts.get("accounts", [])
                for a in accts:
                    if not _is_liability(a):
                        try:
                            available += float(a.get("availableBalance",
                                                     a.get("balance", 0)) or 0)
                        except (TypeError, ValueError):
                            pass
                    raw = await bs.call("get_transactions",
                                        {"bankId": bid, "accountId": a.get("id", "")}) or []
                    if isinstance(raw, dict):
                        raw = raw.get("transactions", raw.get("data", []))
                    if isinstance(raw, list):
                        txns.extend(raw)

        parsed = self._parse_txns(txns)
        subs = self._detect_subscriptions(parsed)
        findings = []

        findings.append(self._report_subscriptions(subs))
        findings.append(self._check_unusual(parsed))
        findings.append(self._check_low_cash(available))

        self.r.set(SUBS_KEY, json.dumps(subs))
        done = [f for f in findings if f]
        return "Spend Guardian: " + ("; ".join(done) if done else "nothing notable.")

    # ------------------------------------------------------------------ parse
    def _parse_txns(self, txns: list) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        out = []
        for t in txns if isinstance(txns, list) else []:
            try:
                amt = float(t.get("amount"))
            except (TypeError, ValueError):
                continue
            raw = t.get("date") or t.get("postedAt") or t.get("transactedAt")
            try:
                d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if d < cutoff or amt >= 0:
                continue
            cat = (t.get("category") or t.get("merchantCategory") or "Other")
            if any(w in cat.lower() for w in _NON_SPEND):
                continue
            merchant = t.get("merchantName") or t.get("name") or t.get("description") or "?"
            out.append({
                "id": t.get("id") or t.get("transactionId") or f"{merchant}:{raw}",
                "merchant": merchant,
                "norm": _norm_merchant(merchant),
                "amount": abs(amt),
                "date": d,
            })
        return out

    # ---------------------------------------------------------- subscriptions
    def _detect_subscriptions(self, txns: list[dict]) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for t in txns:
            if t["norm"]:
                groups.setdefault(t["norm"], []).append(t)

        subs = []
        for norm, items in groups.items():
            if len(items) < 2:
                continue
            items.sort(key=lambda x: x["date"])
            amounts = [i["amount"] for i in items]
            med_amt = statistics.median(amounts)
            # amounts must be consistent (recurring bills are stable)
            if med_amt <= 0 or (max(amounts) - min(amounts)) > max(2.0, 0.20 * med_amt):
                continue
            intervals = [(items[i]["date"] - items[i - 1]["date"]).days
                         for i in range(1, len(items))]
            if not intervals:
                continue
            med_int = statistics.median(intervals)
            cadence = self._cadence(med_int)
            if cadence is None:
                continue
            monthly = med_amt * (30.0 / med_int) if med_int else med_amt
            last = items[-1]["date"]
            subs.append({
                "merchant": items[-1]["merchant"],
                "amount": round(med_amt, 2),
                "cadence": cadence,
                "monthly_est": round(monthly, 2),
                "count": len(items),
                "last_date": last.strftime("%Y-%m-%d"),
                "next_est": (last + timedelta(days=round(med_int))).strftime("%Y-%m-%d"),
            })
        subs.sort(key=lambda s: -s["monthly_est"])
        return subs

    @staticmethod
    def _cadence(days: float) -> str | None:
        if 5 <= days <= 9:
            return "weekly"
        if 12 <= days <= 16:
            return "biweekly"
        if 25 <= days <= 35:
            return "monthly"
        if 85 <= days <= 100:
            return "quarterly"
        if 350 <= days <= 380:
            return "annual"
        return None

    def _report_subscriptions(self, subs: list[dict]) -> str:
        if not subs:
            return ""
        # Surface subscriptions newly detected since last run.
        try:
            prev = {s["merchant"] for s in json.loads(self.r.get(SUBS_KEY) or "[]")}
        except Exception:
            prev = set()
        new = [s for s in subs if s["merchant"] not in prev]
        monthly_total = sum(s["monthly_est"] for s in subs)
        if new:
            lines = [f"{s['merchant']} ~${s['amount']:.2f}/{s['cadence']}" for s in new[:5]]
            route_notification(
                "Brad",
                f"New recurring charge{'s' if len(new) > 1 else ''} detected: "
                + "; ".join(lines)
                + f". Your tracked subscriptions total ~${monthly_total:,.0f}/mo.",
                title="💳 Subscription watch",
                urgency="normal",
                dedup_key="spend:new_subs:" + ",".join(sorted(s["merchant"] for s in new)),
            )
        return f"{len(subs)} subscriptions (~${monthly_total:,.0f}/mo)"

    # -------------------------------------------------------- unusual charges
    def _check_unusual(self, txns: list[dict]) -> str:
        recent_cut = datetime.now(timezone.utc) - timedelta(days=2)
        month = [t for t in txns if t["date"] >= datetime.now(timezone.utc) - timedelta(days=30)]
        if len(month) < 6:
            return ""
        amounts = [t["amount"] for t in month]
        med = statistics.median(amounts)
        threshold = max(150.0, 4 * med)
        flagged = []
        for t in txns:
            if t["date"] < recent_cut or t["amount"] < threshold:
                continue
            if self.r.sismember("finance:alerted", t["id"]):
                continue
            flagged.append(t)
            self.r.sadd("finance:alerted", t["id"])
            self.r.expire("finance:alerted", 60 * 60 * 24 * 45)
        for t in flagged[:3]:
            self._log_alert(f"Large charge: {t['merchant']} ${t['amount']:,.2f}")
            route_notification(
                "Brad",
                f"Unusual charge: ${t['amount']:,.2f} at {t['merchant']} "
                f"— well above your typical ${med:,.0f}. Recognise it?",
                title="⚠️ Large charge",
                urgency="normal",
                dedup_key=f"spend:unusual:{t['id']}",
            )
        return f"{len(flagged)} unusual charge(s)" if flagged else ""

    # --------------------------------------------------------------- low cash
    def _check_low_cash(self, available: float) -> str:
        try:
            profile = json.loads(self.r.get("user:profile") or "{}")
        except Exception:
            profile = {}
        buffer = float(profile.get("cash_buffer_usd", 500))
        if available >= buffer or available <= 0:
            return ""
        self._log_alert(f"Low cash: ${available:,.0f} < ${buffer:,.0f} buffer")
        route_notification(
            "Brad",
            f"Heads up — available cash is ${available:,.0f}, below your "
            f"${buffer:,.0f} buffer.",
            title="💸 Low balance",
            urgency="normal",
            dedup_key="spend:low_cash",
        )
        return f"low cash (${available:,.0f})"

    def _log_alert(self, text: str) -> None:
        try:
            self.r.lpush(ALERTS_KEY, json.dumps({
                "text": text,
                "ts": datetime.now(timezone.utc).isoformat(),
            }))
            self.r.ltrim(ALERTS_KEY, 0, 49)
        except Exception:
            pass
