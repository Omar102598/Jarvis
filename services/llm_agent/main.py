"""JARVIS LLM Agent Service.

The brain of JARVIS. Receives transcribed speech, processes it through an LLM
with tool-calling (smart home, web search, vision, etc.), and publishes the
response for TTS synthesis.
"""

import json
import os
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import redis
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from llm_factory import build_llm
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
from tools.github_tool import github_tool
from tools.notes import manage_notes

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")

# Load system prompt from file, fall back to inline default
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
"""

# Tools available to the LLM
tools = [
    # Smart home
    control_device,
    get_device_states,
    set_scene,
    get_presence,
    # Information
    web_search,
    fetch_url,
    get_camera_snapshot,
    get_weather,
    get_calendar_events,
    # Memory
    remember,
    recall,
    forget,
    # Files & system
    read_file,
    write_file,
    list_files,
    run_shell,
    run_python,
    # Communication
    send_notification,
    send_email,
    send_sms,
    set_reminder,
    # Background agents & dispatch
    get_agent_report,
    trigger_agent,
    spawn_task,
    ask_subagent,
    # Integrations
    spotify_control,
    github_tool,
    manage_notes,
]

# LLM with tool-calling (provider selected by llm_factory from LLM_MODEL)
llm = build_llm(temperature=0.3).bind_tools(tools)

# Redis for conversation history
r = redis.Redis(host=REDIS_HOST, decode_responses=True)


# --- LangGraph Agent ---

def should_continue(state):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


def call_model(state):
    response = llm.invoke(state["messages"])
    return {"messages": state["messages"] + [response]}


tool_node = ToolNode(tools)

workflow = StateGraph(dict)
workflow.add_node("agent", call_model)
workflow.add_node("tools", tool_node)
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
workflow.add_edge("tools", "agent")
agent = workflow.compile()


# --- Request Processing ---

def process_request(text: str, room: str) -> str:
    """Process a user request through the LLM agent with tool-calling."""
    # Make the active room available to dispatch tools so spawned background
    # tasks can announce their results back to the right room.
    ACTIVE_ROOM["room"] = room

    system = SystemMessage(content=SYSTEM_PROMPT.format(
        time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        room=room,
    ))

    # Retrieve conversation history (last 20 messages)
    history_key = f"conversation:{room}"
    history_raw = r.lrange(history_key, -20, -1)
    history = []
    for h in history_raw:
        msg = json.loads(h)
        if msg["role"] == "user":
            history.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            history.append(AIMessage(content=msg["content"]))

    messages = [system] + history + [HumanMessage(content=text)]

    result = agent.invoke({"messages": messages})
    response_text = result["messages"][-1].content

    # Store in conversation history
    r.rpush(history_key, json.dumps({"role": "user", "content": text}))
    r.rpush(history_key, json.dumps({"role": "assistant", "content": response_text}))
    r.ltrim(history_key, -40, -1)

    return response_text


# --- MQTT Handler ---

def on_llm_request(client, userdata, msg):
    data = json.loads(msg.payload)
    text = data["text"]
    room = data["room"]

    print(f"[LLM] Processing: '{text}' (from {room})")

    try:
        response = process_request(text, room)
    except Exception as e:
        print(f"[LLM] Error: {e}")
        response = "I'm sorry, I encountered an error processing that request."

    print(f"[LLM] Response: '{response[:100]}...'")

    client.publish(
        f"jarvis/tts/{room}/speak",
        json.dumps({"text": response, "room": room}),
    )


def main():
    mqtt_client = mqtt.Client()
    mqtt_client.connect(MQTT_HOST, MQTT_PORT)
    mqtt_client.subscribe("jarvis/llm/request")
    mqtt_client.message_callback_add("jarvis/llm/request", on_llm_request)

    print("[LLM] Agent ready, waiting for requests...")
    mqtt_client.loop_forever()


if __name__ == "__main__":
    main()
