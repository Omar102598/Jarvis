"""Dynamic plugin loader for JARVIS llm_agent.

A plugin is a .py file in services/llm_agent/plugins/ that exposes:
  get_tools() -> list[BaseTool]              — required
  get_mcp_server_config() -> dict | None     — optional (launches an MCP subprocess)

The registry is thread-safe. It can hot-reload individual plugins via
reload(plugin_name) without restarting the container, because plugins are
loaded from a volume-mounted directory.
"""

import importlib.util
import os
import threading
from pathlib import Path
from typing import Callable

from langchain_core.tools import BaseTool

PLUGINS_DIR = Path(os.environ.get("PLUGINS_DIR", str(Path(__file__).parent / "plugins")))


class PluginRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._plugins: dict[str, list[BaseTool]] = {}
        self._mcp_configs: dict[str, dict] = {}
        self._change_callbacks: list[Callable] = []
        self._mcp_clients: dict[str, object] = {}  # keep MCP client refs alive

    def register_on_change(self, cb: Callable) -> None:
        """Register a callback invoked after any reload so callers can rebuild the graph."""
        self._change_callbacks.append(cb)

    def load_all(self) -> None:
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        for path in sorted(PLUGINS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            self._load_plugin(path)

    def reload(self, plugin_name: str | None = None) -> None:
        """Reload one plugin by name, or all plugins if name is None."""
        if plugin_name:
            path = PLUGINS_DIR / f"{plugin_name}.py"
            if path.exists():
                self._load_plugin(path)
        else:
            self.load_all()
        for cb in self._change_callbacks:
            try:
                cb()
            except Exception as e:
                print(f"[PluginRegistry] Change callback error: {e}")

    def _load_plugin(self, path: Path) -> bool:
        name = path.stem
        try:
            spec = importlib.util.spec_from_file_location(f"jarvis_plugin_{name}", path)
            if spec is None or spec.loader is None:
                return False
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]

            tools: list[BaseTool] = mod.get_tools() if hasattr(mod, "get_tools") else []
            mcp_cfg: dict | None = mod.get_mcp_server_config() if hasattr(mod, "get_mcp_server_config") else None

            # Optionally wrap MCP server tools if config provided
            if mcp_cfg:
                mcp_tools = self._load_mcp_tools(name, mcp_cfg)
                tools = tools + mcp_tools
                with self._lock:
                    self._mcp_configs[name] = mcp_cfg

            with self._lock:
                self._plugins[name] = tools

            print(f"[PluginRegistry] Loaded '{name}': {len(tools)} tool(s)")
            return True
        except Exception as e:
            print(f"[PluginRegistry] Failed to load plugin '{name}': {e}")
            return False

    def _load_mcp_tools(self, plugin_name: str, config: dict) -> list[BaseTool]:
        """Load tools from a plugin-declared MCP server as per-call proxy tools.

        Delegates to ``mcp_loader.load_tools_for_server`` — the same machinery
        used for config/mcp_servers.yml. This returns tools that open a fresh
        connection on each call, so they remain valid after loading, fixing the
        earlier bug where tools captured from a closed MultiServerMCPClient
        context became unusable once the ``async with`` block exited.
        """
        try:
            from mcp_loader import load_tools_for_server
        except ImportError as e:
            print(f"[PluginRegistry] MCP loader unavailable — skipping MCP for '{plugin_name}': {e}")
            return []

        server_name = config.get("name", plugin_name)
        tools = load_tools_for_server(server_name, config)
        print(f"[PluginRegistry] MCP '{server_name}': {len(tools)} proxy tool(s) for plugin '{plugin_name}'")
        return tools

    def get_all_tools(self) -> list[BaseTool]:
        with self._lock:
            tools: list[BaseTool] = []
            for plugin_tools in self._plugins.values():
                tools.extend(plugin_tools)
            return tools

    def list_plugins(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "name": n,
                    "tool_count": len(t),
                    "tools": [x.name for x in t],
                }
                for n, t in self._plugins.items()
            ]
