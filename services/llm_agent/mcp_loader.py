"""MCP (Model Context Protocol) tool loader for JARVIS.

Reads config/mcp_servers.yml, connects to each enabled server, and returns
LangChain-compatible tools that Jarvis's core agent can call like any other tool.

Design: per-call connections
    Each tool invocation opens a fresh connection to its MCP server, calls the
    tool, and closes the connection. This is slightly slower than a persistent
    connection but works cleanly with Jarvis's per-request asyncio.run() pattern
    and requires no background thread or event-loop bridging.

    For stdio servers (npx-based), this means one subprocess spawn per call.
    For HTTP servers (streamable_http / sse), this means one HTTP request per call.
    Both are acceptable for Jarvis's typical usage pattern (a few tool calls per
    conversation turn).

Reloading:
    Call load_mcp_tools() again (e.g., after editing mcp_servers.yml) to get a
    fresh list. main.py passes this to _rebuild_graph() which recompiles the agent.
    The install_mcp_server tool writes to mcp_servers.yml and queues a graph
    rebuild via the Redis plugin reload queue.
"""

import asyncio
import concurrent.futures
import os
from pathlib import Path
from typing import Any

import yaml
from langchain_core.tools import BaseTool, StructuredTool

# Path to config — resolved relative to the repo root (via volume mount in Docker)
_CONFIG_PATH = Path(os.environ.get("MCP_SERVERS_CONFIG", "/config/mcp_servers.yml"))


def _read_config() -> dict:
    if not _CONFIG_PATH.exists():
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
        entry["command"] = cfg["command"]
        entry["args"] = cfg.get("args", [])
        raw_env = cfg.get("env", {})
        entry["env"] = {k: _expand_env(str(v)) for k, v in raw_env.items()}
    elif transport in ("streamable_http", "sse"):
        entry["url"] = cfg["url"]
    else:
        raise ValueError(f"Unknown MCP transport '{transport}' for server '{name}'")

    return entry


async def _discover_server_tools(name: str, client_config: dict) -> list[dict]:
    """Connect to one MCP server and return its tool schemas (name, description, schema)."""
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        print("[MCP] langchain-mcp-adapters not installed — skipping MCP.")
        return []

    try:
        async with MultiServerMCPClient({name: client_config}) as client:
            tools = client.get_tools()
            return [
                {
                    "server": name,
                    "name": t.name,
                    "description": t.description,
                    "args_schema": t.args_schema,
                    "client_config": client_config,
                }
                for t in tools
            ]
    except Exception as e:
        print(f"[MCP] Could not connect to server '{name}': {e}")
        return []


def _make_proxy_tool(schema: dict) -> BaseTool:
    """Create a LangChain tool that opens a fresh MCP connection per call."""
    server_name = schema["server"]
    tool_name = schema["name"]
    tool_desc = schema["description"]
    args_schema = schema["args_schema"]
    client_config = schema["client_config"]

    async def _arun(**kwargs: Any) -> str:
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError:
            return "MCP not available (langchain-mcp-adapters not installed)."

        try:
            async with MultiServerMCPClient({server_name: client_config}) as client:
                for t in client.get_tools():
                    if t.name == tool_name:
                        return await t.ainvoke(kwargs)
            return f"[MCP] Tool '{tool_name}' not found in server '{server_name}'."
        except Exception as e:
            return f"[MCP] Error calling '{tool_name}' on '{server_name}': {e}"

    def _run(**kwargs: Any) -> str:
        # Synchronous path: run the coroutine in a fresh thread with its own loop
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            import asyncio
            future = pool.submit(asyncio.run, _arun(**kwargs))
            return future.result(timeout=60)

    return StructuredTool(
        name=tool_name,
        description=f"[MCP:{server_name}] {tool_desc}",
        args_schema=args_schema,
        func=_run,
        coroutine=_arun,
    )


def load_tools_for_server(name: str, cfg: dict) -> list[BaseTool]:
    """Discover one MCP server's tools and return per-call proxy tools.

    This is the single, correct way to turn an MCP server config into usable
    LangChain tools. The returned tools open a *fresh* connection on every
    invocation (see ``_make_proxy_tool``), so they stay valid indefinitely —
    unlike a tool captured from inside a closed ``MultiServerMCPClient`` context.

    ``cfg`` may be either our YAML shape (``transport``/``command``/``args``/
    ``env`` or ``url``) or an already-built MultiServerMCPClient entry. Discovery
    runs in a dedicated event loop in a worker thread, so this is safe to call at
    startup or from within another running loop.

    Reused by both ``load_mcp_tools`` (mcp_servers.yml) and the plugin registry
    (plugin-declared MCP servers) so there is exactly one MCP tool-loading path.
    """
    try:
        client_cfg = _build_client_config(name, cfg)
    except Exception:
        client_cfg = cfg  # assume the plugin already supplied a client entry

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _discover_server_tools(name, client_cfg))
            schemas = future.result(timeout=30)
    except Exception as e:
        print(f"[MCP] Discovery timeout/error for '{name}': {e}")
        return []

    return [_make_proxy_tool(s) for s in schemas]


def load_mcp_tools() -> list[BaseTool]:
    """Read mcp_servers.yml, discover tools from enabled servers, return proxy tools.

    Called at startup and on reload. Runs synchronously; MCP discovery happens
    in a thread to avoid blocking the main event loop at import time.
    """
    config = _read_config()
    servers = config.get("servers", {})
    enabled = {k: v for k, v in servers.items() if v.get("enabled", False)}

    if not enabled:
        print("[MCP] No servers enabled in mcp_servers.yml.")
        return []

    tools: list[BaseTool] = []
    for name, cfg in enabled.items():
        server_tools = load_tools_for_server(name, cfg)
        print(f"[MCP] '{name}': {len(server_tools)} tool(s) discovered")
        tools.extend(server_tools)

    print(f"[MCP] Total: {len(tools)} tool(s) from {len(enabled)} server(s)")
    return tools


def reload_mcp_tools() -> list[BaseTool]:
    """Alias for load_mcp_tools() — called by the plugin reload watcher."""
    return load_mcp_tools()
