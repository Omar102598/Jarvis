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

from tools.agents import get_agent_report
from tools.smart_home import control_device, get_device_states, get_presence, set_scene
from tools.web_search import web_search
from tools.vision import get_camera_snapshot
from tools.calendar import get_calendar_events, set_reminder
from tools.weather import get_weather
from tools.notifications import send_notification

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
    control_device,
    get_device_states,
    set_scene,
    get_presence,
    web_search,
    get_camera_snapshot,
    get_agent_report,
    get_calendar_events,
    set_reminder,
    get_weather,
    send_notification,
]

def _build_llm():
    """Pick LLM provider from LLM_MODEL env var.

    Claude models (Anthropic): set LLM_MODEL=claude-opus-4-5, claude-sonnet-4-5, etc.
    OpenAI models:              set LLM_MODEL=gpt-4.1, gpt-4o-mini, etc.
    Default (no key set):       falls back to claude-haiku-4-5 if ANTHROPIC_API_KEY
                                 is present, else gpt-4.1-mini.
    """
    model_name = os.environ.get("LLM_MODEL", "").strip()

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")

    # Default: prefer Claude if only Anthropic key is set
    if not model_name:
        if anthropic_key and not openai_key:
            model_name = "claude-haiku-4-5"
        else:
            model_name = "gpt-4.1-mini"

    if "claude" in model_name.lower() or model_name.startswith("anthropic"):
        from langchain_anthropic import ChatAnthropic
        print(f"[LLM] Using Anthropic: {model_name}")
        return ChatAnthropic(model=model_name, temperature=0.3, anthropic_api_key=anthropic_key)
    else:
        from langchain_openai import ChatOpenAI
        print(f"[LLM] Using OpenAI: {model_name}")
        return ChatOpenAI(model=model_name, temperature=0.3)


# LLM with tool-calling
llm = _build_llm().bind_tools(tools)

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
