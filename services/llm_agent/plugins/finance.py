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
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(_endpoint(), headers=_headers(), json=body)
            if resp.status_code == 401:
                return {"error": "Invalid or revoked BankSync API key (401)."}
            if resp.status_code == 402 or resp.status_code == 403:
                return {"error": f"BankSync access denied ({resp.status_code}) — check plan tier and key scopes."}
            if resp.status_code == 429:
                return {"error": "BankSync rate limit hit (429). Back off and retry."}
            return _parse_response(resp)
    except Exception as e:
        return {"error": str(e)}


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


def sofi_balance() -> str:
    """Get current account balances across connected banks (incl. SoFi)."""
    res = _call_tool("list_accounts", {})
    if res.get("error"):
        return f"Balance fetch failed: {res['error']}"
    data = res.get("data")
    if isinstance(data, str):
        return data
    accts = data.get("accounts", data) if isinstance(data, dict) else data
    if not accts:
        return "No accounts returned."
    lines = []
    for a in accts if isinstance(accts, list) else []:
        name = a.get("name") or a.get("type") or "account"
        bal = a.get("balance", a.get("available", a.get("current", "?")))
        lines.append(f"{name}: ${bal}")
    return "; ".join(lines) if lines else json.dumps(data)[:1500]


def sofi_sync(days: int = 30) -> str:
    """Trigger a fresh sync of recent transactions from connected banks.

    Args:
        days: How many days back the downstream feed should cover (informational).
    """
    res = _call_tool("get_transactions", {"days": days})
    if res.get("error"):
        return f"Sync failed: {res['error']}"
    data = res.get("data")
    if isinstance(data, dict):
        txns = data.get("transactions", [])
        return f"Fetched {len(txns)} transactions from the last {days} days."
    if isinstance(data, list):
        return f"Fetched {len(data)} transactions from the last {days} days."
    return str(data)[:1500]


def sofi_spending(days: int = 30) -> str:
    """Summarise spending by category over the given period.

    Args:
        days: Lookback window in days (default 30).
    """
    res = _call_tool("get_transactions", {"days": days})
    if res.get("error"):
        return f"Spending fetch failed: {res['error']}"
    data = res.get("data")
    txns = []
    if isinstance(data, dict):
        txns = data.get("transactions", [])
    elif isinstance(data, list):
        txns = data
    if not txns:
        return "No transactions found for that period."
    by_cat: dict[str, float] = {}
    total = 0.0
    for t in txns:
        try:
            amt = float(t.get("amount", 0) or 0)
        except (TypeError, ValueError):
            continue
        if amt < 0:  # outflow
            cat = t.get("category", "Uncategorized") or "Uncategorized"
            by_cat[cat] = by_cat.get(cat, 0.0) + abs(amt)
            total += abs(amt)
    if not by_cat:
        return "No spending (outflow) transactions found in that period."
    ranked = sorted(by_cat.items(), key=lambda x: -x[1])
    lines = [f"{c}: ${v:,.2f}" for c, v in ranked]
    return f"Total spending ${total:,.2f} over {days} days. " + "; ".join(lines)


def get_tools():
    """Registry entry point."""
    from langchain_core.tools import tool as _tool
    return [
        _tool(sofi_balance),
        _tool(sofi_sync),
        _tool(sofi_spending),
        _tool(banksync_call),
    ]


# Back-compat: some loaders look for a TOOLS list.
TOOLS = [sofi_balance, sofi_sync, sofi_spending, banksync_call]
