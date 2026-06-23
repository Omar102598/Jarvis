"""JARVIS LLM Agent Service.

The brain of JARVIS. Receives transcribed speech, processes it through an LLM
with tool-calling (smart home, web search, vision, etc.), and publishes the
response for TTS synthesis.

Supports hot-reload of plugins: drop a .py file into services/llm_agent/plugins/
and push its name to the Redis key jarvis:plugin:reload_queue. New tools become
active within ~3 seconds — no container rebuild needed.
"""

import asyncio
import functools
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Annotated

import paho.mqtt.client as mqtt
import redis
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from llm_factory import build_llm
from plugin_registry import PluginRegistry
from tools.agents import get_agent_report, trigger_agent
from tools.smart_home import control_device, get_device_states, get_presence, set_scene
from tools.web_search import web_search
from tools.web import fetch_url
from tools.vision import get_camera_snapshot
from tools.calendar import get_calendar_events, set_reminder
from tools.weather import get_weather
from tools.notifications import send_notification
from tools.memory import forget, recall, remember
from tools.files import list_files, read_file, write_file
from tools.shell import run_shell
from tools.comms import send_email, send_sms
from tools.dispatch import ACTIVE_ROOM, ask_subagent, spawn_task
from tools.code_exec import run_python
from tools.spotify import spotify_control
from tools.spotify_desktop import spotify_desktop
from tools.self_modify import (
    jarvis_find_file, jarvis_grep_code,
    jarvis_list_files, jarvis_read_code, jarvis_rebuild, jarvis_write_code,
)
from tools.developer import dev_list_dir, dev_read_file, dev_search_code, dev_shell, dev_write_file
from tools.github_tool import github_tool
from tools.notes import manage_notes
from tools.mac import (
    mac_applescript, mac_browser_click, mac_browser_fill,
    mac_browser_navigate, mac_browser_read, mac_browser_screenshot,
    mac_chrome_navigate, mac_chrome_read, mac_chrome_js,
    mac_clipboard, mac_notify, mac_open,
    mac_shell, mac_screenshot, mac_spotlight, mac_system_info, mac_type,
)
from tools.plugins import (
    install_plugin, create_dashboard_widget, jarvis_restart_safe, list_plugins,
)
from tools.profile import get_user_profile, update_user_profile

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")

# Load system prompt from file
_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "system.txt")
try:
    with open(_PROMPT_PATH) as _f:
        _BASE_PROMPT = _f.read().strip()
except FileNotFoundError:
    _BASE_PROMPT = (
        "You are JARVIS, an advanced AI assistant inspired by the AI from Iron Man. "
        "You are British, witty, efficient, and loyal."
    )

SYSTEM_PROMPT = _BASE_PROMPT + """

Current time: {time}
User location: {room}
Active plugins: {active_plugins}
Response style: {verbosity}
"""

# ---------------------------------------------------------------------------
# Static "core" tools — always available
# ---------------------------------------------------------------------------

_core_tools = [
    # Smart home
    control_device, get_device_states, set_scene, get_presence,
    # Information
    web_search, fetch_url, get_camera_snapshot, get_weather, get_calendar_events,
    # Memory
    remember, recall, forget,
    # Files & system
    read_file, write_file, list_files, run_shell, run_python,
    # Communication
    send_notification, send_email, send_sms, set_reminder,
    # Background agents & dispatch
    get_agent_report, trigger_agent, spawn_task, ask_subagent,
    # Integrations
    spotify_control, spotify_desktop, github_tool, manage_notes,
    # macOS laptop control
    mac_screenshot, mac_clipboard, mac_system_info, mac_shell, mac_applescript,
    mac_open, mac_spotlight, mac_type, mac_notify,
    # Chrome control (user's signed-in browser)
    mac_chrome_navigate, mac_chrome_read, mac_chrome_js,
    # Browser automation (Playwright via mac_bridge)
    mac_browser_navigate, mac_browser_read, mac_browser_click,
    mac_browser_fill, mac_browser_screenshot,
    # Self-modification (Jarvis repo only)
    jarvis_find_file, jarvis_grep_code, jarvis_list_files,
    jarvis_read_code, jarvis_write_code, jarvis_rebuild,
    # Developer agent (any project on the Mac)
    dev_list_dir, dev_read_file, dev_write_file, dev_shell, dev_search_code,
    # Plugin management & personalization
    install_plugin, create_dashboard_widget, jarvis_restart_safe, list_plugins,
    get_user_profile, update_user_profile,
]

# ---------------------------------------------------------------------------
# Dynamic plugin registry
# ---------------------------------------------------------------------------

_registry = PluginRegistry()
_graph_lock = threading.RLock()

# Globals that _rebuild_graph() replaces atomically
llm = None
tool_node = None
agent = None


def _call_model(llm_ref, state):
    """Agent node: always closes over the llm instance it was built with."""
    response = llm_ref.invoke(state["messages"])
    return {"messages": [response]}


def _rebuild_graph() -> None:
    """Recompile the LangGraph agent with all core + plugin tools."""
    global llm, tool_node, agent

    all_tools = _core_tools + _registry.get_all_tools()
    new_llm = build_llm(temperature=0.3).bind_tools(all_tools)
    new_tool_node = ToolNode(all_tools)
    new_call_model = functools.partial(_call_model, new_llm)

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", new_call_model)
    workflow.add_node("tools", new_tool_node)
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    workflow.add_edge("tools", "agent")
    new_agent = workflow.compile()

    with _graph_lock:
        llm = new_llm
        tool_node = new_tool_node
        agent = new_agent

    print(f"[LLM] Graph rebuilt — {len(all_tools)} tools active "
          f"({len(_core_tools)} core + {len(_registry.get_all_tools())} plugins)")


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

# Redis for conversation history
r = redis.Redis(host=REDIS_HOST, decode_responses=True)


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


def should_continue(state: AgentState):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


# Initial graph build
_registry.register_on_change(_rebuild_graph)
_registry.load_all()
_rebuild_graph()


# ---------------------------------------------------------------------------
# Plugin hot-reload watcher thread
# ---------------------------------------------------------------------------

def _plugin_reload_watcher() -> None:
    """Blocks on a Redis list; reloads the named plugin and rebuilds the graph."""
    # socket_timeout must exceed the blpop timeout or redis-py raises TimeoutError
    watcher_r = redis.Redis(host=REDIS_HOST, decode_responses=True, socket_timeout=10)
    while True:
        try:
            item = watcher_r.blpop("jarvis:plugin:reload_queue", timeout=5)
            if item:
                plugin_name = item[1]
                print(f"[LLM] Hot-reload requested for plugin '{plugin_name}'")
                _registry.reload(plugin_name if plugin_name != "__all__" else None)
        except Exception as e:
            print(f"[LLM] Plugin watcher error: {e}")
            time.sleep(2)


threading.Thread(target=_plugin_reload_watcher, daemon=True).start()


# ---------------------------------------------------------------------------
# Request processing
# ---------------------------------------------------------------------------

def _build_system_prompt(room: str) -> str:
    """Build system prompt with injected profile preferences."""
    profile_raw = r.get("user:profile") or "{}"
    try:
        profile = json.loads(profile_raw)
    except Exception:
        profile = {}
    prefs = profile.get("preferences", {})
    active_plugins = ", ".join(profile.get("enabled_plugins", [])) or "none"
    verbosity = prefs.get("response_verbosity", "balanced")
    return SYSTEM_PROMPT.format(
        time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        room=room,
        active_plugins=active_plugins,
        verbosity=verbosity,
    )


async def _process_async(text: str, room: str) -> str:
    """Async core — allows async tools (Spotify, sub-agents, browser) to work."""
    ACTIVE_ROOM["room"] = room

    # Capture current agent reference so rebuild mid-request doesn't affect us
    current_agent = agent

    system = SystemMessage(content=_build_system_prompt(room))

    history_key = f"conversation:{room}"
    history_raw = r.lrange(history_key, -20, -1)
    history = []
    for h in history_raw:
        msg = json.loads(h)
        content = msg["content"]
        if isinstance(content, list):
            has_tool_use = any(
                isinstance(item, dict) and item.get("type") in ("tool_use", "tool_result")
                for item in content
            )
            if has_tool_use:
                continue
            # Strip any image blocks (avoid re-sending large base64 payloads)
            content = [
                item for item in content
                if not (isinstance(item, dict) and item.get("type") == "image")
            ]
            if not content:
                continue
        elif isinstance(content, str) and "[GLASSES_CAMERA_IMAGE:" in content:
            # Strip stored camera image data URIs — keep only the text prompt
            import re as _re
            content = _re.sub(r"\[GLASSES_CAMERA_IMAGE:[^\]]*\]\n?", "", content).strip()
            if not content:
                continue
        if msg["role"] == "user":
            history.append(HumanMessage(content=content))
        elif msg["role"] == "assistant":
            history.append(AIMessage(content=content))

    messages = [system] + history + [HumanMessage(content=text)]

    result = await current_agent.ainvoke({"messages": messages})
    raw = result["messages"][-1].content

    if isinstance(raw, list):
        response_text = " ".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in raw
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip() or "(no text response)"
    else:
        response_text = str(raw)

    # Sanitize before storing: replace camera image data URIs with a short placeholder
    import re as _re
    stored_text = _re.sub(r"\[GLASSES_CAMERA_IMAGE:[^\]]*\]", "[camera image]", text)
    r.rpush(history_key, json.dumps({"role": "user", "content": stored_text}))
    r.rpush(history_key, json.dumps({"role": "assistant", "content": response_text}))
    r.ltrim(history_key, -40, -1)

    return response_text


def process_request(text: str, room: str) -> str:
    return asyncio.run(_process_async(text, room))


# ---------------------------------------------------------------------------
# MQTT handler
# ---------------------------------------------------------------------------

def _fanout_to_active_surfaces(client, response: str) -> None:
    """Push response to any active non-voice surfaces (iPhone, Mac)."""
    try:
        active = r.smembers("jarvis:active_surfaces")
        for surface_id in active:
            meta_raw = r.get(f"jarvis:surface:{surface_id}:meta")
            if not meta_raw:
                continue
            meta = json.loads(meta_raw)
            surface_type = meta.get("type", "")
            if surface_type == "iphone":
                client.publish(
                    "jarvis/surfaces/iphone/push",
                    json.dumps({"text": response}),
                )
            elif surface_type == "mac":
                client.publish(
                    "jarvis/surfaces/mac/notify",
                    json.dumps({"title": "Jarvis", "body": response[:200]}),
                )
    except Exception as e:
        print(f"[LLM] Surface fanout error: {e}")


def _handle_request(client, data):
    """Process one LLM request in a background thread so the MQTT loop stays live."""
    text = data["text"]
    room = data["room"]

    print(f"[LLM] Processing: '{text}' (from {room})")

    try:
        r.set(f"jarvis:voice:state:{room}", "thinking", ex=60)
    except Exception:
        pass

    try:
        response = process_request(text, room)
    except Exception as e:
        err_str = str(e)
        print(f"[LLM] Error: {err_str}")
        if "tool_use_id" in err_str or "tool_result" in err_str or "400" in err_str:
            try:
                r.delete(f"conversation:{room}")
                print(f"[LLM] Cleared corrupted history for {room}")
            except Exception:
                pass
        response = "I'm sorry, I encountered an error processing that request."

    print(f"[LLM] Response: '{response[:100]}...'")

    word_count = len(response.split())
    speak_secs = max(4, min(45, int(word_count / 140 * 60)))
    try:
        r.set(f"jarvis:voice:state:{room}", "speaking", ex=speak_secs)
    except Exception:
        pass

    # 1. TTS for the originating room (voice output)
    client.publish(
        f"jarvis/tts/{room}/speak",
        json.dumps({"text": response, "room": room}),
    )

    # 2. Fan out to active surfaces (iPhone push, Mac notification)
    _fanout_to_active_surfaces(client, response)


def on_llm_request(client, userdata, msg):
    data = json.loads(msg.payload)
    threading.Thread(target=_handle_request, args=(client, data), daemon=True).start()


def main():
    mqtt_client = mqtt.Client()
    mqtt_client.connect(MQTT_HOST, MQTT_PORT)
    mqtt_client.subscribe("jarvis/llm/request")
    mqtt_client.message_callback_add("jarvis/llm/request", on_llm_request)

    print("[LLM] Agent ready, waiting for requests…")
    mqtt_client.loop_forever()


if __name__ == "__main__":
    main()
