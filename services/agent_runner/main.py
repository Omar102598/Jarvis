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
from classpass_agent import ClasspassAgent
from developer_agent import DeveloperAgent
from email_agent import EmailAgent
from finance_agent import FinanceAgent
from grocery_agent import GroceryAgent
from job_monitor_agent import JobMonitorAgent
from newsletter_agent import NewsletterAgent
from price_monitor_agent import PriceMonitorAgent
from research_agent import ResearchAgent
from sentry_agent import SentryAgent
from task_agent import TaskAgent
from web_monitor_agent import WebMonitorAgent
from workout_agent import WorkoutAgent

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
    "email": EmailAgent,
    "job_monitor": JobMonitorAgent,
    "web_monitor": WebMonitorAgent,
    "research": ResearchAgent,
    "task": TaskAgent,
    "price_monitor": PriceMonitorAgent,
    "grocery": GroceryAgent,
    "classpass": ClasspassAgent,
    "finance": FinanceAgent,
    "developer": DeveloperAgent,
    "workout": WorkoutAgent,
    "sentry": SentryAgent,
    "morning_brief": AmbientAgent,   # dedicated daily-briefing schedule
}

# Agents that can be dispatched on demand with params from the trigger payload
# (e.g. spawn_task), even when not present in agents.yml.
DISPATCHABLE = {"research", "task", "developer", "workout"}

# Personas for REASONING agents (those that make LLM calls) — gives Jarvis a name
# to hand off to ("I'll pass that to Brad, the finance agent") and lets the UI
# distinguish reasoning agents from mechanical ones. Mechanical agents
# (price_monitor, ambient) intentionally have no persona.
AGENT_PERSONAS = {
    "finance":    ("Brad",   "finance — balances, budgets, spending analysis, money advice"),
    "grocery":    ("Remy",   "groceries — fitness-aware meal planning and shopping"),
    "classpass":  ("Kai",    "fitness classes — finding and booking studio classes"),
    "newsletter": ("Walter", "daily news digest"),
    "email":      ("Hermes", "email — inbox triage, important mail, packages"),
    "research":   ("Ada",    "deep multi-source research"),
    "task":       ("Jeeves", "general delegated tasks"),
    "job_monitor":("Riley",  "job listings matching your criteria"),
    "web_monitor":("Scout",  "watching topics/queries for new content"),
    "developer":  ("Forge",  "development — Jarvis self-modification and any coding project"),
    "workout":    ("Apollo", "personal trainer — programs your lifting week around recovery and your ClassPass classes"),
    "sentry":     ("Sentry", "camera watch — assesses Ring motion events and alerts only when it matters"),
}

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
            persona = AGENT_PERSONAS.get(name)
            who = persona[0] if persona else f"the {name} agent"
            _mqtt.publish(
                f"jarvis/tts/{notify_room}/speak",
                json.dumps({
                    "text": f"Sir, {who} has finished. {report[:600]}",
                    "room": notify_room,
                }),
            )
        print(f"[AgentRunner] {name} done. Preview: {report[:80]}…")

    except Exception as exc:
        _redis.set(f"agent:{name}:status", "error")
        _redis.set(f"agent:{name}:last_error", str(exc))

        # Credit exhaustion: tell the user directly (once per hour), not just
        # a silent agent failure — this is why tasks "mysteriously" die.
        err_l = str(exc).lower()
        if ("credit" in err_l or "billing" in err_l) and \
                _redis.set("jarvis:credit_alerted", "1", nx=True, ex=3600):
            try:
                import urllib.request as _rq
                profile = json.loads(_redis.get("user:profile") or "{}")
                phone = profile.get("imessage_to", "")
                if phone:
                    text = (f"⚠️ Jarvis: the '{name}' agent failed — Anthropic API "
                            "credits appear exhausted. Top up at console.anthropic.com "
                            "or agents will keep failing.")
                    script = (f'tell application "Messages" to send {json.dumps(text)} '
                              f'to buddy "{phone}" of (service 1 whose service type is iMessage)')
                    body = json.dumps({"script": script, "timeout": 20}).encode()
                    _rq.urlopen(_rq.Request(
                        "http://host.docker.internal:7777/applescript", data=body,
                        headers={"Content-Type": "application/json"}), timeout=25)
            except Exception:
                pass
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
        # Ring cameras (via the ring-mqtt bridge, if running): device state,
        # motion/ding events, and snapshot JPEGs. Harmless no-op when the
        # bridge isn't up. homeassistant/# carries ring-mqtt's discovery
        # configs, which is where human-readable camera NAMES live.
        client.subscribe("ring/#")
        client.subscribe("homeassistant/camera/+/+/config")
        client.subscribe("homeassistant/binary_sensor/+/+/config")
        print("[AgentRunner] MQTT connected, subscribed to trigger + ring topics.")
    else:
        print(f"[AgentRunner] MQTT connect failed (rc={rc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Ring camera events (ring-mqtt bridge)
# ---------------------------------------------------------------------------

SENTRY_COOLDOWN_S = int(os.environ.get("SENTRY_COOLDOWN_S", "600"))


def _on_ring_discovery(client, userdata, msg):
    """Map ring-mqtt's HA discovery configs → human-readable camera names."""
    try:
        cfg = json.loads(msg.payload)
        device = cfg.get("device") or cfg.get("dev") or {}
        name = device.get("name") or device.get("n") or ""
        # state_topic like ring/<location>/camera/<device_id>/...
        state_topic = cfg.get("state_topic") or cfg.get("stat_t") or ""
        parts = state_topic.split("/")
        if name and len(parts) >= 4 and parts[0] == "ring":
            _redis.hset("ring:camera_names", parts[3], name)
    except Exception:
        pass


def _on_ring_event(client, userdata, msg):
    """Cache Ring state in Redis; dispatch Sentry on motion/ding events."""
    try:
        # Privacy mode ("give me some privacy"): drop EVERYTHING — no snapshot
        # caching, no event logging, no Sentry — until the key expires.
        if _redis.get("sentry:privacy"):
            return
        parts = msg.topic.split("/")  # ring/<location>/camera/<device>/<...>
        if len(parts) < 5 or parts[2] != "camera":
            return
        location, device = parts[1], parts[3]
        suffix = "/".join(parts[4:])
        now = datetime.now(timezone.utc).isoformat()

        _redis.hset("ring:cameras", device, json.dumps(
            {"location": location, "last_seen": now}))

        if suffix == "snapshot/image":
            import base64 as _b64
            _redis.set(f"ring:camera:{device}:snapshot",
                       _b64.b64encode(msg.payload).decode())
            _redis.set(f"ring:camera:{device}:snapshot_ts", now)
            return
        if suffix in ("motion/attributes", "ding/attributes"):
            _redis.set(f"ring:camera:{device}:{parts[4]}_attrs",
                       msg.payload.decode(errors="ignore")[:2000])
            return
        if suffix in ("motion/state", "ding/state") and msg.payload == b"ON":
            kind = parts[4]  # motion | ding

            # Wake detection: the first indoor motion in the morning window
            # IS the "user woke up" signal — fire the briefing then, not on a
            # fixed clock. The agent itself dedupes to once per local day
            # (cron fallback fires later only if no motion was seen).
            if kind == "motion":
                try:
                    from zoneinfo import ZoneInfo
                    lt = datetime.now(ZoneInfo(os.environ.get("USER_TZ", "America/Chicago")))
                    ws, we = (int(x) for x in
                              os.environ.get("WAKE_WINDOW", "5-10").split("-"))
                    already = _redis.get(f"jarvis:briefed:{lt.strftime('%Y-%m-%d')}")
                    if ws <= lt.hour < we and not already and _loop:
                        print(f"[AgentRunner] First morning motion → wake briefing")
                        _loop.call_soon_threadsafe(
                            _loop.create_task,
                            run_agent("morning_brief", AmbientAgent,
                                      {"action": "morning_brief", "room": "office"}))
                except Exception as exc:
                    print(f"[AgentRunner] wake-brief check failed: {exc}", file=sys.stderr)
            _redis.lpush("ring:events", json.dumps(
                {"device": device, "kind": kind, "ts": now}))
            _redis.ltrim("ring:events", 0, 199)

            # Cooldown gates the LLM assessment, not the event log above.
            cd_key = f"sentry:cooldown:{device}"
            if _redis.set(cd_key, "1", nx=True, ex=SENTRY_COOLDOWN_S) and _loop:
                name = _redis.hget("ring:camera_names", device) or device
                print(f"[AgentRunner] Ring {kind} on '{name}' → dispatching Sentry")
                _loop.call_soon_threadsafe(
                    _loop.create_task,
                    run_agent("sentry", SentryAgent,
                              {"device": device, "camera_name": name, "kind": kind}),
                )
    except Exception as exc:
        print(f"[AgentRunner] ring event error: {exc}", file=sys.stderr)


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
    persona_lines = []
    for name, cfg in agents_cfg.items():
        persona = AGENT_PERSONAS.get(name)
        persona_name = persona[0] if persona else ""
        kind = "reasoning" if persona else "mechanical"
        if persona:
            persona_lines.append(f"{persona[0]} — {persona[1]}")
        _redis.set(
            f"agent:{name}:meta",
            json.dumps(
                {
                    "name": name,
                    "display_name": cfg.get("display_name", name),
                    "persona_name": persona_name,
                    "kind": kind,
                    "description": cfg.get("description", ""),
                    "schedule": cfg.get("schedule", ""),
                    "enabled": cfg.get("enabled", False),
                }
            ),
        )
        # Initialise status only if not already set (preserve across restarts)
        if not _redis.exists(f"agent:{name}:status"):
            _redis.set(f"agent:{name}:status", "idle")
    # Summary Jarvis reads into its system prompt so it can hand off by name
    _redis.set("agents:personas", "; ".join(persona_lines))

    # Connect MQTT — topic-routed callbacks (triggers vs Ring camera events)
    _mqtt.user_data_set(agents_cfg)
    _mqtt.on_connect = _on_connect
    _mqtt.message_callback_add("jarvis/agents/+/trigger", _on_trigger)
    _mqtt.message_callback_add("ring/#", _on_ring_event)
    _mqtt.message_callback_add("homeassistant/camera/+/+/config", _on_ring_discovery)
    _mqtt.message_callback_add("homeassistant/binary_sensor/+/+/config", _on_ring_discovery)
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
