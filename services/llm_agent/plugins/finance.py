"""Finance plugin — SoFi / bank data via the BankSync remote MCP server.

BankSync hosts a remote MCP server at https://mcp.banksync.io speaking the
MCP Streamable HTTP transport. We talk to it directly over JSON-RPC with httpx
(no extra SDK needed), authenticating with a workspace API key in the
``X-API-Key`` header.

Config (environment variables, provided at runtime):
  BANKSYNC_API_KEY   workspace key, starts with ``bsk_``  (required)
  BANKSYNC_MCP_URL   override the endpoint (default https://mcp.banksync.io)

The plugin exposes three convenience tools (sync, balance, spending) plus a
generic ``banksync_call`` escape hatch so the agent can reach any MCP tool the
key's scopes allow (list_accounts, get_transactions, get_balance, get_holdings,
trigger syncs, etc.).
"""
import json
import os
import time

import httpx

_DEFAULT_URL = "https://mcp.banksync.io"
_SESSION = {"id": None, "initialized": False}


def _endpoint() -> str:
    return os.environ.get("BANKSYNC_MCP_URL", _DEFAULT_URL).rstrip("/")


def _key() -> str:
    return os.environ.get("BANKSYNC_API_KEY", "").strip()


def _headers() -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-API-Key": _key(),
    }
    if _SESSION["id"]:
        h["Mcp-Session-Id"] = _SESSION["id"]
    return h


def _parse_response(resp: httpx.Response) -> dict:
    """MCP Streamable HTTP replies as JSON or as an SSE stream. Handle both."""
    # Capture a session id if the server assigned one.
    sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
    if sid:
        _SESSION["id"] = sid
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        # Pull the last JSON data: line from the SSE stream.
        payload = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[len("data:"):].strip()
                if chunk:
                    try:
                        payload = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
        return payload or {}
    try:
        return resp.json()
    except Exception:
        try:
            return json.loads(text)
        except Exception:
            return {"error": f"Unparseable response: {text[:200]}"}


def _rpc(method: str, params: dict | None = None, _id: int = 1) -> dict:
    if not _key():
        return {"error": "BankSync not configured. Set BANKSYNC_API_KEY (starts with bsk_)."}
    body = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        body["params"] = params

    # BankSync's hosted MCP occasionally stalls; retry transient timeouts/5xx
    # with backoff rather than failing the whole tool call on one slow response.
    last_err = "unknown error"
    for attempt in range(3):
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(_endpoint(), headers=_headers(), json=body)
                if resp.status_code == 401:
                    return {"error": "Invalid or revoked BankSync API key (401)."}
                if resp.status_code in (402, 403):
                    return {"error": f"BankSync access denied ({resp.status_code}) — check plan tier and key scopes."}
                if resp.status_code == 429:
                    last_err = "BankSync rate limit hit (429)."
                elif resp.status_code >= 500:
                    last_err = f"BankSync server error ({resp.status_code})."
                else:
                    return _parse_response(resp)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = f"BankSync unreachable ({type(e).__name__}) — server slow or down."
        except Exception as e:
            return {"error": str(e)}
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    return {"error": last_err + " Retried 3×; try again shortly."}


def _ensure_session() -> dict | None:
    """Run the MCP initialize handshake once per process. Returns error dict or None."""
    if _SESSION["initialized"]:
        return None
    if not _key():
        return {"error": "BankSync not configured. Set BANKSYNC_API_KEY (starts with bsk_)."}
    init = _rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "jarvis", "version": "1.0"},
    })
    if init.get("error"):
        return init
    # Notify initialized (best effort; no response expected).
    try:
        with httpx.Client(timeout=15) as client:
            client.post(_endpoint(), headers=_headers(), json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })
    except Exception:
        pass
    _SESSION["initialized"] = True
    return None


def _call_tool(name: str, arguments: dict | None = None) -> dict:
    err = _ensure_session()
    if err:
        return err
    res = _rpc("tools/call", {"name": name, "arguments": arguments or {}}, _id=2)
    if res.get("error"):
        # JSON-RPC error object or our own error string.
        e = res["error"]
        return {"error": e.get("message", e) if isinstance(e, dict) else e}
    result = res.get("result", res)
    # MCP tool results come back as a content list of {type, text}.
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        texts = [c.get("text", "") for c in content if isinstance(c, dict)]
        joined = "\n".join(t for t in texts if t)
        # Try to parse JSON payloads for downstream tools.
        try:
            return {"data": json.loads(joined)}
        except Exception:
            return {"data": joined}
    return {"data": result}


# ---------------------------------------------------------------------------
# Traversal helpers — BankSync is bank → accounts → transactions/balance, so the
# convenience tools must resolve the bankId/accountId chain, not call the leaf
# endpoints bare (they require those ids).
# ---------------------------------------------------------------------------

def _list_banks() -> list:
    res = _call_tool("list_banks", {})
    if res.get("error"):
        return []
    data = res.get("data")
    if isinstance(data, list):
        return data
    return data.get("banks", []) if isinstance(data, dict) else []


def _list_accounts(bank_id: str) -> list:
    res = _call_tool("list_accounts", {"bankId": bank_id})
    data = res.get("data")
    if isinstance(data, list):
        return data
    return data.get("accounts", []) if isinstance(data, dict) else []


def _iter_accounts() -> list[tuple[dict, dict]]:
    """Yield (bank, account) pairs across every connected bank."""
    out = []
    for b in _list_banks():
        for a in _list_accounts(b.get("id", "")):
            out.append((b, a))
    return out


def _account_transactions(bank_id: str, account_id: str) -> list:
    res = _call_tool("get_transactions", {"bankId": bank_id, "accountId": account_id})
    data = res.get("data")
    if isinstance(data, dict):
        return data.get("transactions", data.get("data", []))
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def banksync_call(tool: str, arguments_json: str = "{}") -> str:
    """Call any BankSync MCP tool directly by name.

    Use this for tools without a dedicated wrapper, e.g. 'list_accounts',
    'get_holdings', 'list_banks', 'get_transactions'.

    Args:
        tool: The MCP tool name to invoke.
        arguments_json: JSON object string of arguments, e.g. '{"days": 30}'.
    """
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as e:
        return f"Invalid arguments_json: {e}"
    res = _call_tool(tool, args)
    if res.get("error"):
        return f"BankSync error: {res['error']}"
    data = res.get("data")
    return data if isinstance(data, str) else json.dumps(data, indent=2)[:4000]


def _amount(t: dict) -> float | None:
    for k in ("amount", "value", "amountValue"):
        if t.get(k) is not None:
            try:
                return float(t[k])
            except (TypeError, ValueError):
                pass
    return None


def _within_days(t: dict, days: int) -> bool:
    """Keep a transaction if its date is within the window (or it has no date)."""
    raw = t.get("date") or t.get("postedAt") or t.get("transactedAt") or t.get("datetime")
    if not raw:
        return True
    try:
        from datetime import datetime, timezone, timedelta
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d >= datetime.now(timezone.utc) - timedelta(days=days)
    except Exception:
        return True


def _is_liability(account: dict) -> bool:
    """Loans / credit cards are debts, not assets — don't add them to net cash."""
    hay = f"{account.get('accountType','')} {account.get('accountName','')}".lower()
    return any(w in hay for w in ("loan", "credit", "mortgage", "liability"))


def sofi_balance() -> str:
    """Get current balances for every account across connected banks (incl. SoFi).

    Separates cash/asset accounts from loan/credit liabilities so the totals are
    meaningful (a $96k student loan is not $96k of cash).
    """
    pairs = _iter_accounts()
    if not pairs:
        return ("No accounts returned. Either no bank is linked in BankSync yet, or "
                "the key lacks account scope. Use banksync_call('list_banks') to check.")

    def _bal(a: dict) -> float:
        try:
            return float(a.get("balance", a.get("availableBalance", a.get("current", 0))) or 0)
        except (TypeError, ValueError):
            return 0.0

    asset_lines, debt_lines = [], []
    assets, debts = 0.0, 0.0
    for bank, a in pairs:
        bal = _bal(a)
        name = a.get("accountName") or a.get("accountType") or a.get("name") or "account"
        num = a.get("accountNumber", "")
        line = f"  {bank.get('name','Bank')} · {name} {num}: ${bal:,.2f}"
        if _is_liability(a):
            debts += bal
            debt_lines.append(line)
        else:
            assets += bal
            asset_lines.append(line)

    out = []
    if asset_lines:
        out.append("Cash / assets:")
        out += asset_lines
        out.append(f"  → Total cash: ${assets:,.2f}")
    if debt_lines:
        out.append("Loans / liabilities:")
        out += debt_lines
        out.append(f"  → Total owed: ${debts:,.2f}")
    out.append(f"Net position: ${assets - debts:,.2f}")
    return "\n".join(out)


def sofi_sync(days: int = 30) -> str:
    """Fetch recent transactions from all connected accounts.

    Args:
        days: Lookback window in days (default 30).
    """
    pairs = _iter_accounts()
    if not pairs:
        return "No accounts to sync — link a bank in BankSync first."
    total = 0
    for bank, a in pairs:
        txns = _account_transactions(bank.get("id", ""), a.get("id", ""))
        total += sum(1 for t in txns if _within_days(t, days))
    return f"Synced {total} transaction(s) from the last {days} days across {len(pairs)} account(s)."


def sofi_spending(days: int = 30) -> str:
    """Summarise spending (outflows) by category over the given period.

    Args:
        days: Lookback window in days (default 30).
    """
    pairs = _iter_accounts()
    if not pairs:
        return "No accounts found — link a bank in BankSync first."
    by_cat: dict[str, float] = {}
    total = 0.0
    for bank, a in pairs:
        for t in _account_transactions(bank.get("id", ""), a.get("id", "")):
            if not _within_days(t, days):
                continue
            amt = _amount(t)
            if amt is None or amt >= 0:  # keep outflows only
                continue
            cat = t.get("category") or t.get("merchantCategory") or "Uncategorized"
            by_cat[cat] = by_cat.get(cat, 0.0) + abs(amt)
            total += abs(amt)
    if not by_cat:
        return f"No spending (outflow) transactions found in the last {days} days."
    ranked = sorted(by_cat.items(), key=lambda x: -x[1])
    lines = [f"{c}: ${v:,.2f}" for c, v in ranked]
    return f"Total spending ${total:,.2f} over {days} days. " + "; ".join(lines)


def get_financial_report() -> str:
    """Return the latest AI financial report: budget recommendations and advice.

    The finance agent generates this daily from your BankSync accounts (balances,
    income, and spending). Use for "what's my financial report / advice", "how are
    my finances", or "what should my budget be".
    """
    try:
        import redis as _redis_lib
        r = _redis_lib.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)
        raw = r.get("finance:report")
    except Exception as e:
        return f"Couldn't read the financial report: {e}"
    if not raw:
        return ("No financial report yet, sir. It's generated daily from your accounts — "
                "say 'refresh my financial report' to build one now.")
    try:
        data = json.loads(raw)
        return data.get("report") or "The stored report appears empty."
    except Exception:
        return "The stored financial report is unreadable."


def _merchant_key(t: dict) -> str:
    """Normalize a transaction description to a stable merchant key."""
    import re as _re
    raw = str(t.get("description") or t.get("name") or t.get("merchant") or "").lower()
    raw = _re.sub(r"[#*]\S+", "", raw)          # ref codes like #4821, *X92
    raw = _re.sub(r"\d{2,}", "", raw)           # long digit runs (dates, ids)
    raw = _re.sub(r"\s+", " ", raw).strip()
    return raw[:40]


def detect_subscriptions(days: int = 90) -> str:
    """Find recurring charges (subscriptions) across all bank accounts.

    Scans the last N days of transactions, groups outgoing charges by merchant,
    and reports anything that recurs — monthly/weekly cadence, average cost,
    annualized total, and any price increases. Use when the user asks
    "what subscriptions am I paying for?", "any recurring charges?",
    or "where is my money leaking?".

    Args:
        days: How far back to scan (default 90 — three cycles of a monthly sub).
    """
    from datetime import datetime, timezone

    if not _key():
        return "BANKSYNC_API_KEY is not configured."

    merchants: dict[str, list[tuple[datetime, float]]] = {}
    try:
        for _bank, account in _iter_accounts():
            if _is_liability(account):
                continue
            txs = _account_transactions(
                account.get("bankId") or account.get("bank_id") or "",
                account.get("accountId") or account.get("id") or "",
            )
            for t in txs:
                if not _within_days(t, days):
                    continue
                amt = _amount(t)
                if amt is None or amt >= 0:   # outflows only (negative amounts)
                    continue
                raw_date = (t.get("date") or t.get("postedAt")
                            or t.get("transactedAt") or t.get("datetime"))
                try:
                    d = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                key = _merchant_key(t)
                if key:
                    merchants.setdefault(key, []).append((d, abs(amt)))
    except Exception as e:
        return f"Couldn't scan transactions: {e}"

    subs = []
    for key, charges in merchants.items():
        if len(charges) < 2:
            continue
        charges.sort()
        gaps = [(b[0] - a[0]).days for a, b in zip(charges, charges[1:])]
        avg_gap = sum(gaps) / len(gaps) if gaps else 0
        amounts = [c[1] for c in charges]
        avg_amt = sum(amounts) / len(amounts)
        spread = max(amounts) - min(amounts)
        # Recurring = regular cadence (weekly-ish to ~2-monthly) and similar amounts
        if not (5 <= avg_gap <= 65 and spread <= max(2.0, avg_amt * 0.25)):
            continue
        cadence = ("weekly" if avg_gap <= 10 else
                   "biweekly" if avg_gap <= 20 else
                   "monthly" if avg_gap <= 37 else "~every 2 months")
        yearly = avg_amt * (365 / max(avg_gap, 1))
        note = ""
        if amounts[-1] > amounts[0] + 0.5:
            note = f" ⚠ price rose ${amounts[0]:.2f} → ${amounts[-1]:.2f}"
        subs.append((yearly, f"  • {key.title()} — ${avg_amt:.2f} {cadence} "
                             f"(≈${yearly:,.0f}/yr, {len(charges)} charges, "
                             f"last {charges[-1][0].date()}){note}"))

    if not subs:
        return (f"No clear recurring charges found in the last {days} days. "
                "Either the accounts are clean or transactions lack dates.")

    subs.sort(reverse=True)
    total_yearly = sum(s[0] for s in subs)
    lines = [f"Recurring charges (last {days} days) — "
             f"≈${total_yearly:,.0f}/yr total across {len(subs)} subscriptions:"]
    lines += [s[1] for s in subs[:20]]
    return "\n".join(lines)


def get_tools():
    """Registry entry point."""
    from langchain_core.tools import tool as _tool
    return [
        _tool(sofi_balance),
        _tool(sofi_sync),
        _tool(sofi_spending),
        _tool(banksync_call),
        _tool(get_financial_report),
        _tool(detect_subscriptions),
    ]


# Back-compat: some loaders look for a TOOLS list.
TOOLS = [sofi_balance, sofi_sync, sofi_spending, banksync_call,
         get_financial_report, detect_subscriptions]
