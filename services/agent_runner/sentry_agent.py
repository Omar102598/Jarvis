"""Sentry — Ring camera watch agent.

Event-driven (dispatched by the runner's ring/# MQTT handler when a motion or
ding event fires, gated by a per-camera cooldown — see main._on_ring_event).
Looks at the camera's latest snapshot with Claude vision and decides whether
the event is worth telling the user about: a person, package, vehicle, an
open door, the dog somewhere unusual — versus wind, shadows, or the dog just
existing. Only notable events notify.

Notification channels (per the agreed multi-surface policy — proactive alerts
go to the phone): iOS app push (jarvis/surfaces/iphone/push) + iMessage
(reliable while APNs isn't set up yet; drop it once real push lands).
"""

import json
import os
from datetime import datetime, timezone

import aiohttp

from base_agent import BaseAgent

MAC_BRIDGE_URL = os.environ.get("MAC_BRIDGE_URL", "http://host.docker.internal:7777")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
SENTRY_MODEL = os.environ.get("SENTRY_MODEL", "claude-haiku-4-5-20251001")
# Public (tailnet) gateway base URL — used to attach snapshot images to pushes
GATEWAY_PUBLIC_URL = os.environ.get("GATEWAY_PUBLIC_URL", "").rstrip("/")
MOBILE_API_KEY = os.environ.get("MOBILE_API_KEY", "")

_ASSESS_PROMPT = """You are Sentry, a home-camera watch agent. A {kind} event just fired on
the '{camera}' Ring camera. Assess this snapshot.

CONTEXT (use it — it changes what is notable):
- This is an {placement} camera.
- The resident is currently {presence} (phone geofence — reliable).
- Household pets: {pets}. Pets alone are NEVER notable, indoors or out.
{face_line}
{resident_hint}

NOTABLE (worth interrupting the user): a package/delivery, a vehicle in the
driveway, a door/gate open that shouldn't be, a pet doing something
DESTRUCTIVE (not just existing/resting/walking), smoke, or anything genuinely
unusual. For PEOPLE, apply the presence rules:
- Resident HOME + indoor camera: a person is EXPECTED — it is almost
  certainly the resident living their life. NOT notable and NO announce
  unless the person is clearly a STRANGER doing something suspicious.
- Resident HOME + outdoor camera: visitors/deliveries are notable; the
  resident in their own yard is not.
- Resident AWAY + indoor camera: ANY person is ALWAYS notable — possible
  intruder; say so plainly and describe them.
- Resident AWAY + outdoor camera: people at the door/on the property are
  notable.
NOT notable: empty scene, shadows/light changes, plants moving, pets being
pets, known furniture.

A doorbell ding is ALWAYS notable — describe who/what is at the door.

"announce" (one short spoken line, JARVIS voice — British, warm, dry wit) is
ONLY for a person ARRIVING from OUTSIDE (walking toward the door, at the
door, doorbell). NEVER announce anyone seen on an indoor camera — someone
already inside is not "arriving". If the arriving person plausibly matches
the resident's description set "arrival": "resident" with a warm welcome
("Welcome home, sir — I trust the walk went well"). Clearly someone else →
"arrival": "guest" with a description ("Sir, someone's approaching — tall
gentleman, navy jacket"). Unsure → "arrival": "unknown", neutral announce.
No one arriving → "arrival": "", "announce": "".

Return ONLY JSON: {{"notable": true/false, "summary": "one sentence of what
you see", "detail": "1-2 sentences with anything useful (appearance,
direction, what they're carrying)", "announce": "", "arrival": "",
"package_visible": true/false, "person_visible": true/false}}
("package_visible": is a package/box/envelope sitting unattended in frame?
"person_visible": is a HUMAN in frame? Pets alone = false. Only mark true
when you're confident it's a human — a cat or dog is NOT a person.)"""

# Camera-name substrings treated as INDOOR (overridable via profile
# ``indoor_cameras``, a list of name fragments). Everything else = outdoor.
_INDOOR_HINTS = ("living", "bedroom", "kitchen", "office", "hallway",
                 "indoor", "room")


class SentryAgent(BaseAgent):
    """Assess one Ring event with vision; notify only if it matters."""

    async def run(self) -> str:
        device = self.params.get("device", "")
        camera = self.params.get("camera_name") or device or "unknown camera"
        kind = self.params.get("kind", "motion")
        if not device:
            return "Sentry: no device in trigger params."

        # Privacy mode — belt to the event-handler's suspenders
        if self.r.get("sentry:privacy"):
            return "Privacy mode active — event ignored."

        snap_b64 = self.r.get(f"ring:camera:{device}:snapshot")
        snap_ts = self.r.get(f"ring:camera:{device}:snapshot_ts") or ""
        if not snap_b64:
            if kind == "ding":
                await self._notify(f"🔔 Doorbell — someone is at '{camera}' "
                                   "(no snapshot available yet).")
                return f"Ding on {camera}; notified without snapshot."
            return f"Motion on {camera} but no snapshot cached yet — skipping."

        self.log_event("thinking", f"{kind} on '{camera}' — assessing snapshot with vision")
        verdict = await self._assess(snap_b64, camera, kind, device)
        if verdict is None:
            self.log_event("finding", f"{camera}: vision assessment failed")
            return f"Sentry: vision assessment failed for {camera}."

        summary = verdict.get("summary", "").strip()
        detail = verdict.get("detail", "").strip()
        notable = bool(verdict.get("notable")) or kind == "ding"
        self.log_event(
            "finding",
            f"{camera}: {'NOTABLE' if notable else 'not notable'} — {summary}",
        )

        # Keep an assessed-event log the check_ring_camera tool can surface
        self.r.lpush("ring:events:assessed", json.dumps({
            "device": device, "camera": camera, "kind": kind,
            "notable": notable, "summary": summary,
            "ts": datetime.now(timezone.utc).isoformat(),
            "snapshot_ts": snap_ts,
        }))
        self.r.ltrim("ring:events:assessed", 0, 99)

        # ---- Wake briefing: fire when the user is actually UP for the day ----
        # A person being visible ≠ awake — a single 5am glimpse (bathroom trip)
        # used to fire the brief before the user was up. Now we require either:
        #   • SUSTAINED presence — ≥2 person-sightings spanning ≥WAKE_CONFIRM_GAP_S
        #     (a passthrough is one sighting; getting up for the day is repeated
        #     activity over minutes), OR
        #   • an authoritative "awake" signal from the Apple Watch (user:awake:today,
        #     posted by the iOS app from HealthKit sleep tracking) — fires at once.
        # Person-gated (verdict), once per local day (the brief's own SET NX dedupes).
        if verdict.get("person_visible"):
            try:
                import time as _time
                from zoneinfo import ZoneInfo
                lt = datetime.now(ZoneInfo(os.environ.get("USER_TZ", "America/Chicago")))
                ws, we = (int(x) for x in os.environ.get("WAKE_WINDOW", "5-10").split("-"))
                date = lt.strftime("%Y-%m-%d")
                if (ws <= lt.hour < we) and not self.r.get(f"jarvis:briefed:{date}"):
                    gap = int(os.environ.get("WAKE_CONFIRM_GAP_S", "240"))
                    skey = f"jarvis:wake:sightings:{date}"
                    now_ts = _time.time()
                    self.r.lpush(skey, str(now_ts))
                    self.r.ltrim(skey, 0, 29)
                    self.r.expire(skey, 6 * 3600)
                    sightings = []
                    for x in (self.r.lrange(skey, 0, 29) or []):
                        try:
                            sightings.append(float(x))
                        except Exception:
                            pass
                    awake = bool(self.r.get("user:awake:today"))
                    span = (now_ts - min(sightings)) if sightings else 0
                    sustained = len(sightings) >= 2 and span >= gap
                    if awake or sustained:
                        import paho.mqtt.publish as mqtt_pub
                        mqtt_pub.single(
                            "jarvis/agents/morning_brief/trigger",
                            json.dumps({"params": {"action": "morning_brief",
                                                   "room": "office"}}),
                            hostname=MQTT_HOST, port=MQTT_PORT,
                        )
                        why = ("watch-confirmed awake" if awake
                               else f"sustained presence ({len(sightings)} sightings/{int(span/60)}min)")
                        print(f"[Sentry] Morning wake → briefing ({why})")
                    else:
                        print(f"[Sentry] person in wake window but not sustained yet "
                              f"({len(sightings)} sighting(s)) — holding brief")
            except Exception as exc:
                print(f"[Sentry] wake-brief dispatch failed: {exc}")

        # ---- Package watch: stateful presence tracking per camera ----------
        pkg_key = f"ring:package:{device}"
        pkg_was = self.r.get(pkg_key)
        pkg_now = bool(verdict.get("package_visible"))
        pkg_note = ""
        if pkg_now and not pkg_was:
            self.r.set(pkg_key, json.dumps({
                "seen": datetime.now(timezone.utc).isoformat(),
                "summary": summary}), ex=86400)
            pkg_note = self._delivery_email_context()
            notable = True
            # Doorbell concierge: log every delivery, and add extra context when
            # the resident is AWAY (jarvis:away set by the geofence departure).
            self._log_package(camera, summary)
            if self._is_away():
                pkg_note += (" You're out — I've logged the delivery and I'm "
                             "keeping watch until you're back.")
        elif pkg_was and not pkg_now:
            self.r.delete(pkg_key)
            try:
                seen = json.loads(pkg_was).get("seen", "")[:16]
            except Exception:
                seen = ""
            await self._notify(
                f"📦 {camera}: the package (there since {seen}) is no longer "
                f"visible. If you didn't grab it, worth a look — {summary}",
                media_url=self._pin_event_snapshot(snap_b64))
            return f"Package-gone alert sent for {camera}."

        if not notable:
            return f"{camera}: {kind} assessed as not notable ({summary}). No alert."

        icon = "📦" if pkg_note else ("🔔" if kind == "ding" else "👁")
        msg = f"{icon} {camera}: {summary}"
        if detail:
            msg += f" {detail}"
        if pkg_note:
            msg += pkg_note
        await self._notify(msg, media_url=self._pin_event_snapshot(snap_b64))

        # Arrival greeting — spoken indoors (profile-gated, on by default).
        # The iPhone geofence is the AUTHORITATIVE resident-arrival signal
        # (main._on_presence greets + runs the scene). Camera-based resident
        # greetings are only the fallback when the geofence missed — never
        # while presence already says HOME, and always through the SAME
        # debounce key so the two paths can't both greet one arrival.
        announce = (verdict.get("announce") or "").strip()
        arrival = (verdict.get("arrival") or "").strip()
        try:
            profile = json.loads(self.r.get("user:profile") or "{}")
        except Exception:
            profile = {}
        greeted = False
        if announce and profile.get("sentry_greetings", True):
            if arrival == "resident":
                if not self._resident_home() and \
                        self.r.set("jarvis:arrival:debounce", "1", nx=True, ex=1800):
                    greeted = True
            else:   # guest/unknown at the door — always worth saying
                greeted = True
        if greeted:
            self._speak(announce, room=profile.get("sentry_greeting_room", "office"))

        # Arrival scene (Shortcut fallback path): geofence owns the resident
        # arrival scene now — only fire from camera vision when we actually
        # greeted a resident arrival here (geofence missed) in dark hours.
        scene = (profile.get("arrival_scene_shortcut") or "").strip()
        if scene and greeted and arrival == "resident" and self._is_dark_hours(profile):
            await self._run_shortcut(scene)

        return f"ALERT sent — {msg}"

    # ------------------------------------------------------------------

    def _pin_event_snapshot(self, snap_b64: str) -> str:
        """Pin the assessed frame under an event id and return its URL.

        The per-camera snapshot cache is overwritten by every new frame, so a
        card fetched later (push feed / app relaunch) would show the wrong
        moment. Pinning freezes THIS event's image for 48h.
        """
        if not GATEWAY_PUBLIC_URL:
            return ""
        import uuid as _uuid
        event_id = _uuid.uuid4().hex
        try:
            self.r.set(f"ring:snapshot:event:{event_id}", snap_b64, ex=172800)
        except Exception:
            return ""
        return (f"{GATEWAY_PUBLIC_URL}/ring/snapshot/event/{event_id}.jpg"
                f"?k={MOBILE_API_KEY}")

    def _resident_home(self) -> bool:
        return (self.r.get("user:presence:home") or "") == "1"

    async def _face_context(self, device: str) -> str:
        """Deterministic face-ID line for the prompt, from the vision service.

        The vision container processes the same ring-mqtt snapshot in parallel
        with this agent, so poll briefly for a verdict newer than ~2 minutes
        (ring:camera:{device}:face_id, written per snapshot). Degrades to
        'unavailable' if the service is down/still warming — the LLM then
        falls back to the resident_description hint alone.
        """
        import asyncio
        for attempt in range(4):
            raw = self.r.get(f"ring:camera:{device}:face_id")
            if raw:
                try:
                    fid = json.loads(raw)
                    age = (datetime.now(timezone.utc) -
                           datetime.fromisoformat(fid.get("ts", ""))
                           ).total_seconds()
                except Exception:
                    break
                if age <= 120:
                    if not fid.get("faces"):
                        return ("- Face recognition: no face discernible in "
                                "this frame (too far/turned away is common — "
                                "not evidence of a stranger).")
                    name = fid.get("name", "unknown")
                    score = fid.get("score", 0)
                    if name != "unknown":
                        return (f"- Face recognition (deterministic — TRUST "
                                f"this over visual guessing): {name}, an "
                                f"ENROLLED household member (similarity "
                                f"{score}).")
                    return (f"- Face recognition: {fid.get('faces')} face(s) "
                            f"detected, matching NO enrolled person (best "
                            f"similarity {score}).")
            await asyncio.sleep(1)
        return "- Face recognition: unavailable for this event."

    def _delivery_email_context(self) -> str:
        """Cross-reference Hermes's latest inbox triage for delivery mentions.

        Works automatically once IMAP creds are configured; silently empty
        until then.
        """
        try:
            raw = self.r.lrange("agent:email:reports", 0, 0)
            report = json.loads(raw[0]).get("report", "") if raw else ""
            lines = [l.strip() for l in report.splitlines()
                     if any(w in l.lower() for w in
                            ("deliver", "package", "tracking", "shipped", "arriv"))]
            if lines:
                return "\n↳ Likely related (from your email): " + "; ".join(lines[:2])
        except Exception:
            pass
        return ""

    def _is_dark_hours(self, profile: dict) -> bool:
        """True during the arrival-scene window (default 18:00–07:00 local)."""
        try:
            from zoneinfo import ZoneInfo
            hour = datetime.now(
                ZoneInfo(os.environ.get("USER_TZ", "America/Chicago"))).hour
        except Exception:
            hour = datetime.now().hour
        window = str(profile.get("arrival_scene_hours", "18-07"))
        try:
            start, end = (int(x) for x in window.split("-"))
        except Exception:
            start, end = 18, 7
        return hour >= start or hour < end if start > end else start <= hour < end

    async def _run_shortcut(self, name: str) -> None:
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(f"{MAC_BRIDGE_URL}/shortcut/run",
                             json={"name": name, "timeout": 30},
                             timeout=aiohttp.ClientTimeout(total=35))
            print(f"[Sentry] arrival scene shortcut '{name}' triggered")
        except Exception as exc:
            print(f"[Sentry] arrival scene failed: {exc}")

    def _speak(self, text: str, room: str = "office") -> None:
        try:
            import paho.mqtt.publish as mqtt_pub
            mqtt_pub.single(
                f"jarvis/tts/{room}/speak",
                json.dumps({"text": text, "room": room, "is_final": True}),
                hostname=MQTT_HOST, port=MQTT_PORT,
            )
        except Exception as exc:
            print(f"[Sentry] speak failed: {exc}")

    # ------------------------------------------------------------------

    async def _assess(self, snap_b64: str, camera: str, kind: str,
                      device: str = "") -> dict | None:
        try:
            profile = json.loads(self.r.get("user:profile") or "{}")
        except Exception:
            profile = {}
        desc = profile.get("resident_description", "")
        resident_hint = (
            f"- The RESIDENT looks like: {desc}."
            if desc else
            "- No resident description is on file — for arrivals use "
            "\"arrival\": \"unknown\"."
        )
        pets = profile.get("pets_description", "a dog (Finley) and house cats")
        indoor_hints = [str(h).lower() for h in
                        profile.get("indoor_cameras", _INDOOR_HINTS)]
        placement = ("INDOOR" if any(h in camera.lower() for h in indoor_hints)
                     else "OUTDOOR")
        presence = "HOME" if self._resident_home() else "AWAY"
        face_line = await self._face_context(device) if device else \
            "- Face recognition: unavailable for this event."
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
            resp = await client.messages.create(
                model=SENTRY_MODEL,
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/jpeg",
                            "data": snap_b64}},
                        {"type": "text",
                         "text": _ASSESS_PROMPT.format(
                             kind=kind, camera=camera, placement=placement,
                             presence=presence, pets=pets, face_line=face_line,
                             resident_hint=resident_hint)},
                    ],
                }],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            start, end = text.find("{"), text.rfind("}") + 1
            return json.loads(text[start:end]) if start >= 0 else None
        except Exception as exc:
            print(f"[Sentry] vision error: {exc}")
            return None

    def _is_away(self) -> bool:
        """True when the geofence says the resident has left (departure set it)."""
        try:
            return bool(self.r.exists("jarvis:away"))
        except Exception:
            return False

    def _log_package(self, camera: str, summary: str) -> None:
        """Append a delivery to the package log so 'any packages come?' works."""
        try:
            self.r.lpush("packages:log", json.dumps({
                "camera": camera,
                "summary": summary,
                "away": self._is_away(),
                "ts": datetime.now(timezone.utc).isoformat(),
            }))
            self.r.ltrim("packages:log", 0, 99)
        except Exception:
            pass

    async def _notify(self, text: str, media_url: str = "") -> None:
        self.log_event("tool", f"notify (push + iMessage): {text}")
        # iOS app push (works when the app is open; APNs later makes it always-on)
        try:
            import paho.mqtt.publish as mqtt_pub
            body = {"text": text, "title": "Sentry"}
            if media_url:
                body["media_url"] = media_url   # gateway renders an image card
            mqtt_pub.single(
                "jarvis/surfaces/iphone/push",
                json.dumps(body),
                hostname=MQTT_HOST, port=MQTT_PORT,
            )
        except Exception as exc:
            print(f"[Sentry] surface push failed: {exc}")

        # iMessage — the reliable channel until APNs exists
        try:
            profile = json.loads(self.r.get("user:profile") or "{}")
            phone = profile.get("imessage_to", "")
            if phone:
                script = (
                    f'tell application "Messages" to send {json.dumps(text)} '
                    f'to buddy "{phone}" of (service 1 whose service type is iMessage)'
                )
                async with aiohttp.ClientSession() as s:
                    await s.post(f"{MAC_BRIDGE_URL}/applescript",
                                 json={"script": script, "timeout": 20},
                                 timeout=aiohttp.ClientTimeout(total=25))
        except Exception as exc:
            print(f"[Sentry] iMessage failed: {exc}")
