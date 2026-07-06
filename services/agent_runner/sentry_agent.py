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

NOTABLE (worth interrupting the user): a person (describe them briefly), a
package/delivery, a vehicle in the driveway, a door/gate open that shouldn't
be, the dog (Finley) somewhere unusual or doing something destructive, smoke,
or anything genuinely unusual.
NOT notable: empty scene, shadows/light changes, plants moving, the dog
simply resting/walking normally, known furniture.

A doorbell ding is ALWAYS notable — describe who/what is at the door.

If a PERSON is arriving (walking toward the door / at the door / doorbell),
also write "announce": one short spoken line in the voice of JARVIS (the
Iron Man AI — British, warm, dry wit) announcing the arrival indoors.
{resident_hint}
If the person plausibly matches the resident's description, treat it as the
resident coming home: announce a warm personal welcome ("Welcome home, sir —
I trust the walk went well") and set "arrival": "resident". If clearly
someone else: describe them ("Sir, someone's approaching — tall gentleman,
navy jacket") and set "arrival": "guest". Unsure → "arrival": "unknown"
with a neutral announce. Empty announce if no one is arriving.

Return ONLY JSON: {{"notable": true/false, "summary": "one sentence of what
you see", "detail": "1-2 sentences with anything useful (appearance,
direction, what they're carrying)", "announce": "",
"package_visible": true/false}}
("package_visible": is a package/box/envelope sitting unattended in frame?)"""


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

        verdict = await self._assess(snap_b64, camera, kind)
        if verdict is None:
            return f"Sentry: vision assessment failed for {camera}."

        summary = verdict.get("summary", "").strip()
        detail = verdict.get("detail", "").strip()
        notable = bool(verdict.get("notable")) or kind == "ding"

        # Keep an assessed-event log the check_ring_camera tool can surface
        self.r.lpush("ring:events:assessed", json.dumps({
            "device": device, "camera": camera, "kind": kind,
            "notable": notable, "summary": summary,
            "ts": datetime.now(timezone.utc).isoformat(),
            "snapshot_ts": snap_ts,
        }))
        self.r.ltrim("ring:events:assessed", 0, 99)

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
        elif pkg_was and not pkg_now:
            self.r.delete(pkg_key)
            try:
                seen = json.loads(pkg_was).get("seen", "")[:16]
            except Exception:
                seen = ""
            await self._notify(
                f"📦 {camera}: the package (there since {seen}) is no longer "
                f"visible. If you didn't grab it, worth a look — {summary}",
                media_url=self._snapshot_url(device))
            return f"Package-gone alert sent for {camera}."

        if not notable:
            return f"{camera}: {kind} assessed as not notable ({summary}). No alert."

        icon = "📦" if pkg_note else ("🔔" if kind == "ding" else "👁")
        msg = f"{icon} {camera}: {summary}"
        if detail:
            msg += f" {detail}"
        if pkg_note:
            msg += pkg_note
        await self._notify(msg, media_url=self._snapshot_url(device))

        # Arrival greeting — spoken indoors (profile-gated, on by default)
        announce = (verdict.get("announce") or "").strip()
        try:
            profile = json.loads(self.r.get("user:profile") or "{}")
        except Exception:
            profile = {}
        if announce and profile.get("sentry_greetings", True):
            self._speak(announce, room=profile.get("sentry_greeting_room", "office"))

        # Arrival scene: run a HomeKit Shortcut on evening arrivals, e.g.
        # profile arrival_scene_shortcut = "Turn on living room"
        scene = (profile.get("arrival_scene_shortcut") or "").strip()
        if scene and announce and self._is_dark_hours(profile):
            await self._run_shortcut(scene)

        return f"ALERT sent — {msg}"

    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot_url(device: str) -> str:
        if not GATEWAY_PUBLIC_URL:
            return ""
        return f"{GATEWAY_PUBLIC_URL}/ring/snapshot/{device}.jpg?k={MOBILE_API_KEY}"

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

    async def _assess(self, snap_b64: str, camera: str, kind: str) -> dict | None:
        try:
            profile = json.loads(self.r.get("user:profile") or "{}")
        except Exception:
            profile = {}
        desc = profile.get("resident_description", "")
        resident_hint = (
            f"The RESIDENT looks like: {desc}. They often have a dog (Finley)."
            if desc else
            "No resident description is on file — use \"arrival\": \"unknown\"."
        )
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
                         "text": _ASSESS_PROMPT.format(kind=kind, camera=camera,
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

    async def _notify(self, text: str, media_url: str = "") -> None:
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
