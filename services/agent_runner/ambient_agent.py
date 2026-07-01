"""JARVIS Ambient Agent — proactive monitoring and intelligent interrupts.

Runs every 5 minutes. Checks configured triggers and publishes to the LLM
request topic when a condition fires, so Jarvis speaks proactively without
the user having to ask first.

Trigger categories:
  calendar   — events starting within the lookahead window (default 30 min)
  weather    — significant temperature change or rain since last check
  morning    — daily briefing at a configurable hour
  health     — HealthKit anomalies (sleep drop, resting-HR spike) vs. 7-day baseline
  user       — arbitrary conditions stored in Redis under jarvis:ambient:triggers

Cooldown: each trigger type tracks its last-fired timestamp in Redis. A trigger
won't re-fire until its cooldown has elapsed, preventing repeated announcements.

Redis keys used:
  jarvis:ambient:last_fire:{trigger_id}  — unix timestamp of last fire
  jarvis:ambient:triggers                — JSON list of user-defined trigger dicts
  jarvis:calendar:next_event             — set by the calendar tool (optional)
  jarvis:weather:last                    — JSON {temp_c, condition} of last check
"""

import asyncio
import json
import os
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import redis

from base_agent import BaseAgent
from llm_helper import run_llm

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")

# Room to route proactive announcements to — falls back to "office"
_DEFAULT_ROOM = os.environ.get("AMBIENT_ROOM", "office")


class AmbientAgent(BaseAgent):
    """Proactive monitoring agent. Checks triggers and speaks when they fire."""

    async def run(self) -> str:
        params = self.params or {}
        room = params.get("room", _DEFAULT_ROOM)
        calendar_lookahead_min = int(params.get("calendar_lookahead_min", 30))
        morning_hour = int(params.get("morning_hour", 8))
        weather_enabled = params.get("weather_enabled", True)

        triggered: list[str] = []

        triggered += await self._check_calendar(calendar_lookahead_min, room)
        triggered += await self._check_morning_briefing(morning_hour, room)
        triggered += self._check_user_triggers(room)
        triggered += self._check_health_anomalies(room)
        triggered += self._check_classpass(room)

        if weather_enabled:
            triggered += await self._check_weather(room)

        if triggered:
            return f"Ambient: fired {len(triggered)} trigger(s): {'; '.join(triggered)}"
        return "Ambient: no triggers."

    # ------------------------------------------------------------------
    # Calendar trigger
    # ------------------------------------------------------------------

    async def _check_calendar(self, lookahead_min: int, room: str) -> list[str]:
        trigger_id = "calendar_upcoming"
        if not self._cooled_down(trigger_id, cooldown_min=lookahead_min):
            return []

        raw = self.r.get("jarvis:calendar:next_event")
        if not raw:
            return []

        try:
            event = json.loads(raw)
        except Exception:
            return []

        start_str = event.get("start")
        title = event.get("title", "an event")
        if not start_str:
            return []

        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except Exception:
            return []

        now = datetime.now(timezone.utc)
        minutes_away = (start - now).total_seconds() / 60

        if 0 < minutes_away <= lookahead_min:
            msg = (
                f"Sir, heads up — {title} starts in "
                f"{int(minutes_away)} minute{'s' if minutes_away > 1 else ''}."
            )
            self._publish(msg, room, title="Calendar")
            self._mark_fired(trigger_id)
            return [f"calendar:{title}"]

        return []

    # ------------------------------------------------------------------
    # Morning briefing trigger
    # ------------------------------------------------------------------

    async def _check_morning_briefing(self, morning_hour: int, room: str) -> list[str]:
        trigger_id = "morning_briefing"
        # Fire once per day within the target hour; cooldown 23h to avoid double-fire
        if not self._cooled_down(trigger_id, cooldown_min=23 * 60):
            return []

        now = datetime.now()
        if now.hour != morning_hour:
            return []

        # Ask Jarvis to compose a briefing
        prompt = (
            "Good morning. Please give me a brief morning briefing: today's date, "
            "any upcoming calendar events today, current weather, and one key item "
            "from the daily newsletter if available. Keep it under 4 sentences."
        )
        self._publish(prompt, room, is_llm_request=True)
        self._mark_fired(trigger_id)
        return ["morning_briefing"]

    # ------------------------------------------------------------------
    # Weather change trigger
    # ------------------------------------------------------------------

    async def _check_weather(self, room: str) -> list[str]:
        trigger_id = "weather_alert"
        if not self._cooled_down(trigger_id, cooldown_min=120):
            return []

        raw = self.r.get("jarvis:weather:last")
        if not raw:
            return []

        try:
            last = json.loads(raw)
        except Exception:
            return []

        last_temp = last.get("temp_c")
        last_condition = last.get("condition", "").lower()
        last_ts = float(last.get("ts", 0))

        # Only compare if we have a previous reading older than 30 min
        age_min = (datetime.now().timestamp() - last_ts) / 60
        if age_min < 30:
            return []

        raw_current = self.r.get("jarvis:weather:current")
        if not raw_current:
            return []
        try:
            current = json.loads(raw_current)
        except Exception:
            return []

        curr_temp = current.get("temp_c")
        curr_condition = current.get("condition", "").lower()

        alerts = []
        if last_temp is not None and curr_temp is not None:
            delta = abs(curr_temp - last_temp)
            if delta >= 5:
                direction = "dropped" if curr_temp < last_temp else "risen"
                alerts.append(
                    f"Temperature has {direction} by {delta:.0f}°C "
                    f"to {curr_temp:.0f}°C."
                )

        rain_keywords = {"rain", "drizzle", "shower", "storm", "thunder"}
        if any(k in curr_condition for k in rain_keywords) and not any(
            k in last_condition for k in rain_keywords
        ):
            alerts.append("Rain is now expected.")

        if alerts:
            msg = "Weather update, sir. " + " ".join(alerts)
            self._publish(msg, room, title="Weather")
            self._mark_fired(trigger_id)
            # Update baseline
            self.r.set(
                "jarvis:weather:last",
                json.dumps({**current, "ts": datetime.now().timestamp()}),
            )
            return ["weather_alert"]

        return []

    # ------------------------------------------------------------------
    # User-defined triggers stored in Redis
    # ------------------------------------------------------------------

    def _check_user_triggers(self, room: str) -> list[str]:
        raw = self.r.get("jarvis:ambient:triggers")
        if not raw:
            return []
        try:
            triggers = json.loads(raw)
        except Exception:
            return []

        fired = []
        for t in triggers:
            tid = t.get("id", "")
            cooldown = int(t.get("cooldown_min", 60))
            if not tid or not self._cooled_down(tid, cooldown_min=cooldown):
                continue

            condition_key = t.get("condition_key", "")
            condition_value = t.get("condition_value", "")
            message = t.get("message", "")

            if not condition_key or not message:
                continue

            actual = self.r.get(condition_key)
            if actual is not None and str(actual) == str(condition_value):
                self._publish(message, room)
                self._mark_fired(tid)
                fired.append(f"user:{tid}")

        return fired

    # ------------------------------------------------------------------
    # Health anomaly trigger (HealthKit)
    # ------------------------------------------------------------------

    def _check_health_anomalies(self, room: str) -> list[str]:
        trigger_id = "health_anomaly"
        if not self._cooled_down(trigger_id, cooldown_min=12 * 60):
            return []

        latest_raw = self.r.get("user:health:latest")
        if not latest_raw:
            return []
        try:
            latest = json.loads(latest_raw)
        except Exception:
            return []

        # Build a 7-day baseline (skip the most recent reading)
        try:
            hist_raw = self.r.lrange("user:health:history", 1, 14) or []
        except Exception:
            hist_raw = []

        def _avg(key: str) -> float | None:
            vals = []
            for item in hist_raw:
                try:
                    snap = json.loads(item)
                except Exception:
                    continue
                if snap.get(key) is not None:
                    vals.append(float(snap[key]))
            return sum(vals) / len(vals) if vals else None

        alerts: list[str] = []

        # Sleep: flag if last night was 1.5h+ below the weekly average
        sleep = latest.get("sleep_hours")
        sleep_baseline = _avg("sleep_hours")
        if sleep is not None and sleep_baseline and sleep < sleep_baseline - 1.5:
            alerts.append(
                f"You slept {sleep:.1f} hours last night, "
                f"well below your usual {sleep_baseline:.1f}."
            )

        # Resting heart rate: flag if 8+ bpm above the weekly average
        rhr = latest.get("resting_heart_rate")
        rhr_baseline = _avg("resting_heart_rate")
        if rhr is not None and rhr_baseline and rhr > rhr_baseline + 8:
            alerts.append(
                f"Your resting heart rate is {rhr:.0f}, "
                f"up from your usual {rhr_baseline:.0f} — possible fatigue or illness."
            )

        if alerts:
            msg = "A note on your health, sir. " + " ".join(alerts)
            self._publish(msg, room, title="Health")
            self._mark_fired(trigger_id)
            return ["health_anomaly"]

        return []

    # ------------------------------------------------------------------
    # ClassPass favorite-opened trigger
    # ------------------------------------------------------------------

    def _check_classpass(self, room: str) -> list[str]:
        """Surface a favorite ClassPass class whose booking window has opened.

        Reads the suggestions the ClassPass agent stored on its last scan. Only
        favorites that are open, not already booked, and not yet alerted are
        announced. Dedup is shared with the ClassPass agent via the
        ``classpass:seen`` Redis set, so a class is announced exactly once
        regardless of which loop notices it first.
        """
        raw = self.r.get("classpass:suggestions")
        if not raw:
            return []
        try:
            classes = json.loads(raw).get("classes", [])
        except Exception:
            return []

        try:
            booked = json.loads(self.r.get("classpass:booked") or "[]")
        except Exception:
            booked = []
        booked_ids = {b.get("id") for b in booked}

        fired: list[str] = []
        for c in classes:
            cid = c.get("id")
            if not cid or not c.get("is_favorite") or not c.get("booking_open"):
                continue
            if cid in booked_ids or self.r.sismember("classpass:seen", cid):
                continue

            self.r.sadd("classpass:seen", cid)
            self.r.expire("classpass:seen", 7 * 86400)

            instr = f" with {c['instructor']}" if c.get("instructor") else ""
            spots = f" ({c['spots']})" if c.get("spots") else ""
            msg = (
                f"Sir, a favorite just opened on ClassPass — {c.get('name','a class')} "
                f"at {c.get('studio','')}{instr}, {c.get('start_human','')}{spots}. "
                f"Say 'book it' to reserve."
            )
            self._publish(msg, room, title="ClassPass")
            fired.append(f"classpass:{cid}")
            if len(fired) >= 2:  # avoid a flood in a single tick
                break

        return fired

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cooled_down(self, trigger_id: str, cooldown_min: int) -> bool:
        """Return True if enough time has passed since the trigger last fired."""
        key = f"jarvis:ambient:last_fire:{trigger_id}"
        last_raw = self.r.get(key)
        if last_raw is None:
            return True
        elapsed_min = (datetime.now().timestamp() - float(last_raw)) / 60
        return elapsed_min >= cooldown_min

    def _mark_fired(self, trigger_id: str) -> None:
        key = f"jarvis:ambient:last_fire:{trigger_id}"
        self.r.set(key, str(datetime.now().timestamp()))

    def _publish(
        self, text: str, room: str, is_llm_request: bool = False, title: str = "Jarvis"
    ) -> None:
        """Publish a message — either direct TTS or routed through the LLM.

        Direct (already-composed) alerts are also fanned out to active iPhone /
        glasses surfaces so they appear on the HUD, not only spoken in the room.
        """
        try:
            client = mqtt.Client()
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)

            if is_llm_request:
                # Route through the LLM agent so Jarvis composes the full response
                # (the LLM agent fans the result out to surfaces itself).
                client.publish(
                    "jarvis/llm/request",
                    json.dumps({"text": text, "room": room}),
                )
            else:
                # Speak directly (no LLM needed — message is already composed)
                client.publish(
                    f"jarvis/tts/{room}/speak",
                    json.dumps({"text": text, "room": room, "is_final": True}),
                )
                # Fan out the same alert to iPhone / glasses HUD surfaces
                client.publish(
                    "jarvis/surfaces/iphone/push",
                    json.dumps({"text": text, "title": title}),
                )

            client.disconnect()
        except Exception as e:
            print(f"[AmbientAgent] MQTT publish error: {e}")
