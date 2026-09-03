"""JARVIS Background Agent Runner.

Reads config/agents.yml, schedules each enabled agent via APScheduler (cron),
and publishes reports to MQTT when agents complete.  Manual triggers arrive
on MQTT topic ``jarvis/agents/{name}/trigger``.
"""

import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
import redis
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ambient_agent import AmbientAgent
from atlas_agent import AtlasAgent
from chronicle_agent import ChronicleAgent
from classpass_agent import ClasspassAgent
from developer_agent import DeveloperAgent
from email_agent import EmailAgent
from finance_agent import FinanceAgent
from grocery_agent import GroceryAgent
from habit_agent import HabitAgent
from job_monitor_agent import JobMonitorAgent
from newsletter_agent import NewsletterAgent
from qa_agent import QAAgent
from price_monitor_agent import PriceMonitorAgent
from spend_guardian_agent import SpendGuardianAgent
from research_agent import ResearchAgent
from sentry_agent import SentryAgent
from task_agent import TaskAgent
from travel_agent import TravelAgent
from web_monitor_agent import WebMonitorAgent
from workout_agent import WorkoutAgent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
CONFIG_PATH = Path(os.environ.get("AGENTS_CONFIG", "/config/agents.yml"))
# Mac bridge (native host API). host.docker.internal only resolves when this
# container runs ON the Mac — the VPS split sets MAC_BRIDGE_HOST to the Mac's
# tailnet name instead (docs/HETZNER_SETUP.md).
MAC_BRIDGE_URL = (f"http://{os.environ.get('MAC_BRIDGE_HOST') or 'host.docker.internal'}"
                  f":{os.environ.get('MAC_BRIDGE_PORT', '7777')}")

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
    "chronicle": ChronicleAgent,     # nightly daily-memory journal
    "weekly_review": ChronicleAgent, # Sunday "week in review" (action=weekly_review)
    "spend_guardian": SpendGuardianAgent,   # subscriptions + unusual-charge watch
    "habits": HabitAgent,            # Echo — habit mining → Approval Inbox
    "qa": QAAgent,                   # Vega — nightly live-brain regression suite
    "atlas": AtlasAgent,             # Atlas — fused weekly health overview
    "travel": TravelAgent,           # Miles — assisted booking + price watch
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
    "spend_guardian": ("Brad", "finance watch — subscriptions, unusual charges, low-balance alerts"),
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
    "habits":     ("Echo",   "habit mining — spots your routines and deviations, suggests automations via the Approval Inbox"),
    "qa":         ("Vega",   "quality watch — nightly regression checks against the live brain"),
    "atlas":      ("Atlas",  "health analyst — fuses sleep, training, nutrition, and recovery into one weekly picture"),
    "travel":     ("Miles",  "travel — trip planning, price watching, and assisted booking (you always pay yourself)"),
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
    # Stamp the agent for automatic LLM cost attribution (usage.record reads this
    # contextvar); scoped to this task so concurrent agents don't cross-attribute.
    try:
        import usage
        usage.set_current_agent(name)
    except Exception:
        pass
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
                        f"{MAC_BRIDGE_URL}/applescript", data=body,
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
        client.subscribe("jarvis/approvals/resolve")
        # Ring cameras (via the ring-mqtt bridge, if running): device state,
        # motion/ding events, and snapshot JPEGs. Harmless no-op when the
        # bridge isn't up. homeassistant/# carries ring-mqtt's discovery
        # configs, which is where human-readable camera NAMES live.
        client.subscribe("ring/#")
        client.subscribe("homeassistant/camera/+/+/config")
        client.subscribe("homeassistant/binary_sensor/+/+/config")
        client.subscribe("jarvis/presence/home")   # iPhone geofence arrivals
        print("[AgentRunner] MQTT connected, subscribed to trigger + ring + presence topics.")
    else:
        print(f"[AgentRunner] MQTT connect failed (rc={rc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Home arrival (iPhone geofence) — the reliable presence signal Sentry's lone
# indoor camera can't give. On "arrived": run the arrival scene (warm lamps)
# and speak a welcome; a re-arrival within ARRIVAL_DEBOUNCE is ignored so a
# GPS flap at the geofence edge doesn't re-greet.
# ---------------------------------------------------------------------------

ARRIVAL_DEBOUNCE_S = int(os.environ.get("ARRIVAL_DEBOUNCE_S", "1800"))
# The 120m geofence fires while the user is still WALKING UP — hold the
# spoken greeting until a camera motion confirms they're actually inside,
# with this timer as the fallback so camera-less arrivals still get greeted.
ARRIVAL_GREET_FALLBACK_S = int(os.environ.get("ARRIVAL_GREET_FALLBACK_S", "150"))
# A "left" within this many seconds of an "arrived" is treated as GPS jitter at
# the geofence edge and ignored (prevents phantom departures eating the greeting).
DEPARTURE_GRACE_S = int(os.environ.get("DEPARTURE_GRACE_S", "120"))


def _speak_pending_greeting() -> None:
    """Speak the held arrival greeting exactly once (motion or timer wins)."""
    try:
        raw = _redis.get("jarvis:arrival:pending")
        if not raw or not _redis.delete("jarvis:arrival:pending"):
            return   # already consumed by the other path
        data = json.loads(raw)
        room = data.get("room", "office")
        _mqtt.publish(f"jarvis/tts/{room}/speak", json.dumps({
            "text": data.get("text", ""), "room": room, "is_final": True,
        }))
        print("[AgentRunner] arrival greeting spoken")
    except Exception as exc:
        print(f"[AgentRunner] pending greeting error: {exc}")


def _on_presence(client, userdata, msg):
    try:
        _payload = json.loads(msg.payload)
        ev = _payload.get("event", "")
        source = _payload.get("source", "")
    except Exception:
        return
    if ev == "arrived":
        # Clear away/departure state on ANY arrival — even a debounced re-arrival —
        # so away-mode can't stick at 1 while the user is actually home (it did:
        # a flapped arrived→left→arrived left 'away' set with the user home).
        _redis.delete("jarvis:away")
        _redis.delete("jarvis:departure:debounce")
        _redis.set("jarvis:arrival:ts",
                   str(datetime.now(timezone.utc).timestamp()), ex=3600)
        # Voice-triggered ("Jarvis, I'm home"): run the scene, but the BRAIN speaks
        # the welcome itself — skip the held camera-gated greeting and the debounce.
        if source == "voice":
            _redis.set("user:presence:home", "1")
            try:
                profile = json.loads(_redis.get("user:profile") or "{}")
            except Exception:
                profile = {}
            _run_arrival_scene(profile)
            print("[AgentRunner] VOICE arrival → scene (brain speaks the welcome)")
            return
        if not _redis.set("jarvis:arrival:debounce", "1", nx=True, ex=ARRIVAL_DEBOUNCE_S):
            print("[AgentRunner] arrival within debounce — home state refreshed, skipping greeting")
            return
        print("[AgentRunner] iPhone geofence: ARRIVED home → scene + greeting")
        try:
            profile = json.loads(_redis.get("user:profile") or "{}")
        except Exception:
            profile = {}

        # 1) Arrival scene: warm living-room lamps (HA) — only during the
        #    profile's scene window (default 18-07); a noon arrival shouldn't
        #    turn lamps on. Falls back to a named Shortcut if configured.
        scene_ran = _run_arrival_scene(profile)

        # 2) Warm spoken welcome — HELD until the first camera motion says
        #    the user is actually inside (the geofence fires ~120m out).
        #    Timer fallback guarantees it still fires without cameras.
        #    Only claim the lights when we actually touched them.
        who = profile.get("resident_name", "sir")
        text = (f"Welcome home, {who}. I've brought the lights up for you."
                if scene_ran else f"Welcome home, {who}.")
        room = profile.get("sentry_greeting_room", "office")
        _redis.set("jarvis:arrival:pending",
                   json.dumps({"text": text, "room": room}), ex=600)
        threading.Timer(ARRIVAL_GREET_FALLBACK_S,
                        _speak_pending_greeting).start()
    elif ev == "left":
        # GPS-flap hysteresis: a "left" fired shortly after an "arrived" is jitter
        # at the geofence boundary (the 120m radius trips while walking up). Ignore
        # it — otherwise it cancels the held arrival greeting and runs a phantom
        # departure (turning the lamps back off), which is exactly what ate a
        # greeting on an arrived→left→arrived flap.
        try:
            arr_ts = float(_redis.get("jarvis:arrival:ts") or 0)
        except Exception:
            arr_ts = 0.0
        if arr_ts and (datetime.now(timezone.utc).timestamp() - arr_ts) < DEPARTURE_GRACE_S:
            print("[AgentRunner] 'left' within grace of arrival — GPS flap, ignoring")
            return
        # Deliberately KEEP the arrival debounce: deleting it here meant a GPS
        # flap at the geofence edge (left→arrived seconds apart) re-greeted
        # every time. The 30-min TTL from the last greeting is the guard.
        _redis.delete("jarvis:arrival:pending")
        # Departure debounce: a GPS flap at the edge shouldn't kill the lights
        # and re-arm repeatedly.
        if not _redis.set("jarvis:departure:debounce", "1", nx=True, ex=ARRIVAL_DEBOUNCE_S):
            print("[AgentRunner] departure within debounce — skipping scene")
            return
        print("[AgentRunner] iPhone geofence: LEFT home → departure scene + away mode")
        try:
            profile = json.loads(_redis.get("user:profile") or "{}")
        except Exception:
            profile = {}
        # Away mode: Sentry reads this to escalate (any person = alert, not just
        # notable). Cleared on the next arrival.
        _redis.set("jarvis:away", "1")
        _run_departure_scene(profile)
    return


def _run_departure_scene(profile: dict) -> None:
    """On leaving home: turn the managed lamps off (unless disabled) so nothing
    is left burning. Guarded — a failure must not affect away-mode."""
    import urllib.request as _rq
    if not profile.get("departure_lights_off", True):
        return
    ha_url = os.environ.get("HA_URL", "")
    ha_token = os.environ.get("HA_TOKEN", "")
    # Departure turns off EVERY managed lamp — not just the arrival pair. The
    # bedroom lamp stayed burning after a departure because it was never on
    # this list. Override with profile departure_lights if set.
    entities = profile.get("departure_lights") or (
        profile.get("arrival_lights",
                    ["light.living_room_left", "light.living_room_right"])
        + ["light.bedroom_lamp"]
    )
    if not (ha_url and ha_token):
        return
    try:
        body = json.dumps({"entity_id": entities}).encode()
        req = _rq.Request(f"{ha_url}/api/services/light/turn_off", data=body,
                          headers={"Authorization": f"Bearer {ha_token}",
                                   "Content-Type": "application/json"})
        _rq.urlopen(req, timeout=15)
        print(f"[AgentRunner] departure scene: turned off {entities}")
    except Exception as exc:
        print(f"[AgentRunner] departure HA scene failed: {exc}")


def _in_scene_hours(profile: dict) -> bool:
    """True inside the arrival-scene window (profile arrival_scene_hours,
    default 18-07 local) — same semantics as Sentry's _is_dark_hours."""
    try:
        from zoneinfo import ZoneInfo
        hour = datetime.now(ZoneInfo(os.environ.get("USER_TZ", "America/Chicago"))).hour
    except Exception:
        hour = datetime.now().hour
    try:
        start, end = (int(x) for x in str(profile.get("arrival_scene_hours", "18-07")).split("-"))
    except Exception:
        start, end = 18, 7
    return hour >= start or hour < end if start > end else start <= hour < end


def _run_arrival_scene(profile: dict) -> bool:
    """Warm the living-room lamps via HA (preferred) or a named Shortcut.
    Returns True if a scene actually ran (so the greeting can be honest)."""
    import urllib.request as _rq
    if not _in_scene_hours(profile):
        print("[AgentRunner] arrival outside scene hours — lamps untouched")
        return False
    ha_url = os.environ.get("HA_URL", "")
    ha_token = os.environ.get("HA_TOKEN", "")
    entities = profile.get("arrival_lights",
                           ["light.living_room_left", "light.living_room_right"])
    if ha_url and ha_token:
        try:
            body = json.dumps({
                "entity_id": entities,
                "color_temp_kelvin": int(profile.get("arrival_kelvin", 2700)),
                "brightness_pct": int(profile.get("arrival_brightness_pct", 40)),
            }).encode()
            req = _rq.Request(f"{ha_url}/api/services/light/turn_on", data=body,
                              headers={"Authorization": f"Bearer {ha_token}",
                                       "Content-Type": "application/json"})
            _rq.urlopen(req, timeout=15)
            print(f"[AgentRunner] arrival scene: warmed {entities}")
            return True
        except Exception as exc:
            print(f"[AgentRunner] arrival HA scene failed: {exc}")
    # Fallback: a macOS Shortcut, if the user set one
    sc = (profile.get("arrival_scene_shortcut") or "").strip()
    if sc:
        try:
            body = json.dumps({"name": sc, "timeout": 30}).encode()
            _rq.urlopen(_rq.Request(f"{MAC_BRIDGE_URL}/shortcut/run",
                                    data=body, headers={"Content-Type": "application/json"}),
                        timeout=35)
            return True
        except Exception as exc:
            print(f"[AgentRunner] arrival shortcut failed: {exc}")
    return False


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

            # Held arrival greeting: first camera activity after a geofence
            # arrival means the user is actually inside/at the door — say it.
            if _redis.get("jarvis:arrival:pending"):
                _speak_pending_greeting()

            # Wake detection is PERSON-gated in Sentry's vision verdict now —
            # raw motion here was waking the briefing for the CATS. During an
            # unbriefed wake window we bypass the per-camera cooldown so the
            # user's entrance can't hide behind a cat's cooldown from minutes
            # earlier (one cheap haiku vision call per motion, mornings only).
            wake_pending = False
            if kind == "motion":
                try:
                    from zoneinfo import ZoneInfo
                    lt = datetime.now(ZoneInfo(os.environ.get("USER_TZ", "America/Chicago")))
                    ws, we = (int(x) for x in
                              os.environ.get("WAKE_WINDOW", "5-10").split("-"))
                    wake_pending = (ws <= lt.hour < we and not
                                    _redis.get(f"jarvis:briefed:{lt.strftime('%Y-%m-%d')}"))
                except Exception:
                    pass
            _redis.lpush("ring:events", json.dumps(
                {"device": device, "kind": kind, "ts": now}))
            _redis.ltrim("ring:events", 0, 199)

            # Cooldown gates the LLM assessment, not the event log above.
            cd_key = f"sentry:cooldown:{device}"
            if (_redis.set(cd_key, "1", nx=True, ex=SENTRY_COOLDOWN_S) or wake_pending) and _loop:
                name = _redis.hget("ring:camera_names", device) or device
                print(f"[AgentRunner] Ring {kind} on '{name}' → dispatching Sentry")
                _loop.call_soon_threadsafe(
                    _loop.create_task,
                    run_agent("sentry", SentryAgent,
                              {"device": device, "camera_name": name, "kind": kind}),
                )
    except Exception as exc:
        print(f"[AgentRunner] ring event error: {exc}", file=sys.stderr)


def _on_approval_resolve(client, userdata, msg):
    """Single executor for Approval Inbox decisions (see approvals.py).

    Every surface (dashboard, gateway/iOS, the brain's manage_approvals tool)
    publishes {id, decision, by} here; running the action in one place means a
    double-tap on two surfaces can't execute it twice.
    """
    try:
        body = json.loads(msg.payload or b"{}")
        approval_id = body.get("id", "")
        decision = body.get("decision", "")
        if not approval_id or not decision:
            return
        import approvals
        result = approvals.resolve(_redis, approval_id, decision,
                                   by=body.get("by", "user"))
        print(f"[AgentRunner] approval {approval_id}: {decision} → {result}")
    except Exception as exc:
        print(f"[AgentRunner] approval resolve error: {exc}", file=sys.stderr)


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
    _mqtt.message_callback_add("jarvis/approvals/resolve", _on_approval_resolve)
    _mqtt.message_callback_add("ring/#", _on_ring_event)
    _mqtt.message_callback_add("homeassistant/camera/+/+/config", _on_ring_discovery)
    _mqtt.message_callback_add("homeassistant/binary_sensor/+/+/config", _on_ring_discovery)
    _mqtt.message_callback_add("jarvis/presence/home", _on_presence)
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
