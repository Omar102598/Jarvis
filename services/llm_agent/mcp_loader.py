"""MCP (Model Context Protocol) tool loader for JARVIS.

Reads config/mcp_servers.yml, connects to each enabled server, and returns
LangChain-compatible tools that Jarvis's core agent can call like any other tool.

Written against langchain-mcp-adapters >= 0.3 (MultiServerMCPClient is no
longer an async context manager — tools come from ``await client.get_tools()``
or from an explicit ``client.session(name)``).

Two session models, chosen per server in mcp_servers.yml:

  stateless (default)
      Tools from ``client.get_tools()`` — every invocation opens a fresh
      connection/subprocess, calls the tool, and disconnects. Right for
      servers where calls are independent (fetch, filesystem, github).

  persistent (``persistent: true``)
      One dedicated daemon thread + event loop per server holds a single MCP
      session open for the life of the process. Tool calls hop onto that loop.
      REQUIRED for stateful servers: Playwright's browser must keep page state
      between browser_navigate and the next call — a per-call session would
      launch a brand-new browser for every step. Also right for the knowledge-
      graph memory server (avoids a subprocess spawn per call).

Reloading:
    load_mcp_tools() closes any existing persistent sessions and rebuilds from
    the current YAML. The install_mcp_server tool writes to mcp_servers.yml
    and queues a graph rebuild via the Redis plugin reload queue.
"""

import asyncio
import concurrent.futures
import os
import threading
from pathlib import Path
from typing import Any

import yaml
from langchain_core.tools import BaseTool, StructuredTool

# Path to config — resolved relative to the repo root (via volume mount in Docker)
_CONFIG_PATH = Path(os.environ.get("MCP_SERVERS_CONFIG", "/config/mcp_servers.yml"))

# Per-call timeout for tool invocations (browser steps can be slow)
_TOOL_TIMEOUT_S = 120


def _read_config() -> dict:
    if not _CONFIG_PATH.exists():
        print(f"[MCP] Config not found at {_CONFIG_PATH} — no MCP servers loaded.")
        return {}
    with _CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {}


def _expand_env(value: str) -> str:
    """Expand ${VAR} references in string values."""
    import re
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), ""),
        value,
    )


def _build_client_config(name: str, cfg: dict) -> dict:
    """Build a config dict for MultiServerMCPClient from our YAML entry."""
    transport = cfg.get("transport", "stdio")
    entry: dict[str, Any] = {"transport": transport}

    if transport == "stdio":
        entry["command"] = _expand_env(str(cfg["command"]))
        entry["args"] = [_expand_env(str(a)) for a in cfg.get("args", [])]
        raw_env = cfg.get("env", {})
        if raw_env:
            entry["env"] = {k: _expand_env(str(v)) for k, v in raw_env.items()}
    elif transport in ("streamable_http", "sse"):
        # Expanded so a service URL can differ per deployment: HA lives in the
        # compose network on an all-in-one host, but across the tailnet once the
        # core stack moves to a VPS. Hardcoding it silently drops 20 HA tools.
        entry["url"] = _expand_env(str(cfg["url"]))
        raw_headers = cfg.get("headers", {})
        if raw_headers:
            entry["headers"] = {k: _expand_env(str(v)) for k, v in raw_headers.items()}
    else:
        raise ValueError(f"Unknown MCP transport '{transport}' for server '{name}'")

    return entry


# ---------------------------------------------------------------------------
# Persistent sessions — one thread + event loop + open MCP session per server
# ---------------------------------------------------------------------------

class _PersistentSession:
    """Holds one MCP session open on a dedicated event-loop thread.

    The session (and any process behind it, e.g. a Playwright browser) lives
    until close(). Tools are re-wrapped so calls from ANY loop/thread are
    marshalled onto this session's loop.
    """

    def __init__(self, name: str, client_cfg: dict):
        self.name = name
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, daemon=True, name=f"mcp-{name}"
        )
        self._thread.start()
        self._session_cm = None
        fut = asyncio.run_coroutine_threadsafe(self._open(client_cfg), self.loop)
        self.raw_tools = fut.result(timeout=90)

    async def _open(self, client_cfg: dict):
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools as _adapter_load

        client = MultiServerMCPClient({self.name: client_cfg})
        self._session_cm = client.session(self.name)
        session = await self._session_cm.__aenter__()
        return await _adapter_load(session)

    def wrapped_tools(self) -> list[BaseTool]:
        return [self._wrap(t) for t in self.raw_tools]

    def _wrap(self, tool: BaseTool) -> BaseTool:
        loop = self.loop
        server = self.name

        async def _arun(**kwargs: Any) -> str:
            fut = asyncio.run_coroutine_threadsafe(tool.ainvoke(kwargs), loop)
            try:
                return await asyncio.wait_for(asyncio.wrap_future(fut), _TOOL_TIMEOUT_S)
            except Exception as e:
                return f"[MCP:{server}] Error calling '{tool.name}': {e}"

        def _run(**kwargs: Any) -> str:
            fut = asyncio.run_coroutine_threadsafe(tool.ainvoke(kwargs), loop)
            try:
                return fut.result(timeout=_TOOL_TIMEOUT_S)
            except Exception as e:
                return f"[MCP:{server}] Error calling '{tool.name}': {e}"

        return StructuredTool(
            name=tool.name,
            description=f"[MCP:{server}] {tool.description}",
            args_schema=tool.args_schema,
            func=_run,
            coroutine=_arun,
        )

    def close(self) -> None:
        async def _close():
            if self._session_cm is not None:
                try:
                    await self._session_cm.__aexit__(None, None, None)
                except Exception:
                    pass
        try:
            asyncio.run_coroutine_threadsafe(_close(), self.loop).result(timeout=15)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


_persistent_sessions: dict[str, _PersistentSession] = {}
_sessions_lock = threading.Lock()


def _close_persistent_sessions() -> None:
    with _sessions_lock:
        for name, sess in list(_persistent_sessions.items()):
            print(f"[MCP] Closing persistent session '{name}'")
            sess.close()
            _persistent_sessions.pop(name, None)


# ---------------------------------------------------------------------------
# Stateless discovery (per-call sessions handled by the adapter itself)
# ---------------------------------------------------------------------------

async def _discover_stateless(name: str, client_cfg: dict) -> list[BaseTool]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({name: client_cfg})
    tools = await client.get_tools()
    # Prefix descriptions so the agent knows the tool's origin
    for t in tools:
        t.description = f"[MCP:{name}] {t.description}"
    return tools


def load_tools_for_server(name: str, cfg: dict) -> list[BaseTool]:
    """Turn one MCP server config into usable LangChain tools.

    ``cfg`` may be our YAML shape or an already-built client entry (plugins).
    Stateless servers get the adapter's own per-call tools; servers marked
    ``persistent: true`` get a held-open session on a dedicated thread.

    Reused by both ``load_mcp_tools`` (mcp_servers.yml) and the plugin
    registry (plugin-declared MCP servers) — one MCP tool-loading path.
    """
    try:
        client_cfg = _build_client_config(name, cfg)
    except Exception:
        client_cfg = dict(cfg)  # assume the plugin already supplied a client entry
        client_cfg.pop("persistent", None)

    persistent = bool(cfg.get("persistent", False))

    try:
        if persistent:
            with _sessions_lock:
                old = _persistent_sessions.pop(name, None)
            if old:
                old.close()
            sess = _PersistentSession(name, client_cfg)
            with _sessions_lock:
                _persistent_sessions[name] = sess
            return _filter_tools(name, cfg, sess.wrapped_tools())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _discover_stateless(name, client_cfg))
            return _filter_tools(name, cfg, future.result(timeout=60))
    except Exception as e:
        print(f"[MCP] Could not load server '{name}': {e}")
        return []


def _filter_tools(name: str, cfg: dict, tools: list) -> list:
    """Apply per-server ``allow_tools`` / ``deny_tools`` from mcp_servers.yml.

    Third-party MCP servers often ship far more authority than we want to hand
    the brain. The airline servers are the motivating case: they expose
    ``checkout``, ``cancel_trip`` and ``change_flight`` sitting right next to
    ``search_flights``, and a flight booking is real money on a real account.

    Filtering happens at LOAD time, not call time, so an excluded tool never
    enters the graph at all — there is no prompt that can reach it, and no
    reliance on the model choosing to behave. ``allow_tools`` is the safer
    shape: a server that adds a dangerous tool in a later release stays
    filtered out by default instead of silently gaining it.
    """
    allow = {str(t).strip() for t in (cfg.get("allow_tools") or []) if str(t).strip()}
    deny = {str(t).strip() for t in (cfg.get("deny_tools") or []) if str(t).strip()}
    if not allow and not deny:
        return tools

    def _names(tool) -> set:
        full = getattr(tool, "name", "") or ""
        # adapters sometimes namespace as "server__tool" — match either form
        return {full, full.split("__")[-1]}

    kept = []
    for tool in tools:
        names = _names(tool)
        if allow and not (names & allow):
            continue
        if deny and (names & deny):
            continue
        kept.append(tool)

    dropped = len(tools) - len(kept)
    if dropped:
        print(f"[MCP] '{name}': withheld {dropped} tool(s) by policy")

    # An allowlist that silently matches nothing is indistinguishable from a
    # broken server, so say so — upstream renames are the usual cause.
    if allow:
        matched = set().union(*(_names(t) for t in kept)) if kept else set()
        missing = allow - matched
        if missing:
            print(f"[MCP] '{name}': allow_tools not found upstream: {sorted(missing)}")
    return kept


def load_mcp_tools() -> list[BaseTool]:
    """Read mcp_servers.yml, discover tools from enabled servers, return tools.

    Called at startup and on reload. Closes stale persistent sessions first so
    a reload never leaks browsers/subprocesses.
    """
    _close_persistent_sessions()

    config = _read_config()
    servers = config.get("servers", {})
    enabled = {k: v for k, v in servers.items() if v.get("enabled", False)}

    if not enabled:
        print("[MCP] No servers enabled in mcp_servers.yml.")
        return []

    tools: list[BaseTool] = []
    for name, cfg in enabled.items():
        server_tools = load_tools_for_server(name, cfg)
        mode = "persistent" if cfg.get("persistent") else "stateless"
        print(f"[MCP] '{name}' ({mode}): {len(server_tools)} tool(s)")
        tools.extend(server_tools)

    print(f"[MCP] Total: {len(tools)} tool(s) from {len(enabled)} server(s)")
    return tools


def reload_mcp_tools() -> list[BaseTool]:
    """Alias for load_mcp_tools() — called by the plugin reload watcher."""
    return load_mcp_tools()
