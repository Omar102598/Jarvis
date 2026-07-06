"""Plugin management and safe-restart tools.

install_plugin        — install a Python package and/or write a plugin scaffold
install_mcp_server    — add an MCP server to config/mcp_servers.yml and reload
test_tool             — run a plugin tool in a sandboxed call for quick self-debug
create_dashboard_widget — write a widget file and notify the dashboard to reload
jarvis_restart_safe   — queue a container restart via the mac_bridge watchdog
list_plugins          — show currently loaded plugins
"""

import json
import os
import subprocess
import sys
import traceback
from typing import Optional

import httpx
import yaml
from langchain_core.tools import tool

_MAC_BRIDGE = os.environ.get("MAC_BRIDGE_HOST", "host.docker.internal")
_MAC_BRIDGE_PORT = os.environ.get("MAC_BRIDGE_PORT", "7777")
_BASE = f"http://{_MAC_BRIDGE}:{_MAC_BRIDGE_PORT}"


# ---------------------------------------------------------------------------
# install_plugin
# ---------------------------------------------------------------------------

@tool
def install_plugin(
    name: str,
    pip_package: str = "",
    plugin_code: str = "",
) -> str:
    """Install a new Jarvis plugin that adds tools at runtime (no rebuild needed).

    Args:
        name:         Snake-case plugin name, e.g. "finance" or "calendar_extended".
        pip_package:  Optional pip package to install, e.g. "banksync-client".
        plugin_code:  Optional full Python source for the plugin file.
                      If omitted, a minimal scaffold is generated.
    """
    # 1. Optionally install pip package inside the container
    if pip_package:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", pip_package],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return f"pip install failed:\n{result.stderr[:500]}"

    # 2. Generate scaffold if no code provided
    if not plugin_code:
        plugin_code = _scaffold(name, pip_package)

    # 3. Write plugin file via mac_bridge (so it's written to the host fs / volume mount)
    rel_path = f"services/llm_agent/plugins/{name}.py"
    try:
        resp = httpx.post(
            f"{_BASE}/jarvis/write",
            json={"path": rel_path, "content": plugin_code},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        return f"Failed to write plugin file: {e}"

    # 4. Push reload request to Redis queue (watcher thread in main.py picks it up)
    try:
        import redis as _redis
        r = _redis.Redis(
            host=os.environ.get("REDIS_HOST", "redis"),
            decode_responses=True,
        )
        r.lpush("jarvis:plugin:reload_queue", name)
    except Exception as e:
        return f"Plugin file written but reload queue push failed: {e}. Rebuild llm_agent to activate."

    return (
        f"Plugin '{name}' installed.\n"
        f"File: {rel_path}\n"
        f"Reload queued — new tools will be active within ~3 seconds."
    )


def _scaffold(name: str, pip_package: str) -> str:
    pkg_comment = f"  pip_package: {pip_package}\n" if pip_package else ""
    return f'''"""Jarvis plugin: {name}

{pkg_comment}Add your tools below and they will be hot-loaded without a rebuild.
"""

from langchain_core.tools import tool


@tool
def {name}_example(query: str) -> str:
    """Replace this with real {name} functionality."""
    return f"[{name}] received: {{query}}"


def get_tools():
    return [{name}_example]


# Uncomment to connect an MCP server:
# def get_mcp_server_config():
#     return {{
#         "name": "{name}",
#         "command": "npx",
#         "args": ["-y", "@{name}/mcp-server"],
#         "env": {{}},
#     }}
'''


# ---------------------------------------------------------------------------
# create_dashboard_widget
# ---------------------------------------------------------------------------

@tool
def create_dashboard_widget(
    name: str,
    title: str,
    template_html: str,
    refresh_secs: int = 30,
) -> str:
    """Create a new dashboard widget that appears on the Jarvis web dashboard.

    Args:
        name:          Slug for the widget, e.g. "finance" or "system_stats".
        title:         Human-readable title shown in the dashboard header.
        template_html: HTML content for the widget card (can include inline JS
                       that polls /api/widgets/{name}/data for live data).
        refresh_secs:  How often the widget should refresh its data (default 30s).
    """
    manifest = json.dumps({
        "name": name,
        "title": title,
        "refresh_secs": refresh_secs,
    }, indent=2)

    try:
        resp_manifest = httpx.post(
            f"{_BASE}/jarvis/write",
            json={"path": f"services/dashboard/widgets/{name}/manifest.json", "content": manifest},
            timeout=10,
        )
        resp_manifest.raise_for_status()

        resp_html = httpx.post(
            f"{_BASE}/jarvis/write",
            json={"path": f"services/dashboard/widgets/{name}/widget.html", "content": template_html},
            timeout=10,
        )
        resp_html.raise_for_status()
    except Exception as e:
        return f"Failed to write widget files: {e}"

    # Notify dashboard to re-scan widgets (via MQTT)
    try:
        import paho.mqtt.publish as publish
        publish.single(
            "jarvis/dashboard/reload",
            payload=json.dumps({"widget": name}),
            hostname=os.environ.get("MQTT_HOST", "mosquitto"),
        )
    except Exception:
        pass  # Dashboard will pick up the new widget on next poll

    return f"Widget '{title}' ({name}) created. It will appear on the dashboard within 30 seconds."


# ---------------------------------------------------------------------------
# jarvis_restart_safe
# ---------------------------------------------------------------------------

@tool
def jarvis_restart_safe(service: str = "llm_agent") -> str:
    """Request a safe restart of a Jarvis Docker service.

    The restart is queued via the mac_bridge watchdog and happens ~3 seconds
    after this tool returns, so your response is delivered before the service
    goes down. The service comes back automatically (restart: always).

    Args:
        service: Docker service name to restart (default: llm_agent).
    """
    try:
        resp = httpx.post(
            f"{_BASE}/jarvis/request-restart",
            json={"service": service},
            timeout=5,
        )
        resp.raise_for_status()
        return f"Restart queued for '{service}'. I'll be back in a moment, sir."
    except Exception as e:
        return (
            f"Could not queue restart: {e}. Dispatch Forge to rebuild it: "
            f"spawn_task(\"rebuild the {service} service\", agent=\"developer\")."
        )


# ---------------------------------------------------------------------------
# list_plugins
# ---------------------------------------------------------------------------

@tool
def list_plugins() -> str:
    """List all currently loaded Jarvis plugins and their tools."""
    try:
        from plugin_registry import _registry  # type: ignore
        plugins = _registry.list_plugins()
        if not plugins:
            return "No plugins loaded. Drop .py files into services/llm_agent/plugins/ to add tools."
        lines = ["Loaded plugins:"]
        for p in plugins:
            tools_str = ", ".join(p["tools"]) or "(none)"
            lines.append(f"  • {p['name']} — {p['tool_count']} tool(s): {tools_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not read plugin registry: {e}"


# ---------------------------------------------------------------------------
# install_mcp_server
# ---------------------------------------------------------------------------

_MCP_CONFIG_PATH = "/config/mcp_servers.yml"


@tool
def install_mcp_server(
    name: str,
    npm_package: str = "",
    url: str = "",
    transport: str = "stdio",
    description: str = "",
    extra_args: str = "",
    persistent: bool = False,
    env_keys: str = "",
) -> str:
    """Add an MCP server to Jarvis and activate its tools without a rebuild.

    APPENDS the server to config/mcp_servers.yml via the mac_bridge (preserving
    the file's comments), then queues a graph rebuild so the new tools are live
    within ~10 seconds. This is how any user extends Jarvis by asking.

    Provide EITHER npm_package (for stdio/npx servers) OR url (for HTTP servers).

    Examples:
      install_mcp_server("slack", npm_package="@modelcontextprotocol/server-slack",
                         env_keys="SLACK_BOT_TOKEN,SLACK_TEAM_ID")
      install_mcp_server("myapi", url="http://localhost:9000/mcp", transport="streamable_http")

    Args:
        name:        Unique slug for the server, e.g. "slack" or "notion".
        npm_package: npm package to run via npx, e.g. "@modelcontextprotocol/server-slack".
                     Leave empty if using a URL-based server.
        url:         URL for HTTP/SSE transport. Leave empty if using npm_package.
        transport:   "stdio", "streamable_http", or "sse". Auto-detected if omitted.
        description: Human-readable description for the config file.
        extra_args:  Space-separated extra CLI args for stdio servers.
        persistent:  True for STATEFUL servers that must keep one session open
                     across tool calls (e.g. browser automation). Default False.
        env_keys:    Comma-separated env var names the server needs (e.g.
                     "SLACK_BOT_TOKEN"). Written as ${VAR} references — the
                     user must add the actual values to .env themselves.
    """
    import re as _re
    if not npm_package and not url:
        return "Provide either npm_package or url."
    if not _re.fullmatch(r"[a-z0-9_]+", name):
        return "Name must be a lowercase slug (a-z, 0-9, underscores)."

    if not transport:
        transport = "stdio" if npm_package else "streamable_http"

    # Read existing config via mac_bridge (to check for duplicates only —
    # we APPEND text rather than re-dumping YAML, which would strip every
    # comment in the registry).
    try:
        resp = httpx.get(f"{_BASE}/jarvis/read", params={"path": "config/mcp_servers.yml"}, timeout=10)
        existing = resp.json().get("content", "") if resp.status_code == 200 else ""
    except Exception as e:
        return f"Could not read mcp_servers.yml: {e}"
    try:
        servers = (yaml.safe_load(existing) or {}).get("servers", {}) or {}
    except Exception:
        return "mcp_servers.yml is not valid YAML — fix it before installing servers."
    if name in servers:
        return (f"A server named '{name}' already exists "
                f"(enabled={servers[name].get('enabled')}). Pick another name or edit it.")

    # Build the YAML block as text (2-space indent under `servers:`)
    lines = [
        "",
        "  # ---------------------------------------------------------------------------",
        f"  # {name} — added by Jarvis (install_mcp_server)",
        f"  # {description or 'MCP server: ' + name}",
        "  # ---------------------------------------------------------------------------",
        f"  {name}:",
        "    enabled: true",
    ]
    if persistent:
        lines.append("    persistent: true   # stateful — one session held open across calls")
    lines.append(f"    transport: {transport}")
    if npm_package:
        lines += ["    command: npx", "    args:", '      - "-y"', f'      - "{npm_package}"']
        for a in (extra_args.split() if extra_args else []):
            lines.append(f'      - "{a}"')
    else:
        lines.append(f'    url: "{url}"')
    keys = [k.strip() for k in env_keys.split(",") if k.strip()]
    if keys:
        lines.append("    env:")
        for k in keys:
            lines.append(f'      {k}: "${{{k}}}"')
    if description:
        lines.append(f'    description: "{description}"')

    new_content = existing.rstrip("\n") + "\n" + "\n".join(lines) + "\n"

    # Validate the assembled file parses before writing
    try:
        parsed = yaml.safe_load(new_content)
        assert name in (parsed.get("servers") or {})
    except Exception as e:
        return f"Refusing to write — generated YAML failed validation: {e}"

    try:
        resp = httpx.post(
            f"{_BASE}/jarvis/write",
            json={"path": "config/mcp_servers.yml", "content": new_content},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        return f"Failed to write mcp_servers.yml: {e}"

    # Queue graph reload
    try:
        import redis as _redis
        r = _redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)
        r.lpush("jarvis:plugin:reload_queue", "__all__")
    except Exception as e:
        return f"Config written but reload failed: {e}. Rebuild llm_agent to activate."

    transport_note = f"npx {npm_package}" if npm_package else url
    key_note = (f" NOTE: add {', '.join(keys)} to .env or its tools will fail."
                if keys else "")
    return (
        f"MCP server '{name}' added ({transport_note}). "
        f"Graph rebuild queued — new tools live in ~10 seconds.{key_note}"
    )


@tool
def list_mcp_servers() -> str:
    """List every MCP server in Jarvis's registry with its status.

    Use when the user asks "what MCP servers do I have?", "what integrations
    are installed?", or before installing one that may already exist.
    """
    try:
        resp = httpx.get(f"{_BASE}/jarvis/read", params={"path": "config/mcp_servers.yml"}, timeout=10)
        servers = (yaml.safe_load(resp.json().get("content", "")) or {}).get("servers", {}) or {}
    except Exception as e:
        return f"Could not read the MCP registry: {e}"
    if not servers:
        return "No MCP servers in the registry."
    lines = [f"{len(servers)} MCP server(s) in the registry:"]
    for sname, cfg in servers.items():
        state = "ENABLED" if cfg.get("enabled") else "disabled"
        mode = " (persistent)" if cfg.get("persistent") else ""
        what = cfg.get("url") or " ".join(cfg.get("args", [])[-1:]) or cfg.get("command", "")
        lines.append(f"  • {sname}: {state}{mode} — {what}"
                     + (f" — {cfg['description']}" if cfg.get("description") else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# test_tool
# ---------------------------------------------------------------------------

@tool
def test_tool(plugin_name: str, tool_name: str, args_json: str = "{}") -> str:
    """Run a plugin tool in a sandboxed call and return its output or error.

    Use this after writing a new plugin to verify it works before telling the
    user it's ready. Surfaces Python exceptions and tracebacks so you can fix
    issues in the same turn.

    Args:
        plugin_name: Name of the plugin file (without .py), e.g. "finance".
        tool_name:   Name of the tool function defined in that plugin.
        args_json:   JSON object of keyword arguments for the tool, e.g. '{"query": "AAPL"}'.
                     Use '{}' for tools that take no arguments.
    """
    import importlib.util
    from pathlib import Path

    plugins_dir = Path("/app/plugins")
    plugin_path = plugins_dir / f"{plugin_name}.py"

    if not plugin_path.exists():
        return f"Plugin file not found: {plugin_path}"

    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as e:
        return f"Invalid args_json: {e}"

    try:
        spec = importlib.util.spec_from_file_location(f"test_plugin_{plugin_name}", plugin_path)
        if spec is None or spec.loader is None:
            return f"Could not load plugin spec for '{plugin_name}'."
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as e:
        return f"Plugin import failed:\n{traceback.format_exc()}"

    tools = mod.get_tools() if hasattr(mod, "get_tools") else []
    target = next((t for t in tools if t.name == tool_name), None)

    if target is None:
        available = [t.name for t in tools]
        return f"Tool '{tool_name}' not found in '{plugin_name}'. Available: {available}"

    try:
        import asyncio
        if asyncio.iscoroutinefunction(getattr(target, "_arun", None)):
            result = asyncio.run(target.arun(args))
        else:
            result = target.invoke(args)
        return f"Tool '{tool_name}' result:\n{result}"
    except Exception:
        return f"Tool '{tool_name}' raised an exception:\n{traceback.format_exc()}"
