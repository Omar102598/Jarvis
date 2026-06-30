"""JARVIS Background Agent Runner.

Reads config/agents.yml, schedules each enabled agent via APScheduler (cron),
and publishes reports to MQTT when agents complete.  Manual triggers arrive
on MQTT topic ``jarvis/agents/{name}/trigger``.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
import redis
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ambient_agent import AmbientAgent
from grocery_agent import GroceryAgent
from job_monitor_agent import JobMonitorAgent
from newsletter_agent import NewsletterAgent
from price_monitor_agent import PriceMonitorAgent
from research_agent import ResearchAgent
from task_agent import TaskAgent
from web_monitor_agent import WebMonitorAgent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
CONFIG_PATH = Path(os.environ.get("AGENTS_CONFIG", "/config/agents.yml"))

AGENT_CLASSES = {
    "ambient": AmbientAgent,
    "newsletter": NewsletterAgent,
    "job_monitor": JobMonitorAgent,
    "web_monitor": WebMonitorAgent,
    "research": ResearchAgent,
    "task": TaskAgent,
    "price_monitor": PriceMonitorAgent,
    "grocery": GroceryAgent,
}

# Agents that can be dispatched on demand with params from the trigger payload
# (e.g. spawn_task), even when not present in agents.yml.
DISPATCHABLE = {"research", "task"}

# ---------------------------------------------------------------------------
# Shared clients
# ---------------------------------------------------------------------------

_redis = redis.Redis(host=REDIS_HOST, decode_responses=True)
_mqtt = mqtt.Client()
_loop: asyncio.AbstractEventLoop | None = None

# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


async def run_agent(name: str, agent_class, params: dict) -> None:
    """Execute one agent, persist its report, and publish to MQTT.

    If params contains 'notify_room', a spoken announcement is sent to that
    room's TTS topic when the agent finishes — this is how spawn_task results
    reach the user who dispatched them.
    """
    print(f"[AgentRunner] Starting: {name}")
    _redis.set(f"agent:{name}:status", "running")
    notify_room = (params or {}).get("notify_room", "")

    try:
        # Each run gets its own Redis connection to avoid cross-thread issues
        r = redis.Redis(host=REDIS_HOST, decode_responses=True)
        agent = agent_class(name, params, r)
        report = await agent.run()
        agent.store_report(report)
        _redis.set(f"agent:{name}:status", "idle")
        _redis.delete(f"agent:{name}:last_error")

        _mqtt.publish(
            f"jarvis/agents/{name}/report",
            json.dumps(
                {
                    "agent": name,
                    "report": report,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ),
        )

        if notify_room:
            _mqtt.publish(
                f"jarvis/tts/{notify_room}/speak",
                json.dumps({
                    "text": f"Sir, the {name} agent has finished. {report[:600]}",
                    "room": notify_room,
                }),
            )
        print(f"[AgentRunner] {name} done. Preview: {report[:80]}…")

    except Exception as exc:
        _redis.set(f"agent:{name}:status", "error")
        _redis.set(f"agent:{name}:last_error", str(exc))
        if notify_room:
            _mqtt.publish(
                f"jarvis/tts/{notify_room}/speak",
                json.dumps({
                    "text": f"Sir, the {name} task ran into a problem: {exc}",
                    "room": notify_room,
                }),
            )
        print(f"[AgentRunner] {name} failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# MQTT callbacks
# ---------------------------------------------------------------------------


def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe("jarvis/agents/+/trigger")
        print("[AgentRunner] MQTT connected, subscribed to trigger topics.")
    else:
        print(f"[AgentRunner] MQTT connect failed (rc={rc})", file=sys.stderr)


def _on_trigger(client, userdata, msg):
    """Handle a manual run request published to jarvis/agents/{name}/trigger.

    The payload may carry {"params": {...}} to override/supply parameters at
    trigger time. This is how spawn_task delivers an ad-hoc task description
    (and notify_room) to the generic 'task' / 'research' agents, which need
    not appear in agents.yml.
    """
    parts = msg.topic.split("/")  # ['jarvis', 'agents', name, 'trigger']
    if len(parts) < 4:
        return
    name = parts[2]

    # Parse optional params from the payload
    payload_params: dict = {}
    try:
        if msg.payload:
            body = json.loads(msg.payload)
            if isinstance(body, dict):
                payload_params = body.get("params", {}) or {}
    except (json.JSONDecodeError, ValueError):
        pass

    agent_class = AGENT_CLASSES.get(name)
    if not agent_class:
        print(f"[AgentRunner] Trigger for unknown agent: {name}")
        return

    # Merge configured params (if any) with payload params (payload wins).
    cfg = userdata.get(name) or {}
    if not cfg and name not in DISPATCHABLE and not payload_params:
        print(f"[AgentRunner] Trigger for unconfigured agent: {name}")
        return
    params = {**cfg.get("params", {}), **payload_params}

    if _loop is not None:
        _loop.call_soon_threadsafe(
            _loop.create_task,
            run_agent(name, agent_class, params),
        )
        print(f"[AgentRunner] Manual trigger queued for: {name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    global _loop
    _loop = asyncio.get_running_loop()

    # Load agent configuration
    if not CONFIG_PATH.exists():
        print(f"[AgentRunner] Config file not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    with CONFIG_PATH.open() as f:
        config = yaml.safe_load(f)

    agents_cfg: dict[str, dict] = {
        a["name"]: a for a in config.get("agents", [])
    }

    # Write agent metadata to Redis so the dashboard can display it
    for name, cfg in agents_cfg.items():
        _redis.set(
            f"agent:{name}:meta",
            json.dumps(
                {
                    "name": name,
                    "display_name": cfg.get("display_name", name),
                    "description": cfg.get("description", ""),
                    "schedule": cfg.get("schedule", ""),
                    "enabled": cfg.get("enabled", False),
                }
            ),
        )
        # Initialise status only if not already set (preserve across restarts)
        if not _redis.exists(f"agent:{name}:status"):
            _redis.set(f"agent:{name}:status", "idle")

    # Connect MQTT
    _mqtt.user_data_set(agents_cfg)
    _mqtt.on_connect = _on_connect
    _mqtt.on_message = _on_trigger
    _mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    _mqtt.loop_start()

    # Schedule enabled agents
    scheduler = AsyncIOScheduler()

    for name, cfg in agents_cfg.items():
        if not cfg.get("enabled", False):
            print(f"[AgentRunner] {name}: disabled — skipping")
            continue

        agent_class = AGENT_CLASSES.get(name)
        if not agent_class:
            print(f"[AgentRunner] Unknown agent type '{name}' — skipping", file=sys.stderr)
            continue

        schedule = cfg.get("schedule", "0 8 * * *")
        params = cfg.get("params", {})

        try:
            trigger = CronTrigger.from_crontab(schedule)
        except Exception as exc:
            print(f"[AgentRunner] Bad cron for '{name}': {exc}", file=sys.stderr)
            continue

        scheduler.add_job(
            run_agent,
            trigger=trigger,
            args=[name, agent_class, params],
            id=name,
            name=cfg.get("display_name", name),
            replace_existing=True,
            misfire_grace_time=300,
        )
        print(f"[AgentRunner] Scheduled '{name}' → cron: {schedule}")

    scheduler.start()
    print("[AgentRunner] All agents scheduled and running.")

    try:
        while True:
            await asyncio.sleep(30)
    except (KeyboardInterrupt, SystemExit):
        print("[AgentRunner] Shutting down…")
    finally:
        scheduler.shutdown(wait=False)
        _mqtt.loop_stop()
        _mqtt.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
