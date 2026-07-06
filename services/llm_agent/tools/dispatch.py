"""Dispatch tools for JARVIS — delegate work to sub-agents.

Two patterns, mirroring how Claude dispatches work:

  spawn_task(task)        — fire-and-forget. Hands a long/open-ended goal to a
                            background task agent and returns immediately. The
                            result lands in Redis (and is announced if a room
                            is set). Use for "go research X", "keep digging".

  ask_subagent(type, q)   — synchronous. Delegates to a focused specialist
                            sub-agent (researcher / coder / planner) that runs
                            its own tool loop, and waits for its answer. Use
                            when you need the result to continue the reply.
"""

import json
import os
import uuid

import paho.mqtt.publish as mqtt_publish
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# Context the agent sets per request so spawned tasks can announce results
# back to the right room. Set by main.py before invoking the graph.
ACTIVE_ROOM = {"room": ""}


@tool
def spawn_task(task: str, agent: str = "task") -> str:
    """Dispatch a long-running task to a background agent and return now.

    Use for open-ended work that shouldn't block the conversation: deep
    research, multi-step gathering, "go find out and let me know". The task
    runs in the background; its result is saved and announced when ready.

    ALL coding work goes to 'developer' (Forge): modifying Jarvis's own code,
    adding tools/features, fixing bugs, and any software project the user
    starts. Forge reads code, writes files, runs builds/tests, and rebuilds
    services itself — give it a complete task description, including the
    project directory for non-Jarvis projects.

    Args:
        task: A clear, self-contained description of what to accomplish.
        agent: 'task' (general), 'research' (deep web research), or
            'developer' (Forge — all code changes and dev projects).
    """
    agent = agent if agent in ("task", "research", "developer") else "task"
    payload = {
        "source": "llm",
        "params": {"task": task, "notify_room": ACTIVE_ROOM.get("room", "")},
    }
    try:
        mqtt_publish.single(
            f"jarvis/agents/{agent}/trigger",
            payload=json.dumps(payload),
            hostname=MQTT_HOST,
            port=MQTT_PORT,
        )
    except Exception as exc:
        return f"Could not dispatch the task: {exc}"
    return (
        f"I've dispatched a {agent} agent to handle that. I'll let you know "
        "when it's done — or ask me for the result in a minute."
    )


# --- Synchronous specialist sub-agents ---

_SPECIALISTS = {
    "researcher": (
        "You are a research specialist. Use web_search and fetch_url to gather "
        "current information, then give a concise, sourced answer.",
        ["web_search", "fetch_url"],
    ),
    "coder": (
        "You are a coding specialist. Use run_python to compute, parse data, or "
        "verify logic. Show the result and a one-line explanation.",
        ["run_python", "read_file", "write_file"],
    ),
    "planner": (
        "You are a planning specialist. Break the request into a clear, ordered "
        "plan of concrete steps. Reason carefully; you have no tools.",
        [],
    ),
}


def _collect_tools(names: list) -> list:
    """Resolve specialist tool names to actual tool objects (lazy import)."""
    registry = {}
    try:
        from tools.web_search import web_search
        registry["web_search"] = web_search
    except Exception:
        pass
    try:
        from tools.web import fetch_url
        registry["fetch_url"] = fetch_url
    except Exception:
        pass
    try:
        from tools.code_exec import run_python
        registry["run_python"] = run_python
    except Exception:
        pass
    try:
        from tools.files import read_file, write_file
        registry["read_file"] = read_file
        registry["write_file"] = write_file
    except Exception:
        pass
    return [registry[n] for n in names if n in registry]


@tool
async def ask_subagent(specialist: str, question: str) -> str:
    """Delegate a focused question to a specialist sub-agent and wait for it.

    Use when you need a deeper answer in a specific domain before replying:
    'researcher' (web research), 'coder' (compute/parse with Python), or
    'planner' (break down a complex request).

    Args:
        specialist: One of 'researcher', 'coder', 'planner'.
        question: The self-contained question or task for the specialist.
    """
    spec = _SPECIALISTS.get(specialist)
    if not spec:
        return f"Unknown specialist '{specialist}'. Choose researcher, coder, or planner."
    system_prompt, tool_names = spec

    try:
        from llm_factory import build_llm

        llm = build_llm(temperature=0.2)
        tools = _collect_tools(tool_names)

        if not tools:
            # Planner (or tools unavailable): single reasoning pass
            result = await llm.ainvoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=question),
            ])
            return result.content

        # Tool-using specialist: small ReAct loop
        from langgraph.graph import END, StateGraph
        from langgraph.prebuilt import ToolNode

        bound = llm.bind_tools(tools)

        def call_model(state):
            return {"messages": state["messages"] + [bound.invoke(state["messages"])]}

        def should_continue(state):
            last = state["messages"][-1]
            return "tools" if getattr(last, "tool_calls", None) else END

        wf = StateGraph(dict)
        wf.add_node("agent", call_model)
        wf.add_node("tools", ToolNode(tools))
        wf.set_entry_point("agent")
        wf.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        wf.add_edge("tools", "agent")
        graph = wf.compile()

        out = await graph.ainvoke({
            "messages": [
                SystemMessage(content=system_prompt),
                HumanMessage(content=question),
            ]
        })
        return out["messages"][-1].content
    except Exception as exc:
        return f"The {specialist} sub-agent failed: {exc}"
