"""Agent tools for JARVIS — read background agent results and trigger runs."""

import json
import os

import paho.mqtt.publish as mqtt_publish
import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)


@tool
def get_agent_report(agent_name: str, limit: int = 1) -> str:
    """Retrieve the most recent report(s) from a background agent.

    Use this when the user asks what an agent found, e.g. "What did the
    newsletter agent find today?" or "Any new jobs from the job monitor?"

    Args:
        agent_name: One of 'newsletter', 'job_monitor', 'web_monitor'
        limit: How many of the most recent reports to return (1–5, default 1)
    """
    limit = max(1, min(limit, 5))
    raw_list = _r.lrange(f"agent:{agent_name}:reports", 0, limit - 1)

    if not raw_list:
        status = _r.get(f"agent:{agent_name}:status") or "unknown"
        if status == "running":
            return f"The {agent_name} agent is currently running. Try again in a moment."
        return (
            f"No reports found for '{agent_name}'. "
            "The agent may not have run yet or the name may be incorrect. "
            "Available agents: newsletter, job_monitor, web_monitor."
        )

    reports = []
    for raw in raw_list:
        try:
            entry = json.loads(raw)
            ts = entry.get("timestamp", "unknown time")
            text = entry.get("report", "")
            reports.append(f"[{ts}]\n{text}")
        except json.JSONDecodeError:
            continue

    return "\n\n---\n\n".join(reports) if reports else "Could not parse agent reports."


@tool
def trigger_agent(agent_name: str) -> str:
    """Run a background agent right now instead of waiting for its schedule.

    Use when the user wants fresh results immediately, e.g. "run the job
    monitor now" or "refresh the newsletter". The agent runs in the
    background; check its results shortly after with get_agent_report.

    Args:
        agent_name: Which agent to run, e.g. 'newsletter', 'job_monitor',
            'web_monitor'.
    """
    try:
        mqtt_publish.single(
            f"jarvis/agents/{agent_name}/trigger",
            payload=json.dumps({"source": "llm"}),
            hostname=MQTT_HOST,
            port=MQTT_PORT,
        )
    except Exception as exc:
        return f"Could not trigger '{agent_name}': {exc}"
    return (
        f"Started the {agent_name} agent. It's running now — "
        "ask me for its report in a moment."
    )
