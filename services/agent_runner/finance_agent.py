"""Finance Widget Agent — populates the dashboard finance widget from BankSync.

The dashboard's finance widget reads ``widget:finance:data`` from Redis (the
dashboard just serves whatever a producer writes there). Nothing was writing it,
so the widget rendered empty. This agent is that producer: on a schedule it pulls
balances + recent spending from the BankSync remote MCP (same source the
llm_agent finance plugin uses) and writes the widget's data shape to Redis.

Env: BANKSYNC_API_KEY (bsk_...), BANKSYNC_MCP_URL (default https://mcp.banksync.io)

Widget data shape written to ``widget:finance:data``:
  {
    "total_cash": float,          # sum of asset (non-loan) balances
    "available_balance": float,   # sum of available balances on asset accounts
    "total_spent_30d": float,
    "budgets": [{"category": str, "spent": float}, ...],
    "updated": "HH:MM"            # local time string
    // or {"error": "..."} if BankSync is unreachable / unconfigured
  }
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import aiohttp

from base_agent import BaseAgent
from llm_helper import complete

_DEFAULT_URL = "https://mcp.banksync.io"
WIDGET_KEY = "widget:finance:data"
REPORT_KEY = "finance:report"       # LLM-written report text (read by get_financial_report tool)
BUDGETS_KEY = "finance:budgets"     # structured recommended budgets
# Categories that are money movement, not discretionary spending
_NON_SPEND = ("transfer", "loan payment", "credit card payment", "payment")


def _log(msg: str) -> None:
    print(f"[FinanceAgent] {msg}", flush=True)


def _is_liability(a: dict) -> bool:
    hay = f"{a.get('accountType','')} {a.get('accountName','')}".lower()
    return any(w in hay for w in ("loan", "credit", "mortgage", "liability"))


class _BankSync:
    """Minimal async BankSync MCP (Streamable HTTP / JSON-RPC) client."""

    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self.url = os.environ.get("BANKSYNC_MCP_URL", _DEFAULT_URL).rstrip("/")
        self.key = os.environ.get("BANKSYNC_API_KEY", "").strip()
        self.sid: str | None = None

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-API-Key": self.key,
        }
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        return h

    async def _rpc(self, method: str, params: dict | None = None, _id: int = 1) -> dict:
        body = {"jsonrpc": "2.0", "id": _id, "method": method}
        if params is not None:
            body["params"] = params
        # Guard the call: a single BankSync timeout must not raise out of the whole run.
        try:
            async with self.s.post(
                self.url, headers=self._headers(), json=body,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
                if sid:
                    self.sid = sid
                text = await resp.text()
                if "text/event-stream" in resp.headers.get("content-type", ""):
                    payload = {}
                    for line in text.splitlines():
                        line = line.strip()
                        if line.startswith("data:"):
                            chunk = line[5:].strip()
                            if chunk:
                                try:
                                    payload = json.loads(chunk)
                                except json.JSONDecodeError:
                                    continue
                    return payload
                try:
                    return json.loads(text)
                except Exception:
                    return {"error": f"unparseable: {text[:120]}"}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    async def initialize(self) -> bool:
        if not self.key:
            return False
        init = await self._rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "jarvis-finance-widget", "version": "1.0"},
        })
        if init.get("error"):
            return False
        try:
            async with self.s.post(
                self.url, headers=self._headers(),
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                timeout=aiohttp.ClientTimeout(total=15),
            ):
                pass
        except Exception:
            pass
        return True

    async def call(self, name: str, args: dict | None = None):
        res = await self._rpc("tools/call", {"name": name, "arguments": args or {}}, _id=2)
        if res.get("error"):
            return None
        result = res.get("result", res)
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            joined = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
            try:
                return json.loads(joined)
            except Exception:
                return joined
        return result


class FinanceAgent(BaseAgent):
    async def run(self) -> str:
        # params: {"action": "report"} forces a fresh LLM report regardless of cache.
        force_report = (self.params or {}).get("action") == "report"

        if not os.environ.get("BANKSYNC_API_KEY", "").strip():
            self._write({"error": "BankSync not configured"})
            return "BankSync not configured (no BANKSYNC_API_KEY)."

        async with aiohttp.ClientSession() as session:
            bs = _BankSync(session)
            if not await bs.initialize():
                self._write({"error": "BankSync unreachable"})
                return "BankSync handshake failed."

            banks = await bs.call("list_banks", {}) or []
            if isinstance(banks, dict):
                banks = banks.get("banks", [])

            total_cash = available = debt = 0.0
            accounts: list[tuple[str, str]] = []   # (bankId, accountId)
            acct_summ: list[str] = []
            for b in banks:
                bid = b.get("id", "")
                accts = await bs.call("list_accounts", {"bankId": bid}) or []
                if isinstance(accts, dict):
                    accts = accts.get("accounts", [])
                for a in accts:
                    accounts.append((bid, a.get("id", "")))
                    try:
                        bal = float(a.get("balance", 0) or 0)
                    except (TypeError, ValueError):
                        bal = 0.0
                    nm = a.get("accountName") or a.get("accountType") or "account"
                    acct_summ.append(f"{nm}: ${bal:,.2f}")
                    if _is_liability(a):
                        debt += bal
                    else:
                        total_cash += bal
                        try:
                            available += float(a.get("availableBalance", bal) or 0)
                        except (TypeError, ValueError):
                            pass

            # 30-day cashflow: split true spending from money movement, and tally income
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            by_cat: dict[str, float] = {}
            total_spent = income = 0.0
            for bid, aid in accounts:
                txns = await bs.call("get_transactions", {"bankId": bid, "accountId": aid}) or []
                if isinstance(txns, dict):
                    txns = txns.get("transactions", txns.get("data", []))
                for t in txns if isinstance(txns, list) else []:
                    try:
                        amt = float(t.get("amount"))
                    except (TypeError, ValueError):
                        continue
                    raw = t.get("date") or t.get("postedAt") or t.get("transactedAt")
                    if raw:
                        try:
                            d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                            if d.tzinfo is None:
                                d = d.replace(tzinfo=timezone.utc)
                            if d < cutoff:
                                continue
                        except Exception:
                            pass
                    cat = (t.get("category") or t.get("merchantCategory") or "Other")
                    if amt >= 0:
                        income += amt
                        continue
                    if any(w in cat.lower() for w in _NON_SPEND):
                        continue  # transfers / loan payments aren't discretionary spend
                    by_cat[cat] = by_cat.get(cat, 0.0) + abs(amt)
                    total_spent += abs(amt)

            budgets = [
                {"category": c, "spent": round(v, 2)}
                for c, v in sorted(by_cat.items(), key=lambda x: -x[1])[:6]
            ]

            # ── widget data (mechanical, every run) ────────────────────
            data = {
                "total_cash": round(total_cash, 2),
                "available_balance": round(available, 2),
                "total_spent_30d": round(total_spent, 2),
                "income_30d": round(income, 2),
                "budgets": budgets,
                "updated": datetime.now().strftime("%H:%M"),
            }
            self._write(data)
            _log(f"  ✓ widget: cash ${total_cash:,.0f}, spent30d ${total_spent:,.0f}, "
                 f"income30d ${income:,.0f}, {len(budgets)} categories")

            # ── LLM report + budgets + advice (daily cache) ────────────
            if force_report or self._report_stale():
                report = await self._generate_report(
                    total_cash, available, debt, income, total_spent, by_cat, acct_summ
                )
                if report:
                    return report

        return (f"Finance widget refreshed: ${total_cash:,.0f} cash, "
                f"${total_spent:,.0f} spent / ${income:,.0f} income (30d).")

    # ------------------------------------------------------------------
    def _report_stale(self, max_age_h: int = 20) -> bool:
        try:
            raw = self.r.get(REPORT_KEY)
            if not raw:
                return True
            gen = json.loads(raw).get("generated_at", "")
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(gen)).total_seconds() / 3600
            return age >= max_age_h
        except Exception:
            return True

    async def _generate_report(
        self, cash, available, debt, income, spent, by_cat, acct_summ
    ) -> str:
        """Ask the LLM (financial-advisor persona) for a budget + report + advice."""
        _log("  Generating financial report via LLM…")
        spend_lines = "; ".join(f"{c}: ${v:,.0f}" for c, v in
                                sorted(by_cat.items(), key=lambda x: -x[1])[:8]) or "none"

        system = (
            "You are JARVIS, a sharp but concise British financial advisor. From the "
            "user's real account data produce practical guidance. Note that transfers "
            "and loan payments have already been excluded from 'spending'. Recommend "
            "realistic MONTHLY budgets per major category (grounded in their income and "
            "current spend), and give specific, actionable advice.\n"
            "Return ONLY valid JSON, no markdown:\n"
            '{"summary": "one-line financial health read", '
            '"budgets": [{"category": "Food And Drink", "recommended_monthly": 600}], '
            '"advice": ["specific tip 1", "specific tip 2", "specific tip 3"]}'
        )
        user = (
            f"Cash (assets): ${cash:,.2f}; available ${available:,.2f}; "
            f"total debt ${debt:,.2f}.\n"
            f"Last 30 days — income ${income:,.2f}, discretionary spending ${spent:,.2f}.\n"
            f"Spending by category (30d): {spend_lines}.\n"
            f"Accounts: {'; '.join(acct_summ[:12])}.\n"
            "Produce the budget, summary, and advice now."
        )
        try:
            resp = await complete(system=system, user=user, max_tokens=700)
            import re
            m = re.search(r"\{.*\}", resp or "", re.DOTALL)
            parsed = json.loads(m.group()) if m else {}
        except Exception as exc:
            _log(f"  ✗ report generation failed: {exc}")
            return ""

        summary = parsed.get("summary", "")
        rec_budgets = parsed.get("budgets", []) or []
        advice = parsed.get("advice", []) or []

        lines = [
            "J·A·R·V·I·S — FINANCIAL REPORT",
            datetime.now().strftime("%A, %B %-d"),
            "",
            summary,
            "",
            f"Cash: ${cash:,.0f}  |  Debt: ${debt:,.0f}  |  Net: ${cash - debt:,.0f}",
            f"Last 30d: +${income:,.0f} in, -${spent:,.0f} spent",
        ]
        if rec_budgets:
            lines += ["", "SUGGESTED MONTHLY BUDGETS"]
            for b in rec_budgets[:8]:
                try:
                    lines.append(f"  {b.get('category','?')}: ${float(b.get('recommended_monthly',0)):,.0f}")
                except (TypeError, ValueError):
                    continue
        if advice:
            lines += ["", "ADVICE"]
            for a in advice[:4]:
                lines.append(f"  • {a}")
        report = "\n".join(lines)

        self.r.set(REPORT_KEY, json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "report": report,
            "summary": summary,
            "budgets": rec_budgets,
            "advice": advice,
        }), ex=1209600)  # 2 weeks
        self.r.set(BUDGETS_KEY, json.dumps(rec_budgets))
        _log(f"  ✓ report generated ({len(rec_budgets)} budgets, {len(advice)} tips)")
        return report

    def _write(self, data: dict) -> None:
        try:
            self.r.set(WIDGET_KEY, json.dumps(data))
        except Exception as exc:
            _log(f"  ✗ Redis write failed: {exc}")
