"""JARVIS LLM Agent Service.

The brain of JARVIS. Receives transcribed speech, processes it through an LLM
with tool-calling (smart home, web search, vision, etc.), and publishes the
response for TTS synthesis.

Supports hot-reload of plugins: drop a .py file into services/llm_agent/plugins/
and push its name to the Redis key jarvis:plugin:reload_queue. New tools become
active within ~3 seconds — no container rebuild needed.

Voice responses stream sentence-by-sentence to TTS so the first word plays
within seconds of the LLM starting to generate, rather than after the full
response is ready.

Model routing:
  Haiku  — simple commands, device control, quick lookups (cheap + fast)
  Sonnet — default conversational + moderate tool use
  Opus   — code, complex reasoning, long research queries (powerful)
"""

import asyncio
import functools
import json
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

import paho.mqtt.client as mqtt
import redis
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from llm_factory import TIER_MODELS, build_llm
from plugin_registry import PluginRegistry
from tools.agents import get_agent_report, trigger_agent
from tools.smart_home import control_device, get_device_states, get_presence, set_scene
from tools.scenes import manage_scenes
from tools.email_drafts import get_email_drafts
from tools.focus import focus_mode
from tools.web_search import web_search
from tools.web import fetch_url
from tools.vision import get_camera_snapshot
from tools.calendar import get_calendar_events, set_reminder
from tools.weather import get_weather
from tools.notifications import send_notification
from tools.memory import forget, recall, remember, consolidate_memory
from tools.chronicle import recall_journal
from tools.finance_insights import get_spending_insights
from tools.tasks import manage_tasks
from tools.health import get_readiness
from tools.nutrition import log_meal, get_nutrition_today
from tools.visual_memory import log_sighting, recall_visual
from tools.capture import quick_capture
from tools.packages import get_packages
from tools.actions import get_recent_actions, undo_last_action
from tools.firecrawl import scrape_page
from tools.people import manage_people
from tools.routines import detect_routines
from tools.travel import plan_trip, get_trips
from tools.calls import make_call
from tools.watches import manage_watches
from tools.presence import arrive_home, leave_home
from tools.apple_tv import apple_tv
from tools.files import list_files, read_file, write_file
from tools.shell import run_shell
from tools.comms import send_email, send_sms
from tools.dispatch import ACTIVE_ROOM, ask_subagent, spawn_task
from tools.code_exec import run_python
from tools.spotify import spotify_control
from tools.spotify_desktop import spotify_desktop
# Read-only code inspection. Writes/builds/rebuilds are NOT core tools —
# all code modification is dispatched to the developer agent (Forge) via
# spawn_task(task, agent="developer").
from tools.self_modify import (
    jarvis_find_file, jarvis_grep_code,
    jarvis_list_files, jarvis_read_code,
)
from tools.developer import dev_list_dir, dev_read_file, dev_search_code
from tools.github_tool import github_tool
from tools.notes import manage_notes
# mac_browser_* wrappers are no longer bound — fresh-session browsing now goes
# through the Playwright MCP server (config/mcp_servers.yml). mac_chrome_* and
# mac_fresh_add stay: they drive the user's signed-in browser via mac_bridge.
from tools.mac import (
    mac_applescript,
    mac_fresh_add,
    mac_chrome_navigate, mac_chrome_read, mac_chrome_js,
    mac_clipboard, mac_notify, mac_open,
    mac_shell, mac_screenshot, mac_spotlight, mac_system_info, mac_type,
    mac_list_shortcuts, mac_run_shortcut, read_imessages,
    apple_reminder_add, apple_reminders_list,
)
from tools.plugins import (
    install_plugin, install_mcp_server, create_dashboard_widget,
    jarvis_restart_safe, list_mcp_servers, list_plugins, test_tool,
)
from tools.setup import get_setup_status, personalize_jarvis
from tools.ring import (
    check_ring_camera, list_ring_cameras, ring_live_view, ring_privacy,
    who_came_by,
)
# PetKit pet feeders + water fountains (via the PetKit HACS integration in
# Home Assistant — uses the existing HA_URL / HA_TOKEN, no extra creds in Jarvis).
from tools.petkit import (
    feed_pet, get_feeder_status, get_feeding_schedule, get_fountain_status,
    list_petkit_devices, toggle_feeding_plan,
)
from tools.profile import get_user_profile, update_user_profile
from tools.workout import get_todays_workout, get_workout_plan, plan_workout_week
from tools.grocery import (
    approve_grocery_order, get_grocery_status, get_usual_order,
    learn_fresh_cart, pin_favorite_product, suggest_meals, trigger_grocery_run,
)
from tools.classpass import (
    book_class,
    get_class_suggestions,
    join_classpass_waitlist,
    manage_classpass_favorites,
    search_classpass,
    trigger_classpass_scan,
)
from mcp_loader import load_mcp_tools

MQTT_HOST  = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT  = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
USER_ID    = os.environ.get("JARVIS_USER_ID", "default")

# Load system prompt from file — static portion is eligible for prompt caching
_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "system.txt")
try:
    with open(_PROMPT_PATH) as _f:
        _BASE_PROMPT = _f.read().strip()
except FileNotFoundError:
    _BASE_PROMPT = (
        "You are JARVIS, an advanced AI assistant inspired by the AI from Iron Man. "
        "You are British, witty, efficient, and loyal."
    )

# ---------------------------------------------------------------------------
# Model routing — classify request complexity into one of three tiers
# ---------------------------------------------------------------------------

# Keywords that force escalation to Opus
_OPUS_TRIGGERS = frozenset([
    "write", "code", "script", "function", "debug", "implement", "refactor",
    "explain in detail", "explain why", "explain how", "how does", "why does",
    "compare", "difference between", "analyze", "analyse", "research",
    "plan", "design", "architect", "strategy", "outline",
    "summarize", "summarise", "essay", "report",
])

# Keywords that allow Haiku (only for short, command-style queries)
_HAIKU_TRIGGERS = frozenset([
    "turn on", "turn off", "switch on", "switch off", "toggle",
    "play", "pause", "stop music", "skip", "next track", "previous track",
    "what time", "what's the time", "what is the time",
    "set a timer", "set timer", "remind me",
    "weather", "temperature outside",
    "lights", "thermostat", "lock", "unlock",
    "volume up", "volume down", "mute",
    "open ", "close ",
    "feed the", "feed my",
])


def _classify_tier(text: str) -> str:
    """Return 'haiku', 'sonnet', or 'opus' based on query complexity."""
    lower  = text.lower().strip()
    words  = lower.split()
    n      = len(words)

    # Long queries or explicit reasoning/code keywords → Opus
    if n > 30 or any(t in lower for t in _OPUS_TRIGGERS):
        return "opus"

    # Short command-style queries → Haiku
    if n <= 14 and any(lower.startswith(t) or t in lower for t in _HAIKU_TRIGGERS):
        return "haiku"

    return "sonnet"


# ---------------------------------------------------------------------------
# Thinking words — shown in dashboard while processing
# ---------------------------------------------------------------------------

_THINKING_WORDS = [
    "Analyzing", "Triangulating", "Computing", "Correlating", "Deducing",
    "Synthesizing", "Cross-referencing", "Extrapolating", "Calibrating",
    "Interfacing", "Scanning", "Sequencing", "Parsing", "Evaluating",
    "Interpolating", "Resolving", "Mapping", "Inferring", "Modelling",
    "Sifting", "Probing", "Querying", "Auditing", "Reconstructing",
    "Assessing", "Determining", "Verifying", "Indexing", "Tracing",
    "Classifying", "Profiling", "Decoding", "Translating", "Compiling",
    "Vectorizing", "Tokenizing", "Hashing", "Routing", "Optimizing",
]


def _start_thinking_rotation(stop_event: threading.Event) -> None:
    """Rotate through thinking words every ~2.5s while processing."""
    words = random.sample(_THINKING_WORDS, len(_THINKING_WORDS))
    idx = 0
    while not stop_event.is_set():
        r.set("jarvis:thinking:word", words[idx % len(words)], ex=10)
        idx += 1
        stop_event.wait(timeout=2.5)
    r.delete("jarvis:thinking:word")


# ---------------------------------------------------------------------------
# Core tools — always available
# ---------------------------------------------------------------------------

_core_tools = [
    # Smart home + manual presence (geofence fallback) + Apple TV
    control_device, get_device_states, set_scene, get_presence, manage_scenes,
    arrive_home, leave_home, apple_tv,
    # PetKit pet feeders + water fountains (via Home Assistant)
    feed_pet, get_feeder_status, get_feeding_schedule, toggle_feeding_plan,
    get_fountain_status, list_petkit_devices,
    # Information
    web_search, fetch_url, scrape_page, get_camera_snapshot, get_weather, get_calendar_events,
    # Ring cameras (via ring-mqtt bridge)
    check_ring_camera, list_ring_cameras, ring_privacy,
    ring_live_view, who_came_by, get_packages,
    # Memory
    remember, recall, forget, recall_journal, consolidate_memory,
    # Files & system
    read_file, write_file, list_files, run_shell, run_python,
    # Communication
    send_notification, send_email, send_sms, set_reminder, get_email_drafts,
    # Deep-work Focus mode (holds non-urgent notifications)
    focus_mode,
    # Background agents & dispatch
    get_agent_report, trigger_agent, spawn_task, ask_subagent,
    # Integrations
    spotify_control, spotify_desktop, github_tool, manage_notes,
    # Relationship memory (CRM-lite) + routine detection from history
    manage_people, detect_routines,
    # Travel (Miles) + outbound calls + dynamic Scout watches
    plan_trip, get_trips, make_call, manage_watches,
    # Task loop (GTD: inbox / next-actions / projects) + action journal/undo
    manage_tasks, get_recent_actions, undo_last_action,
    # macOS laptop control
    mac_screenshot, mac_clipboard, mac_system_info, mac_shell, mac_applescript,
    mac_open, mac_spotlight, mac_type, mac_notify,
    # Chrome control (user's signed-in browser) + Amazon Fresh add
    mac_chrome_navigate, mac_chrome_read, mac_chrome_js, mac_fresh_add,
    # Siri ecosystem (macOS Shortcuts) + iMessage reading + Apple Reminders
    mac_list_shortcuts, mac_run_shortcut, read_imessages,
    apple_reminder_add, apple_reminders_list,
    # Code inspection, read-only (writes go through Forge via spawn_task)
    jarvis_find_file, jarvis_grep_code, jarvis_list_files, jarvis_read_code,
    dev_list_dir, dev_read_file, dev_search_code,
    # Plugin management & personalization
    install_plugin, install_mcp_server, create_dashboard_widget,
    jarvis_restart_safe, list_mcp_servers, list_plugins, test_tool,
    get_user_profile, update_user_profile,
    get_setup_status, personalize_jarvis,
    # Finance — unified spending insights (Spend Guardian + budgets)
    get_spending_insights,
    # Grocery agent control
    get_grocery_status, approve_grocery_order, trigger_grocery_run, suggest_meals,
    learn_fresh_cart, get_usual_order, pin_favorite_product,
    # Workout coach (Apollo) + fused readiness score
    get_todays_workout, get_workout_plan, plan_workout_week, get_readiness,
    # Nutrition (Sage), visual memory (glasses), hands-free capture
    log_meal, get_nutrition_today, log_sighting, recall_visual, quick_capture,
    trigger_classpass_scan, get_class_suggestions, book_class, manage_classpass_favorites,
    search_classpass, join_classpass_waitlist,
]

# ---------------------------------------------------------------------------
# Dynamic plugin registry
# ---------------------------------------------------------------------------

_registry  = PluginRegistry()
_graph_lock = threading.RLock()

# One compiled agent per model tier + a reference to the default for compat
_agents: dict[str, object] = {}
llm        = None
tool_node  = None
agent      = None


def _strip_thinking_blocks(msg):
    """Drop thinking/redacted_thinking blocks from an AIMessage's content.

    Sonnet-5 emits thinking blocks by default. When the graph is streamed
    (stream_mode="messages"), langchain-anthropic's chunk aggregation can mangle
    them (block present but its 'thinking' text missing) — and when the tool
    loop replays that message, the API 400s with
    "messages.N.content.0.thinking.thinking: Field required", forcing every
    sonnet reply into the slow non-streaming fallback. We never request
    extended thinking explicitly, so replaying without the blocks is valid —
    strip them before the message enters graph state. (Still on latest
    langchain-anthropic 1.4.8, which hasn't fixed the aggregation.)
    """
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        cleaned = [b for b in content
                   if not (isinstance(b, dict)
                           and b.get("type") in ("thinking", "redacted_thinking"))]
        if len(cleaned) != len(content):
            msg = msg.model_copy(update={"content": cleaned})
    return msg


def _call_model(llm_ref, state):
    """Agent node: closed over the llm instance it was built with."""
    response = llm_ref.invoke(state["messages"])
    return {"messages": [_strip_thinking_blocks(response)]}


def _rebuild_graph() -> None:
    """Recompile one LangGraph agent per model tier sharing the same tool set."""
    global _agents, llm, tool_node, agent

    mcp_tools = load_mcp_tools()
    all_tools = _core_tools + _registry.get_all_tools() + mcp_tools
    new_tool_node = ToolNode(all_tools)
    new_agents: dict[str, object] = {}

    for tier in ("haiku", "sonnet", "opus"):
        new_llm  = build_llm(model=tier, temperature=0.3).bind_tools(all_tools)
        new_call = functools.partial(_call_model, new_llm)

        workflow = StateGraph(AgentState)
        workflow.add_node("agent", new_call)
        workflow.add_node("tools", new_tool_node)
        workflow.set_entry_point("agent")
        workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        workflow.add_edge("tools", "agent")
        new_agents[tier] = workflow.compile()

    with _graph_lock:
        _agents   = new_agents
        agent     = new_agents["sonnet"]   # backward-compat global
        tool_node = new_tool_node

    print(f"[LLM] Graph rebuilt — {len(all_tools)} tools "
          f"({len(_core_tools)} core + {len(_registry.get_all_tools())} plugins "
          f"+ {len(mcp_tools)} MCP), "
          f"3 model tiers: {', '.join(TIER_MODELS.values())}")


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

r = redis.Redis(host=REDIS_HOST, decode_responses=True)


def _push_tool_event(tool: str, args_preview: str, status: str, result_preview: str) -> None:
    event = {
        "id": str(uuid.uuid4())[:8],
        "tool": tool,
        "args_preview": args_preview,
        "status": status,
        "result_preview": result_preview,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        r.lpush("jarvis:tool_events", json.dumps(event))
        r.ltrim("jarvis:tool_events", 0, 99)
    except Exception:
        pass


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


def should_continue(state: AgentState):
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return END


# Initial build
_registry.register_on_change(_rebuild_graph)
_registry.load_all()
_rebuild_graph()


# ---------------------------------------------------------------------------
# Plugin hot-reload watcher
# ---------------------------------------------------------------------------

def _plugin_reload_watcher() -> None:
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
# Sentence streaming helpers
# ---------------------------------------------------------------------------

_SENT_END = re.compile(r'(?<=[.!?…])\s+')

# Camera/video-frame markers embedded by the mobile_gateway (/ask/image, /ask/video)
_IMG_MARKER = re.compile(r"\[GLASSES_CAMERA_IMAGE:\s*(data:image/[^\]]+)\]")


def _split_sentences(buf: str) -> tuple[list[str], str]:
    parts = _SENT_END.split(buf)
    if len(parts) == 1:
        return [], buf
    return [p.strip() for p in parts[:-1] if p.strip()], parts[-1]


# ---------------------------------------------------------------------------
# Request processing
# ---------------------------------------------------------------------------

def _build_system_prompt(room: str) -> list[dict]:
    """Return a list of Anthropic content blocks for the system message.

    Block 0 — the large static base prompt — is marked for prompt caching.
    Block 1 — the small dynamic suffix (time, room, verbosity) — is not cached
              because it changes every request.
    """
    profile_raw = r.get("user:profile") or "{}"
    try:
        profile = json.loads(profile_raw)
    except Exception:
        profile = {}
    prefs          = profile.get("preferences", {})
    active_plugins = ", ".join(profile.get("enabled_plugins", [])) or "none"
    verbosity      = prefs.get("response_verbosity", "balanced")
    personas       = r.get("agents:personas") or ""

    is_voice = not room.startswith(("mobile-", "glasses-"))
    voice_note = (
        "\nThis response will be spoken aloud. Keep it to 2-3 sentences maximum. "
        "No bullet points, no markdown, no lists — plain spoken prose only."
        if is_voice else ""
    )

    persona_note = (
        f"\nYour reasoning-agent team (refer to them by name when handing off a task, "
        f"e.g. 'I'll pass that to Brad'): {personas}"
        if personas else ""
    )

    dynamic = (
        f"Current time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"User location: {room}\n"
        f"Active plugins: {active_plugins}\n"
        f"Response style: {verbosity}"
        f"{persona_note}"
        f"{voice_note}"
    )

    return [
        # Static portion — eligible for Anthropic prompt caching (>= 1024 tokens for Sonnet/Opus)
        {"type": "text", "text": _BASE_PROMPT, "cache_control": {"type": "ephemeral"}},
        # Dynamic portion — changes every request, not cached
        {"type": "text", "text": dynamic},
    ]


async def _process_async(text: str, room: str, tier: str = "sonnet", on_sentence=None) -> str:
    """Stream response sentence-by-sentence via on_sentence callback.

    Selects the agent compiled for the given model tier.
    Always returns the full accumulated response for history storage.
    """
    ACTIVE_ROOM["room"] = room

    with _graph_lock:
        current_agent = _agents.get(tier, _agents.get("sonnet"))

    system_blocks = _build_system_prompt(room)
    system        = SystemMessage(content=system_blocks)

    history_key = f"conversation:{USER_ID}"
    history_raw = r.lrange(history_key, -20, -1)
    history = []
    for h in history_raw:
        msg     = json.loads(h)
        content = msg["content"]
        if isinstance(content, list):
            has_tool_use = any(
                isinstance(item, dict) and item.get("type") in ("tool_use", "tool_result")
                for item in content
            )
            if has_tool_use:
                continue
            content = [
                item for item in content
                if not (isinstance(item, dict) and item.get("type") in
                        ("image", "thinking", "redacted_thinking"))
            ]
            if not content:
                continue
        elif isinstance(content, str) and "[GLASSES_CAMERA_IMAGE:" in content:
            content = re.sub(r"\[GLASSES_CAMERA_IMAGE:[^\]]*\]\n?", "", content).strip()
            if not content:
                continue
        if msg["role"] == "user":
            history.append(HumanMessage(content=content))
        elif msg["role"] == "assistant":
            history.append(AIMessage(content=content))

    # Convert camera-image markers into REAL multimodal content blocks. The
    # gateway embeds photos/video-frames as [GLASSES_CAMERA_IMAGE: data:...]
    # text markers; without this conversion Claude receives megabytes of
    # base64 AS TEXT and cannot see the image at all.
    _img_uris = _IMG_MARKER.findall(text)
    if _img_uris:
        _cleaned = _IMG_MARKER.sub("", text).strip() or "What am I looking at?"
        user_content: list | str = (
            [{"type": "text", "text": _cleaned}]
            + [{"type": "image_url", "image_url": {"url": u}} for u in _img_uris[:8]]
        )
    else:
        user_content = text
    messages = [system] + history + [HumanMessage(content=user_content)]

    full_response = ""
    sentence_buf  = ""
    _emitted_tc_ids: set = set()

    try:
        async for chunk, metadata in current_agent.astream(
            {"messages": messages}, stream_mode="messages"
        ):
            node = metadata.get("langgraph_node")

            # Capture tool results from the tools node
            if node == "tools":
                if hasattr(chunk, "name") and chunk.name:
                    result = str(chunk.content)[:200] if chunk.content else ""
                    _push_tool_event(chunk.name, "", "done", result)
                continue

            if node != "agent":
                continue

            # Capture new tool calls announced in agent AIMessage chunks
            if hasattr(chunk, "tool_calls") and chunk.tool_calls:
                for tc in chunk.tool_calls:
                    tc_id = tc.get("id") or ""
                    if tc_id and tc_id not in _emitted_tc_ids:
                        _emitted_tc_ids.add(tc_id)
                        args = tc.get("args", {})
                        args_preview = json.dumps(args, ensure_ascii=False)[:120] if args else ""
                        _push_tool_event(tc.get("name", "unknown"), args_preview, "calling", "")

            if not hasattr(chunk, "content") or not chunk.content:
                continue

            # content can be a plain str (streaming text delta) or a list of
            # Anthropic content blocks ({"type":"text","text":"..."}) which is
            # what the API returns after a tool-use turn completes.
            # NOTE: must not reuse the `text` parameter here — it's the user's
            # message and is stored to conversation history after the loop.
            raw_content = chunk.content
            if isinstance(raw_content, str):
                chunk_text = raw_content
            elif isinstance(raw_content, list):
                chunk_text = "".join(
                    item.get("text", "") if isinstance(item, dict) else ""
                    for item in raw_content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            else:
                chunk_text = str(raw_content)

            if not chunk_text:
                continue

            sentence_buf  += chunk_text
            full_response += chunk_text

            sentences, sentence_buf = _split_sentences(sentence_buf)
            for s in sentences:
                if on_sentence:
                    on_sentence(s)

        remainder = sentence_buf.strip()
        if remainder:
            if on_sentence:
                on_sentence(remainder)

    except Exception as exc:
        print(f"[LLM] Streaming error ({exc}), falling back to ainvoke")
        result = await current_agent.ainvoke({"messages": messages})
        raw    = result["messages"][-1].content
        if isinstance(raw, list):
            full_response = " ".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in raw
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip() or "(no text response)"
        else:
            full_response = str(raw)
        if on_sentence:
            on_sentence(full_response)

    if not full_response:
        full_response = "(no text response)"

    if isinstance(full_response, list):
        full_response = " ".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in full_response
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip() or "(no text response)"

    stored_text = re.sub(r"\[GLASSES_CAMERA_IMAGE:[^\]]*\]", "[camera image]", text)
    r.rpush(history_key, json.dumps({"role": "user",      "content": stored_text}))
    r.rpush(history_key, json.dumps({"role": "assistant", "content": full_response}))
    r.ltrim(history_key, -40, -1)

    return full_response


def process_request(text: str, room: str) -> str:
    return asyncio.run(_process_async(text, room))


# ---------------------------------------------------------------------------
# MQTT handler
# ---------------------------------------------------------------------------

def _fanout_to_active_surfaces(
    client, response: str, source_room: str = "", title: str = "Jarvis",
    always_persist: bool = False,
) -> None:
    """Push a message to every active surface that didn't originate the request.

    Used both after a user-initiated reply and (via on_agent_report) when a
    background agent completes, so proactive results reach the iPhone / glasses
    HUD instead of only being spoken in one room.

    ``always_persist``: for PROACTIVE content (morning brief, agent reports),
    publish to the iPhone push topic even when no iPhone surface is currently
    active — the gateway persists every push to surface:pushes, so it's waiting
    in the app's feed when opened. Without this, a 6 AM brief spoken to an empty
    room (app not connected) vanishes with no trace — exactly the bug seen.
    """
    source_is_mobile = source_room.startswith(("mobile-", "glasses-", "siri-", "satellite-"))
    try:
        pushed_iphone = False
        active = r.smembers("jarvis:active_surfaces")
        for surface_id in active:
            meta_raw = r.get(f"jarvis:surface:{surface_id}:meta")
            if not meta_raw:
                continue
            meta         = json.loads(meta_raw)
            surface_type = meta.get("type", "")
            if surface_type == "iphone":
                if source_is_mobile:
                    continue
                client.publish(
                    "jarvis/surfaces/iphone/push",
                    json.dumps({"text": response, "title": title}),
                )
                pushed_iphone = True
            elif surface_type == "mac":
                pass  # Mac already hears the spoken response
        # Proactive content: guarantee it reaches the phone feed even if the app
        # wasn't connected (the gateway persists it for later fetch).
        if always_persist and not pushed_iphone and not source_is_mobile:
            client.publish(
                "jarvis/surfaces/iphone/push",
                json.dumps({"text": response, "title": title}),
            )
    except Exception as e:
        print(f"[LLM] Surface fanout error: {e}")


# ---------------------------------------------------------------------------
# Self-echo suppression — the follow-up mic window catches Jarvis's own TTS
# playback and re-submits it as a "command", creating a feedback loop that
# derails conversations and burns tokens. We remember what Jarvis just said and
# drop incoming voice text that matches it (targeted — real user speech, even a
# follow-up, won't match Jarvis's own words).
# ---------------------------------------------------------------------------
import collections as _collections
import difflib as _difflib
import re as _re
import time as _time

_recent_replies: "_collections.deque" = _collections.deque(maxlen=16)
_recent_replies_lock = threading.Lock()
_ECHO_WINDOW_S = 60.0


def _norm_echo(t: str) -> str:
    return _re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def _record_reply(text: str) -> None:
    n = _norm_echo(text)
    if len(n) >= 4:
        with _recent_replies_lock:
            _recent_replies.append((_time.time(), n))


def _is_self_echo(text: str) -> bool:
    n = _norm_echo(text)
    if len(n) < 4:
        return False
    now = _time.time()
    with _recent_replies_lock:
        recents = [rt for ts, rt in _recent_replies if now - ts < _ECHO_WINDOW_S]
    words = set(n.split())
    for rt in recents:
        if n in rt or rt in n:
            return True
        if _difflib.SequenceMatcher(None, n, rt).ratio() > 0.80:
            return True
        rt_words = set(rt.split())
        if words and len(words & rt_words) / len(words) > 0.70:
            return True
    return False


# ---------------------------------------------------------------------------
# Conversation-end reasoning — decide when a spoken exchange is naturally over so
# the STT doesn't keep the follow-up mic open. A closing turn ("thanks", "ok that's
# all", "goodbye") still gets a warm reply, but we signal the STT to stop listening
# (via jarvis:voice:end_turn:{room}) instead of re-opening the window.
# ---------------------------------------------------------------------------
_CLOSER_PHRASES = {
    "thanks", "thank you", "thanks a lot", "thanks so much", "thank you so much",
    "thx", "ty", "ok", "okay", "cool", "got it", "great", "perfect", "awesome",
    "nice", "sounds good", "nevermind", "never mind", "thats all", "that's all",
    "thats it", "that's it", "no thats it", "no that's it", "thats everything",
    "that's everything", "goodbye", "bye", "good night", "goodnight", "night",
    "nope", "no thanks", "no thank you", "all good", "appreciate it", "stop",
    "that will be all", "that'll be all", "we're good", "were good", "im good",
    "i'm good", "you're the best", "youre the best",
}


def _is_conversation_closer(text: str) -> bool:
    """True if the user's turn is a closing/acknowledgment with no new request."""
    if "?" in text:
        return False   # a question always expects an answer
    t = _re.sub(r"[^a-z' ]", " ", text.lower())
    t = " ".join(t.split())
    if not t:
        return False
    if t in _CLOSER_PHRASES:
        return True
    words = t.split()
    if len(words) <= 4:
        for c in _CLOSER_PHRASES:
            if t == c or t.startswith(c + " ") or t.endswith(" " + c):
                return True
    return False


def _handle_request(client, data):
    text = data["text"]
    room = data["room"]

    # Household multi-user foundation: record the verified speaker (from
    # speaker_verify) so JARVIS can address people by name and future per-user
    # logic can key off it. Full per-user profile isolation needs each voice
    # enrolled; this is the non-breaking hook that makes it possible.
    speaker = data.get("speaker")
    if speaker:
        try:
            r.set("jarvis:current_speaker", str(speaker), ex=1800)
        except Exception:
            pass

    # Drop Jarvis's own voice echoed back through the follow-up mic window.
    if _is_self_echo(text):
        print(f"[LLM] Dropped self-echo (not processing): '{text[:60]}'")
        try:
            r.set(f"jarvis:voice:state:{room}", "ready")
        except Exception:
            pass
        return

    tier = _classify_tier(text)
    print(f"[LLM] Processing (tier={tier}): '{text}' (from {room})")

    # Mark state + start rotating thinking words
    try:
        r.set(f"jarvis:voice:state:{room}", "thinking", ex=120)
    except Exception:
        pass

    _stop_thinking = threading.Event()
    threading.Thread(
        target=_start_thinking_rotation, args=(_stop_thinking,), daemon=True
    ).start()

    _sentence_queue: list[str] = []

    def on_sentence(sentence: str) -> None:
        _sentence_queue.append(sentence)

    response = ""
    try:
        response = asyncio.run(_process_async(text, room, tier=tier, on_sentence=on_sentence))
    except Exception as e:
        err_str = str(e)
        print(f"[LLM] Error: {err_str}")
        # Only wipe history for genuine history-corruption errors (dangling
        # tool_use/tool_result pairs). A bare "400" match once deleted the
        # user's whole conversation over an unrelated temperature-param error.
        if "tool_use_id" in err_str or "tool_result" in err_str:
            try:
                r.delete(f"conversation:{USER_ID}")
                print(f"[LLM] Cleared corrupted history for user {USER_ID}")
            except Exception:
                pass
        response = "I'm sorry, I encountered an error processing that request."
        _sentence_queue.append(response)
    finally:
        _stop_thinking.set()

    # Ensure we always have at least one sentence to publish (and a done signal fires)
    if not _sentence_queue:
        _sentence_queue.append(response or "(no response)")

    # Set state to "speaking" — will be cleared when tts_mac publishes /done
    try:
        r.set(f"jarvis:voice:state:{room}", "speaking", ex=300)
    except Exception:
        pass

    for i, sentence in enumerate(_sentence_queue):
        is_final = (i == len(_sentence_queue) - 1)
        _record_reply(sentence)   # remember it so the echo doesn't loop back
        client.publish(
            f"jarvis/tts/{room}/speak",
            json.dumps({"text": sentence, "room": room, "is_final": is_final}),
        )

    print(f"[LLM] Tier={tier}, {len(_sentence_queue)} sentence(s): '{response[:80]}...'")

    # Conversation-end reasoning: if the user's turn was a closer, still let the
    # reply play, but tell the STT to STOP listening instead of re-opening the
    # follow-up mic. Skipped for non-voice surfaces (mobile/glasses/siri).
    if not room.startswith(("mobile-", "glasses-", "siri-", "satellite-")) and _is_conversation_closer(text):
        try:
            r.set(f"jarvis:voice:end_turn:{room}", "1", ex=30)
            print(f"[LLM] Conversation closer detected ('{text[:30]}') — ending turn.")
        except Exception:
            pass

    # Proactive dispatches (e.g. the morning brief) always land in the phone feed.
    _fanout_to_active_surfaces(client, response, source_room=room,
                               always_persist=bool(data.get("proactive")))


def on_llm_request(client, userdata, msg):
    data = json.loads(msg.payload)
    threading.Thread(target=_handle_request, args=(client, data), daemon=True).start()


def on_tts_done(client, userdata, msg):
    """TTS has finished playing the final sentence — reset voice state to ready."""
    try:
        data = json.loads(msg.payload)
        room = data.get("room", "office")
        r.set(f"jarvis:voice:state:{room}", "ready")
        print(f"[LLM] TTS done in '{room}' → state=ready")
    except Exception as e:
        print(f"[LLM] on_tts_done error: {e}")


# Background agents whose reports are NOT auto-pushed to surfaces here:
#   - newsletter/job_monitor/web_monitor/price_monitor: noisy/large — read in dashboard
#   - grocery: pushes its own richer report (with cart links) to surfaces directly
#   - classpass: pushes its own favorite-opened / auto-booked alerts directly
#   - ambient: fans its own alerts out directly
_FANOUT_AGENT_BLOCKLIST = {
    "newsletter", "job_monitor", "web_monitor", "price_monitor", "grocery",
    "classpass", "ambient",
    # morning_brief's report is just an internal "dispatched to the brain"
    # status — the actual briefing reaches surfaces via the brain's proactive
    # response, so don't also push the status as a card.
    "morning_brief",
    # chronicle's nightly report is an internal "journaled X" status; the weekly
    # review delivers its own card directly. Neither should fan out as a card.
    "chronicle", "weekly_review",
    # finance runs every 30 min to refresh the widget — don't push each run.
    # Its daily report is read on demand via the get_financial_report tool.
    "finance",
}


def on_agent_report(client, userdata, msg):
    """A background agent finished — fan a short summary out to active surfaces.

    This makes background-agent completions proactively visible on the iPhone /
    glasses HUD (Month 4b), not just stored in Redis for the dashboard.
    """
    try:
        data   = json.loads(msg.payload)
        name   = data.get("agent", "agent")
        report = (data.get("report") or "").strip()
        if not report or name in _FANOUT_AGENT_BLOCKLIST:
            return
        summary = report if len(report) <= 240 else report[:237] + "…"
        _fanout_to_active_surfaces(client, summary, title=name.replace("_", " ").title(),
                                   always_persist=True)
        print(f"[LLM] Fanned out '{name}' report to surfaces.")
    except Exception as e:
        print(f"[LLM] on_agent_report error: {e}")


def main():
    mqtt_client = mqtt.Client()
    mqtt_client.connect(MQTT_HOST, MQTT_PORT)

    mqtt_client.subscribe("jarvis/llm/request")
    mqtt_client.message_callback_add("jarvis/llm/request", on_llm_request)

    # Subscribe to TTS done so we can reset voice state precisely
    mqtt_client.subscribe("jarvis/tts/+/done")
    mqtt_client.message_callback_add("jarvis/tts/+/done", on_tts_done)

    # Proactive fanout: surface background-agent completions to active devices
    mqtt_client.subscribe("jarvis/agents/+/report")
    mqtt_client.message_callback_add("jarvis/agents/+/report", on_agent_report)

    print("[LLM] Agent ready, waiting for requests…")
    mqtt_client.loop_forever()


if __name__ == "__main__":
    main()
