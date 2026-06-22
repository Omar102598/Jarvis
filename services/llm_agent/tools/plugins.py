"""Plugin management and safe-restart tools.

install_plugin        — install a Python package and/or write a plugin scaffold
create_dashboard_widget — write a widget file and notify the dashboard to reload
jarvis_restart_safe   — queue a container restart via the mac_bridge watchdog
list_plugins          — show currently loaded plugins
"""

import json
import os
import subprocess
import sys
from typing import Optional

import httpx
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
        return f"Could not queue restart: {e}. Use jarvis_rebuild('{service}') instead."


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
