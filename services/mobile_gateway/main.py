"""JARVIS Mobile Gateway Service.

Endpoints
---------
POST /ask/audio     — WAV/M4A audio → WAV reply  (Siri Shortcuts, backward-compat)
POST /ask/text      — JSON text     → WAV reply  (Siri Shortcuts, backward-compat)
POST /ask/query     — JSON text     → JSON {text, audio_b64, display}  (iOS native app)
POST /ask/query/audio — raw audio   → JSON {text, audio_b64, display}  (iOS native app)
POST /ask/image     — JSON {image_b64, text} → JSON  (iOS native app, glasses camera)
GET  /ws/glasses    — WebSocket: receives DisplayPayload pushes in real-time
GET  /health        — health check
POST /debug/audio   — echo metadata for debugging

Authentication
--------------
All endpoints require the ``X-API-Key`` header to match MOBILE_API_KEY in .env.
Leave MOBILE_API_KEY empty to disable auth (local-network-only deployments).
"""

import asyncio
import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt
import redis as _redis_lib
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from faster_whisper import WhisperModel
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# STT hallucination filter — faster-whisper emits these on silence/noise
# ---------------------------------------------------------------------------

_HALLUCINATION_PHRASES = {
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "like and subscribe",
    "subscribe to my channel",
    "thanks for listening",
    "thank you for listening",
    "don't forget to subscribe",
    "see you in the next video",
    "see you next time",
    "you",
    ".",
    "",
}

_REPEAT_PATTERN = re.compile(r"(.{10,}?)\1{2,}", re.DOTALL)


def _is_hallucination(text: str) -> bool:
    lower = text.lower().strip().rstrip(".,!?")
    if lower in _HALLUCINATION_PHRASES:
        return True
    if len(text.strip()) < 3:
        return True
    if _REPEAT_PATTERN.search(text):
        return True
    return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
PIPER_MODEL = os.environ.get("PIPER_MODEL", "en_GB-alan-medium")
LLM_TIMEOUT = float(os.environ.get("MOBILE_LLM_TIMEOUT", "45"))
API_KEY = os.environ.get("MOBILE_API_KEY", "")

_redis = _redis_lib.Redis(host=REDIS_HOST, decode_responses=True)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_pending_lock = threading.Lock()
pending_requests: dict[str, asyncio.Future] = {}
_event_loop: asyncio.AbstractEventLoop | None = None

# Connected WebSocket clients for push display payloads
_ws_clients: set[WebSocket] = set()
_ws_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------

print("[Gateway] Loading Whisper turbo model (CPU)...")
_stt_model = WhisperModel("turbo", device="cpu", compute_type="int8")
print("[Gateway] Whisper ready.")


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.wav") -> str:
    suffix = os.path.splitext(filename)[1] if filename else ".wav"
    if not suffix:
        suffix = ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    try:
        segments, _ = _stt_model.transcribe(tmp_path, language="en", vad_filter=True)
        text = " ".join(s.text for s in segments).strip()
        if _is_hallucination(text):
            print(f"[Gateway] STT hallucination filtered: '{text}'")
            return ""
        return text
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

# Same neural voice as the Mac's tts_mac service (one .env setting drives
# both surfaces). Piper stays as the offline fallback only — its robotic
# delivery is what the app used to sound like.
TTS_VOICE = os.environ.get("TTS_VOICE", "en-GB-RyanNeural")
TTS_RATE = os.environ.get("TTS_RATE", "-5%")
TTS_PITCH = os.environ.get("TTS_PITCH", "-5Hz")


async def _synthesize_edge(text: str) -> bytes:
    """edge-tts → mp3 → WAV (keeps the audio/wav contract for old clients).

    Must be awaited from the caller's loop — running asyncio.run() here from
    inside the endpoint's event loop was the bug that silently punted every
    request back to robotic Piper.
    """
    import edge_tts

    mp3_path = tempfile.mktemp(suffix=".mp3")
    wav_path = tempfile.mktemp(suffix=".wav")
    try:
        comm = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
        await comm.save(mp3_path)
        subprocess.run(
            ["ffmpeg", "-v", "quiet", "-y", "-i", mp3_path,
             "-ar", "22050", "-ac", "1", wav_path],
            check=True, timeout=30,
        )
        with open(wav_path, "rb") as f:
            return f.read()
    finally:
        for p in (mp3_path, wav_path):
            if os.path.exists(p):
                os.unlink(p)


# ElevenLabs premium tier (optional): ElevenLabs → edge-tts → piper.
# Quota/auth failures trip a 10-min cooldown (don't hammer a drained account).
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")
_el_down_until = 0.0


async def _synthesize_elevenlabs(text: str) -> Optional[bytes]:
    global _el_down_until
    import time as _time
    if not ELEVENLABS_API_KEY or _time.time() < _el_down_until:
        return None
    import aiohttp
    mp3_path = tempfile.mktemp(suffix=".mp3")
    wav_path = tempfile.mktemp(suffix=".wav")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
                json={"text": text, "model_id": ELEVENLABS_MODEL,
                      "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status in (401, 402, 429):
                    print(f"[Gateway] ElevenLabs quota/auth ({r.status}) — "
                          "edge-tts for the next 10 minutes")
                    _el_down_until = _time.time() + 600
                    return None
                if r.status != 200:
                    return None
                data = await r.read()
        with open(mp3_path, "wb") as f:
            f.write(data)
        subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-i", mp3_path,
                        "-ar", "22050", "-ac", "1", wav_path],
                       check=True, timeout=30)
        with open(wav_path, "rb") as f:
            return f.read()
    except Exception as e:
        print(f"[Gateway] ElevenLabs failed ({e}) — falling back")
        return None
    finally:
        for p in (mp3_path, wav_path):
            if os.path.exists(p):
                os.unlink(p)


async def synthesize_speech_async(text: str) -> bytes:
    el = await _synthesize_elevenlabs(text)
    if el:
        return el
    try:
        return await _synthesize_edge(text)
    except Exception as exc:
        print(f"[Gateway] edge-tts failed ({exc}) — falling back to piper")
    return synthesize_speech(text)


def synthesize_speech(text: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            ["piper", "--model", PIPER_MODEL, "--output_file", tmp_path],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError("TTS synthesis failed")
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Display payload parsing
#
# The LLM agent may prefix responses with structured display headers when the
# source is "glasses".  Format:
#
#   DISPLAY: short heading (≤4 words)
#   BODY: one-line HUD text (≤80 chars)
#   MEDIA_URL: https://...   (optional, for image/video type)
#   ---
#   Full spoken: The full sentence JARVIS will say aloud.
#
# AUDIO_ONLY: prefix means skip the HUD (music control, timers, etc.).
# If no prefix is found the whole response becomes the spoken text and
# the HUD shows the first sentence as title + rest as body.
# ---------------------------------------------------------------------------

@dataclass
class DisplayPayload:
    type: str          # "text" | "image" | "video" | "audio_only"
    title: str
    body: str
    media_url: Optional[str]
    tts_text: str


def parse_display_response(raw: str) -> DisplayPayload:
    """Extract structured display fields from an LLM response."""
    title = ""
    body = ""
    media_url = None
    tts_text = raw.strip()
    payload_type = "text"

    if raw.startswith("AUDIO_ONLY:"):
        tts_text = raw.split("AUDIO_ONLY:", 1)[1].strip()
        return DisplayPayload("audio_only", "", "", None, tts_text)

    if "DISPLAY:" in raw:
        # Extract DISPLAY / BODY / MEDIA_URL headers
        m_display = re.search(r"DISPLAY:\s*(.+)", raw)
        m_body = re.search(r"BODY:\s*(.+)", raw)
        m_media = re.search(r"MEDIA_URL:\s*(https?://\S+)", raw)

        title = m_display.group(1).strip() if m_display else ""
        body = m_body.group(1).strip() if m_body else ""
        media_url = m_media.group(1).strip() if m_media else None

        if media_url:
            lower = media_url.lower()
            payload_type = "video" if any(lower.endswith(ext) for ext in (".mp4", ".mov", ".m4v")) else "image"

        # Full spoken text is after the "---" separator
        if "---" in raw:
            after = raw.split("---", 1)[1]
            spoken_m = re.search(r"Full spoken:\s*(.+)", after, re.DOTALL)
            tts_text = spoken_m.group(1).strip() if spoken_m else after.strip()
        else:
            tts_text = raw

    else:
        # No structured headers — derive a short HUD heading from the first sentence
        sentences = re.split(r"(?<=[.!?])\s+", raw.strip())
        title = sentences[0][:60] if sentences else raw[:60]
        body = " ".join(sentences[1:])[:120] if len(sentences) > 1 else ""
        tts_text = raw

    return DisplayPayload(payload_type, title, body, media_url, tts_text)


async def _push_display_payload(payload: DisplayPayload) -> None:
    """Broadcast a DisplayPayload to all connected WebSocket clients."""
    if not _ws_clients:
        return
    data = json.dumps(asdict(payload))
    dead: set[WebSocket] = set()
    async with _ws_lock:
        for ws in _ws_clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        _ws_clients.difference_update(dead)


# ---------------------------------------------------------------------------
# MQTT client
# ---------------------------------------------------------------------------

_mqtt_client = mqtt.Client()


def _on_mqtt_connect(client, userdata, flags, rc):
    if rc != 0:
        print(f"[Gateway] MQTT connection failed (rc={rc})")
        return
    print(f"[Gateway] MQTT connected")
    client.subscribe("jarvis/tts/+/speak")
    client.subscribe("jarvis/surfaces/iphone/push")
    client.subscribe("jarvis/tools/event")


def _on_surface_push(client, userdata, msg):
    """Relay llm_agent surface fanout to all connected WebSocket clients.

    Optional ``media_url`` in the payload turns the push into an image card —
    used by Sentry to attach the camera snapshot to its alerts (renders on the
    app HUD view and, later, the glasses display).

    Every push is ALSO persisted to Redis (``surface:pushes``) so the app can
    fetch what it missed: iOS suspends the WebSocket in the background, and a
    push relayed to zero connected clients used to vanish — Sentry snapshot
    cards never appeared unless the app was open at that exact moment.
    """
    global _event_loop
    try:
        data = json.loads(msg.payload.decode())
        text = data.get("text", "")
        if not text:
            return
        title = data.get("title", "Jarvis")
        media_url = data.get("media_url") or None
        ptype = "text"
        if media_url:
            ptype = "video" if ".m3u8" in media_url else "image"
        try:
            _redis.lpush("surface:pushes", json.dumps({
                "id": uuid.uuid4().hex,
                "title": title, "text": text, "media_url": media_url,
                "type": ptype,
                "ts": datetime.now(timezone.utc).isoformat(),
            }))
            _redis.ltrim("surface:pushes", 0, 49)
        except Exception as exc:
            print(f"[Gateway] push persist failed: {exc}")
        payload = DisplayPayload(ptype, title, text[:200], media_url, text)
        if _event_loop is not None:
            asyncio.run_coroutine_threadsafe(_push_display_payload(payload), _event_loop)
        # Real push via APNs (no-op until the Apple Developer .p8 is configured).
        # We're on the MQTT callback thread here, so the sync sender is fine.
        try:
            import apns
            extra = {"media_url": media_url} if media_url else None
            n = apns.send_alert(_redis, title, text, payload_extra=extra)
            if n:
                print(f"[Gateway] APNs delivered to {n} device(s)")
        except Exception as exc:
            print(f"[Gateway] APNs error: {exc}")
    except Exception as exc:
        print(f"[Gateway] Surface push error: {exc}")


def _on_mqtt_message(client, userdata, msg):
    global _event_loop
    try:
        data = json.loads(msg.payload.decode())
        room = data.get("room", "")
        text = data.get("text", "")

        with _pending_lock:
            future = pending_requests.get(room)

        if future is not None and _event_loop is not None:
            _event_loop.call_soon_threadsafe(
                lambda f=future, t=text: f.set_result(t) if not f.done() else None
            )
    except Exception as exc:
        print(f"[Gateway] MQTT message error: {exc}")


# ---------------------------------------------------------------------------
# Live tool-call stream (SSE)
#
# The brain publishes each tool call/result to jarvis/tools/event. Surfaces
# subscribe here to render them as they happen instead of polling /tool-events
# and watching a spinner through a long multi-tool turn.
#
# Each SSE listener gets its own bounded Queue. paho delivers on its own thread,
# so the hand-off goes through the captured event loop.
# ---------------------------------------------------------------------------

_tool_stream_subscribers: set = set()
_TOOL_STREAM_QUEUE_MAX = 100


def _on_tool_event(client, userdata, msg):
    try:
        payload = msg.payload.decode()
    except Exception:
        return
    loop = _event_loop
    if loop is None:
        return  # MQTT can deliver before startup finished capturing the loop

    def _dispatch() -> None:
        for queue in list(_tool_stream_subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # One stalled reader must not block the others or grow without
                # bound. Drop it; the client reconnects and backfills from
                # /tool-events, which still holds the durable history.
                _tool_stream_subscribers.discard(queue)

    try:
        loop.call_soon_threadsafe(_dispatch)
    except RuntimeError:
        pass


_mqtt_client.on_connect = _on_mqtt_connect
_mqtt_client.on_message = _on_mqtt_message
_mqtt_client.message_callback_add("jarvis/surfaces/iphone/push", _on_surface_push)
_mqtt_client.message_callback_add("jarvis/tools/event", _on_tool_event)


def _surface_cleanup() -> None:
    """Periodically prune surfaces whose heartbeat TTL has expired."""
    while True:
        time.sleep(60)
        try:
            members = _redis.smembers("jarvis:active_surfaces")
            for sid in members:
                if not _redis.exists(f"jarvis:surface:{sid}:active"):
                    _redis.srem("jarvis:active_surfaces", sid)
                    print(f"[Gateway] Pruned stale surface '{sid}'")
        except Exception as exc:
            print(f"[Gateway] Surface cleanup error: {exc}")


threading.Thread(target=_surface_cleanup, daemon=True, name="surface-cleanup").start()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _event_loop
    _event_loop = asyncio.get_event_loop()
    try:
        _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except OSError as exc:
        raise RuntimeError(f"[Gateway] Cannot connect to MQTT: {exc}") from exc
    _mqtt_client.loop_start()
    print("[Gateway] JARVIS Mobile Gateway ready.")
    yield
    _mqtt_client.loop_stop()
    _mqtt_client.disconnect()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="JARVIS Mobile Gateway",
    description="Voice/text/image API for iPhone Siri Shortcut and native app",
    version="2.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _check_api_key(x_api_key: str) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# ---------------------------------------------------------------------------
# Core pipelines
# ---------------------------------------------------------------------------

async def _run_pipeline_text(text: str, source: str = "mobile") -> str:
    """Send text through the MQTT agent; return the raw response text."""
    response_text, _turn = await _run_pipeline_text_turn(text, source)
    return response_text


async def _run_pipeline_text_turn(text: str, source: str = "mobile") -> tuple[str, str]:
    """As _run_pipeline_text, but also returns the room, which is the turn id.

    The brain stamps every tool event of this request with the room, so handing
    it back lets a client attach that turn's tool calls to the exact message it
    just received — no guessing from timing.
    """
    request_id = uuid.uuid4().hex[:12]
    room = f"{source}-{request_id}"

    future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
    with _pending_lock:
        pending_requests[room] = future

    try:
        _mqtt_client.publish(
            "jarvis/llm/request",
            json.dumps({
                "text": text,
                "room": room,
                "verified": True,
                "source": source,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }),
        )
        reply = await asyncio.wait_for(asyncio.shield(future), timeout=LLM_TIMEOUT)
        return reply, room
    except asyncio.TimeoutError:
        future.cancel()
        raise HTTPException(status_code=504, detail="JARVIS did not respond in time.")
    finally:
        with _pending_lock:
            pending_requests.pop(room, None)


# Speech cleanup (mirrors tts_mac): strip markdown/emoji/decoration so the
# app's synthesized voice never says "asterisk asterisk" — DISPLAY text is
# untouched; this applies only to what gets synthesized.
_MD_SPEECH = re.compile(
    r"\*{1,3}(.+?)\*{1,3}|`{1,3}[^`]*`{1,3}|#{1,6}\s*"
    r"|\[([^\]]*)\]\([^)]*\)|>\s*|-{3,}|_{3,}|\*{3,}", re.DOTALL)
_EMOJI_SPEECH = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F]+")
_DECOR_SPEECH = re.compile(r"[─-╿▀-▟■-◿•‣⁃∙·✓✗✔✘✦✧▶►]")


def _strip_for_speech(text: str) -> str:
    text = _MD_SPEECH.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = _EMOJI_SPEECH.sub("", text)
    text = _DECOR_SPEECH.sub(" ", text)
    text = re.sub(r"[*_`~#>|]", "", text)
    return re.sub(r"\s+", " ", text).strip()


async def _run_pipeline_audio(text: str, source: str = "mobile") -> tuple[str, bytes]:
    """Send text through MQTT agent; return (response_text, wav_bytes)."""
    response_text = await _run_pipeline_text(text, source)
    wav_bytes = await synthesize_speech_async(_strip_for_speech(response_text))
    return response_text, wav_bytes


async def _run_pipeline_json(text: str, speak: bool = True) -> dict:
    """Run the full pipeline and return a JSON-friendly dict with display payload.

    Used by the native iOS app endpoints.  Sends source='glasses' so the LLM
    knows to format the response with DISPLAY:/BODY: headers.

    ``speak=False`` returns text only and skips synthesis entirely — the caller
    typed, so it has nothing to play.
    """
    turn_id = ""
    if not speak:
        response_text, turn_id = await _run_pipeline_text_turn(text, source="glasses")
        wav_bytes = b""
    else:
        response_text, wav_bytes = await _run_pipeline_audio(text, source="glasses")
    payload = parse_display_response(response_text)

    # Push display payload to any connected WebSocket clients immediately
    asyncio.create_task(_push_display_payload(payload))

    return {
        "text": payload.tts_text,
        "audio_b64": base64.b64encode(wav_bytes).decode(),
        # Lets the client attach this turn's tool calls to this exact message.
        "turn_id": turn_id,
        "display": {
            "type": payload.type,
            "title": payload.title,
            "body": payload.body,
            "media_url": payload.media_url,
        },
    }


# ---------------------------------------------------------------------------
# Endpoints — Siri Shortcuts (backward-compatible, unchanged behaviour)
# ---------------------------------------------------------------------------

@app.post(
    "/ask/audio",
    response_class=Response,
    summary="[Siri Shortcuts] Send voice audio → WAV reply",
)
async def ask_audio(request: Request, x_api_key: str = Header(default="")):
    """Primary Siri Shortcut endpoint — returns raw WAV bytes."""
    _check_api_key(x_api_key)

    content_type = request.headers.get("content-type", "")
    audio_bytes = b""
    filename = "audio.m4a"
    # Room mic-satellites (Pi Zero + ReSpeaker) use this same endpoint but
    # identify their physical room via header/form so replies route as that
    # room's voice surface instead of "mobile".
    source = request.headers.get("x-jarvis-room", "").strip() or "mobile"

    if "multipart/form-data" in content_type:
        form = await request.form()
        room_field = form.get("room")
        if room_field and not hasattr(room_field, "read"):
            source = str(room_field).strip() or source
        audio_field = form.get("audio")
        if audio_field is None:
            for v in form.values():
                audio_field = v
                break
        if audio_field is None:
            raise HTTPException(status_code=400, detail="No audio field in form data")
        if hasattr(audio_field, "read"):
            audio_bytes = await audio_field.read()
            filename = getattr(audio_field, "filename", None) or "audio.m4a"
        else:
            try:
                audio_bytes = base64.b64decode(str(audio_field))
            except Exception:
                audio_bytes = str(audio_field).encode("latin-1")
    else:
        audio_bytes = await request.body()

    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio received")

    # Auto-detect base64-encoded body
    if len(audio_bytes) > 10 and all(0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D) for b in audio_bytes[:200]):
        try:
            decoded = base64.b64decode(audio_bytes.strip())
            if len(decoded) > 100:
                audio_bytes = decoded
        except Exception:
            pass

    text = transcribe_audio(audio_bytes, filename)
    if not text:
        raise HTTPException(status_code=422, detail="No speech detected in audio")

    print(f"[Gateway] /ask/audio transcribed ({source}): '{text}'")
    _, wav_bytes = await _run_pipeline_audio(text, source=source)
    return Response(content=wav_bytes, media_type="audio/wav")


class TextRequest(BaseModel):
    text: str


@app.post(
    "/ask/text",
    response_class=Response,
    summary="[Siri Shortcuts] Send text → WAV reply",
)
async def ask_text(request: TextRequest, x_api_key: str = Header(default="")):
    """Alternative Siri Shortcut endpoint — returns raw WAV bytes."""
    _check_api_key(x_api_key)
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")
    print(f"[Gateway] /ask/text: '{request.text}'")
    _, wav_bytes = await _run_pipeline_audio(request.text, source="mobile")
    return Response(content=wav_bytes, media_type="audio/wav")


# ---------------------------------------------------------------------------
# Endpoints — iOS native app (return JSON with display payload)
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    text: str
    source: str = "glasses"
    # Modality matching: a TYPED message gets a typed reply. Skipping synthesis
    # is not just a UX choice — it avoids paying ElevenLabs per character and
    # removes TTS from the response latency for text turns.
    speak: bool = True


@app.post("/ask/query", summary="[iOS app] Text query → JSON with display payload")
async def ask_query(request: QueryRequest, x_api_key: str = Header(default="")):
    """
    Used by the native iOS Jarvis app for text and typed chat queries.
    Returns structured JSON so the app can update the HUD and play TTS directly.
    ``speak: false`` (typed chat) returns text only and skips synthesis.
    """
    _check_api_key(x_api_key)
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")
    print(f"[Gateway] /ask/query: '{request.text}' (speak={request.speak})")
    return await _run_pipeline_json(request.text, speak=request.speak)


@app.post("/ask/query/siri", summary="[Siri App Intent] Text query → plain text, no audio synthesis")
async def ask_query_siri(request: QueryRequest, x_api_key: str = Header(default="")):
    """Fast path for the AskJarvisIntent: Siri speaks the dialog itself, so
    skipping server-side WAV synthesis cuts seconds off the round-trip.

    source="siri" → room "siri-…" so the brain treats this as a VOICE surface
    (2-3 spoken sentences, no markdown/lists). Long dialogs are the reason
    Siri silently SHOWS answers instead of speaking them.
    """
    _check_api_key(x_api_key)
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")
    print(f"[Gateway] /ask/query/siri: '{request.text}'")
    response_text = await _run_pipeline_text(request.text, source="siri")
    payload = parse_display_response(response_text)
    text = payload.tts_text.strip()

    # Belt-and-braces: if the model still rambles, trim at a sentence boundary —
    # past ~600 chars Siri reliably stops speaking and just displays the text.
    if len(text) > 600:
        cut = text[:600]
        for stop in (". ", "! ", "? "):
            idx = cut.rfind(stop)
            if idx > 200:
                cut = cut[: idx + 1]
                break
        text = cut + " Full details are in the Jarvis app, sir."
    return {"text": text}


@app.post("/ask/query/audio", summary="[iOS app] Audio bytes → JSON with display payload")
async def ask_query_audio(request: Request, x_api_key: str = Header(default="")):
    """
    Used by the native iOS app for voice queries (PTT mic button or glasses tap).
    Accepts raw WAV bytes in the request body.
    """
    _check_api_key(x_api_key)
    audio_bytes = await request.body()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio body")

    text = transcribe_audio(audio_bytes, "audio.wav")
    if not text:
        raise HTTPException(status_code=422, detail="No speech detected in audio")

    print(f"[Gateway] /ask/query/audio transcribed: '{text}'")
    return await _run_pipeline_json(text)


class ImageQueryRequest(BaseModel):
    image_b64: str
    text: str = "What am I looking at?"
    source: str = "glasses"


@app.post("/ask/image", summary="[iOS app] Glasses photo + text → JSON with display payload")
async def ask_image(request: ImageQueryRequest, x_api_key: str = Header(default="")):
    """
    Accepts a base64-encoded JPEG from the glasses camera and an optional text
    prompt.  The image is injected as a data URI into the LLM request so the
    vision pipeline can describe what the glasses see.
    """
    _check_api_key(x_api_key)
    if not request.image_b64:
        raise HTTPException(status_code=400, detail="image_b64 must not be empty")
    # Beyond the bus ceiling the broker drops the publisher and the reply never
    # comes, so the request would otherwise burn the full 45s timeout and report
    # a misleading "JARVIS did not respond in time". Say what actually happened.
    if len(request.image_b64) > MAX_IMAGE_B64:
        raise HTTPException(
            status_code=413,
            detail=(f"Image too large ({len(request.image_b64) // 1024}KB encoded); "
                    f"limit is {MAX_IMAGE_B64 // 1024}KB. Downscale before sending."))

    # Wrap image as a data URI so the LLM agent's vision tool can receive it.
    # Sniff the real format — the API rejects a mislabelled image outright.
    data_uri = f"data:{_image_media_type(request.image_b64)};base64,{request.image_b64}"
    composite_text = f"{_stash_image(data_uri)}\n{request.text}"

    print(f"[Gateway] /ask/image: prompt='{request.text}', image_len={len(request.image_b64)}")
    return await _run_pipeline_json(composite_text)


# Images used to travel to the brain INLINE in the request text, as
# [GLASSES_CAMERA_IMAGE: data:image/jpeg;base64,…]. That put megabytes on the
# MQTT bus, and mosquitto rejects oversize packets — it drops the publisher, no
# reply can arrive, and the gateway burns its full timeout before reporting
# "JARVIS did not respond in time". Raising message_size_limit did NOT lift the
# ceiling; a 2.5MB photo was still refused with the limit set to 25MB.
#
# So the payload no longer goes on the bus at all. The bytes go into Redis —
# which both services already share — and the message carries only a short key.
# Size stops being a bus concern entirely, and video frame-sampling (up to 6
# images in one request) stops being hopeless.
IMAGE_REF_TTL_S = int(os.environ.get("IMAGE_REF_TTL_S", "600"))


def _stash_image(data_uri: str) -> str:
    """Park an image in Redis and return the marker to send in its place.

    Falls back to the inline marker if Redis is unavailable, so behaviour is no
    worse than before rather than failing outright.
    """
    try:
        key = f"jarvis:imgref:{uuid.uuid4().hex[:16]}"
        _redis.setex(key, IMAGE_REF_TTL_S, data_uri)
        return f"[GLASSES_CAMERA_IMAGE_REF: {key}]"
    except Exception as exc:
        print(f"[Gateway] image stash failed ({exc}) — falling back to inline")
        return f"[GLASSES_CAMERA_IMAGE: {data_uri}]"


# Ceiling for an image, set from measurement against the live stack now that
# images travel by reference and the bus is no longer the constraint:
#
#     2.6MB base64 -> 200 in 4.7s
#     4.8MB base64 -> the brain answered correctly, but took longer than
#                     LLM_TIMEOUT, so the caller had already given up
#
# The old 5MB ceiling therefore admitted sizes that reliably produced a 45s
# "JARVIS did not respond in time" — a timeout that describes nothing and hides
# a request that actually succeeded. 3MB sits inside what completes comfortably,
# so anything larger is refused immediately with a reason instead.
#
# Clients downscale to 1568px before sending (a few hundred KB), so in practice
# this only catches callers that do not.
MAX_IMAGE_B64 = int(os.environ.get("MAX_IMAGE_B64", str(3 * 1024 * 1024)))


def _image_media_type(b64: str, default: str = "image/jpeg") -> str:
    """Identify an image's real format from its leading bytes.

    This used to be hardcoded to image/jpeg, which is fine for the glasses and
    for ffmpeg-sampled video frames but wrong for anything the user picks out
    of their photo library — screenshots are PNG. Anthropic validates the
    declared media type against the actual bytes and rejects a mismatch with
    "the image appears to be a image/png image", which surfaced to the user as
    a generic "I encountered an error processing that request."

    Only the first few bytes are decoded, so this stays cheap on a large photo.
    """
    try:
        head = base64.b64decode(b64[:32], validate=False)
    except Exception:
        return default
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return default


class VideoQueryRequest(BaseModel):
    video_b64: str
    text: str = "What is happening in this video?"
    source: str = "glasses"
    # Recording a clip means eyes are on the screen — default to text.
    speak: bool = False


# Vertex tokens live ~1h; cache so a burst of clips doesn't re-auth each time.
_GOOGLE_TOKEN: dict = {"token": "", "exp": 0.0}


async def _google_access_token() -> str:
    """OAuth token for Vertex AI, or "" if no Google credentials are reachable.

    Two sources, in order: the GCE metadata server (the gateway runs on the
    GCP VM, so this needs no key files at all), then Application Default
    Credentials for off-GCP runs. Never raises — callers treat "" as
    "Vertex unavailable" and fall back.
    """
    if _GOOGLE_TOKEN["token"] and time.time() < _GOOGLE_TOKEN["exp"]:
        return _GOOGLE_TOKEN["token"]

    def _store(token: str, ttl: float) -> str:
        _GOOGLE_TOKEN.update(token=token, exp=time.time() + max(0.0, ttl - 60))
        return token

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/"
                "service-accounts/default/token",
                headers={"Metadata-Flavor": "Google"},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return _store(data["access_token"],
                                  float(data.get("expires_in", 3600)))
    except Exception:
        pass  # not on GCE (Mac edge, laptop, a friend's box) — try ADC

    try:
        import google.auth
        from google.auth.transport.requests import Request as _GoogleRequest

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        await asyncio.get_running_loop().run_in_executor(
            None, creds.refresh, _GoogleRequest())
        expiry = getattr(creds, "expiry", None)
        ttl = 3600.0
        if expiry is not None:
            from datetime import datetime, timezone
            ref = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
            ttl = max(0.0, (ref - datetime.now(timezone.utc)).total_seconds())
        return _store(creds.token or "", ttl)
    except Exception:
        return ""


async def _analyze_video_gemini(video_b64: str, question: str) -> Optional[str]:
    """True video-native analysis via Gemini (motion, temporal order, audio).

    Claude only sees sampled frames; Gemini ingests the actual clip. Used when
    a Google backend is reachable and the clip is small enough for inline
    upload (~<14MB base64); returns None on any failure so the caller falls
    back to the frame-sampling path. Model override: GEMINI_VIDEO_MODEL.

    Backends are tried in order:
      1. Vertex AI  — billed to the GCP project, so it draws on ordinary GCP
         credit. Auth is the VM's own service account (no key in .env).
      2. AI Studio  — GOOGLE_API_KEY. Billed against a *separate* AI Studio
         prepay balance that GCP credits do NOT fund, so it is the fallback.
    """
    if len(video_b64) > 14_000_000:
        return None

    model = os.environ.get("GEMINI_VIDEO_MODEL", "gemini-3.6-flash").strip()
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "video/mp4", "data": video_b64}},
                {"text": question},
            ],
        }]
    }

    attempts: list = []
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if project:
        location = os.environ.get("VERTEX_LOCATION", "global").strip() or "global"
        host = ("aiplatform.googleapis.com" if location == "global"
                else f"{location}-aiplatform.googleapis.com")
        attempts.append((
            "vertex",
            f"https://{host}/v1/projects/{project}/locations/{location}"
            f"/publishers/google/models/{model}:generateContent",
        ))
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if api_key:
        attempts.append((
            "aistudio",
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}",
        ))
    if not attempts:
        return None

    import aiohttp
    for backend, url in attempts:
        headers = {}
        if backend == "vertex":
            token = await _google_access_token()
            if not token:
                print("[Gateway] Vertex: no Google credentials — skipping")
                continue
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        url, json=body, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=90)) as resp:
                    if resp.status != 200:
                        detail = (await resp.text())[:200].replace("\n", " ")
                        print(f"[Gateway] Gemini video via {backend} failed "
                              f"({resp.status}): {detail}")
                        continue
                    data = await resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if text:
                print(f"[Gateway] Gemini video analyzed via {backend} ({model})")
                return text
        except Exception as e:
            print(f"[Gateway] Gemini video error via {backend} ({e})")
    print("[Gateway] Gemini unavailable — falling back to frame sampling")
    return None


@app.post("/ask/video", summary="[iOS app/glasses] Short video clip + text → JSON with display payload")
async def ask_video(request: VideoQueryRequest, x_api_key: str = Header(default="")):
    """Analyze a short video clip (fridge scan, pantry pan, 'what's going on here').

    Claude doesn't ingest video directly, so we sample up to 6 evenly spaced
    frames with ffmpeg (already in this image for audio work), downscale them,
    and send them as multiple image blocks — the llm_agent converts the
    markers into a single multimodal message so the model sees the sequence.
    Keep clips short (~5-20s); frames are sampled across the full duration.
    """
    import subprocess
    import tempfile
    import glob as _glob
    import shutil

    _check_api_key(x_api_key)
    if not request.video_b64:
        raise HTTPException(status_code=400, detail="video_b64 must not be empty")

    # Preferred path: video-native Gemini (sees motion/order/audio, not just
    # frames). The description is then routed through the normal pipeline so
    # Jarvis answers in-character with full context/memory.
    gemini_desc = await _analyze_video_gemini(request.video_b64, request.text)
    if gemini_desc:
        print(f"[Gateway] /ask/video: Gemini analyzed the clip natively")
        composite = (
            f"(A video the user just recorded was analyzed; here is what it shows: "
            f"{gemini_desc})\n\nThe user asked: {request.text}"
        )
        return await _run_pipeline_json(composite, speak=request.speak)

    tmpdir = tempfile.mkdtemp(prefix="jarvis_vid_")
    try:
        video_path = os.path.join(tmpdir, "clip.mp4")
        with open(video_path, "wb") as f:
            f.write(base64.b64decode(request.video_b64))

        # Duration → sample 6 frames evenly across the clip
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30,
        )
        try:
            duration = max(float(probe.stdout.strip()), 0.5)
        except ValueError:
            raise HTTPException(status_code=422, detail="Could not read the video (bad encoding?)")

        n_frames = 6
        fps = n_frames / duration
        subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", video_path,
             "-vf", f"fps={fps:.4f},scale='min(896,iw)':-2",
             "-frames:v", str(n_frames), "-q:v", "6",
             os.path.join(tmpdir, "frame_%02d.jpg")],
            timeout=120, check=True,
        )

        frames = sorted(_glob.glob(os.path.join(tmpdir, "frame_*.jpg")))
        if not frames:
            raise HTTPException(status_code=422, detail="No frames could be extracted")

        markers = []
        for fp in frames:
            with open(fp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            markers.append(_stash_image(f"data:image/jpeg;base64,{b64}"))

        composite_text = (
            "\n".join(markers)
            + f"\nThese are {len(frames)} frames sampled in order from a "
              f"{duration:.0f}-second video. {request.text}"
        )
        print(f"[Gateway] /ask/video: prompt='{request.text}', "
              f"{duration:.1f}s clip → {len(frames)} frames")
        return await _run_pipeline_json(composite_text)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Video processing timed out")
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=422, detail="ffmpeg could not decode the video")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# WebSocket — real-time display pushes to the iOS app
# ---------------------------------------------------------------------------

@app.websocket("/ws/glasses")
async def ws_glasses(websocket: WebSocket, x_api_key: str = Header(default="")):
    """
    The iOS app connects here on launch.  Whenever the LLM produces a
    response originating from a glasses/mobile request, a DisplayPayload
    JSON object is pushed to all connected clients so the HUD can update
    independently of the HTTP response cycle.
    """
    if API_KEY and x_api_key != API_KEY:
        await websocket.close(code=1008, reason="Invalid API key")
        return

    await websocket.accept()
    async with _ws_lock:
        _ws_clients.add(websocket)
    print(f"[Gateway] WebSocket client connected ({len(_ws_clients)} total)")

    try:
        while True:
            # Keep the connection alive; actual data is pushed from _push_display_payload
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with _ws_lock:
            _ws_clients.discard(websocket)
        print(f"[Gateway] WebSocket client disconnected ({len(_ws_clients)} remaining)")


# ---------------------------------------------------------------------------
# Debug / health
# ---------------------------------------------------------------------------

@app.post("/debug/audio", summary="Echo audio metadata for debugging")
async def debug_audio(request: Request):
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    first_hex = body[:60].hex() if body else ""
    is_b64 = bool(body) and all(0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D) for b in body[:200])
    decoded_size = None
    if is_b64:
        try:
            decoded_size = len(base64.b64decode(body.strip()))
        except Exception:
            decoded_size = -1
    return {
        "total_bytes": len(body),
        "content_type": content_type,
        "first_60_hex": first_hex,
        "looks_like_base64": is_b64,
        "base64_decoded_size": decoded_size,
    }


@app.get("/health", summary="Health check")
async def health():
    return {
        "status": "ok",
        "service": "jarvis-mobile-gateway",
        "ws_clients": len(_ws_clients),
    }


# ---------------------------------------------------------------------------
# Presence / surface registry
# ---------------------------------------------------------------------------

class HeartbeatRequest(BaseModel):
    surface_id: str
    type: str = "unknown"
    capabilities: list[str] = []


@app.post("/presence/heartbeat", summary="Surface heartbeat — register or renew presence")
async def presence_heartbeat(req: HeartbeatRequest, x_api_key: str = Header(default="")):
    """
    Called every 15 s by each active surface (iPhone, Mac).
    Stores surface metadata in Redis with a 30-second TTL; the surface is
    considered offline if a heartbeat is missed for two intervals.
    """
    _check_api_key(x_api_key)
    now = datetime.now(timezone.utc).isoformat()
    try:
        _redis.setex(f"jarvis:surface:{req.surface_id}:active", 30, "1")
        _redis.set(f"jarvis:surface:{req.surface_id}:meta", json.dumps({
            "type": req.type,
            "capabilities": req.capabilities,
            "last_seen": now,
        }))
        _redis.sadd("jarvis:active_surfaces", req.surface_id)
        active = sorted(_redis.smembers("jarvis:active_surfaces"))
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")
    return {"ok": True, "surface_id": req.surface_id, "active_surfaces": active}


@app.get("/presence/surfaces", summary="List all currently active surfaces")
async def list_surfaces(x_api_key: str = Header(default="")):
    _check_api_key(x_api_key)
    try:
        members = sorted(_redis.smembers("jarvis:active_surfaces"))
        surfaces = []
        for sid in members:
            meta_raw = _redis.get(f"jarvis:surface:{sid}:meta")
            active = bool(_redis.exists(f"jarvis:surface:{sid}:active"))
            if meta_raw:
                meta = json.loads(meta_raw)
                meta["surface_id"] = sid
                meta["active"] = active
                surfaces.append(meta)
        return {"surfaces": surfaces}
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")


# ---------------------------------------------------------------------------
# HealthKit ingestion (Month 4a)
# ---------------------------------------------------------------------------

class HealthSnapshotRequest(BaseModel):
    steps: Optional[float] = None
    active_energy_kcal: Optional[float] = None
    resting_heart_rate: Optional[float] = None
    hrv_ms: Optional[float] = None
    sleep_hours: Optional[float] = None
    body_mass_lbs: Optional[float] = None
    workouts_today: Optional[int] = None
    workout_minutes_today: Optional[float] = None
    # Set true when the app detects the user just woke (HealthKit sleep analysis
    # 'awake' transition, or first unlock after sleep). Authoritative wake signal
    # for the morning brief — Sentry fires the brief at once instead of waiting
    # for sustained camera presence.
    just_woke: Optional[bool] = None
    source: str = "ios_healthkit"


def _compute_readiness(snapshot: dict, history: list[dict]) -> dict:
    """Fuse HealthKit signals into ONE readiness score (0-100) that sets the
    day's tone — Apollo's volume, Kai's intensity ranking, the morning brief.

    Baselines come from the user's own recent history (median), so the score is
    personal, not absolute. Missing signals are simply skipped.
    """
    import statistics as _stats

    def _med(key):
        vals = [h.get(key) for h in history[1:15] if h.get(key) is not None]
        return _stats.median(vals) if vals else None

    score = 100.0
    factors: list[str] = []

    hrv, hrv_base = snapshot.get("hrv_ms"), _med("hrv_ms")
    if hrv is not None and hrv_base:
        delta = (hrv - hrv_base) / hrv_base
        if delta < 0:
            pen = min(30.0, abs(delta) * 100)
            score -= pen
            factors.append(f"HRV {round(hrv)}ms below ~{round(hrv_base)}ms baseline")
        else:
            factors.append(f"HRV {round(hrv)}ms at/above baseline")

    rhr, rhr_base = snapshot.get("resting_heart_rate"), _med("resting_heart_rate")
    if rhr is not None and rhr_base:
        delta = (rhr - rhr_base) / rhr_base
        if delta > 0:
            score -= min(25.0, delta * 250)
            factors.append(f"resting HR {round(rhr)} above ~{round(rhr_base)}")

    sleep = snapshot.get("sleep_hours")
    if sleep is not None:
        if sleep < 7:
            score -= min(30.0, (7 - sleep) * 8)
            factors.append(f"{sleep:g}h sleep")
        elif sleep >= 7.5:
            factors.append(f"{sleep:g}h sleep")

    # Yesterday's training load (from the previous day's snapshot) → fatigue.
    prev_load = history[1].get("workout_minutes_today") if len(history) > 1 else None
    if prev_load and prev_load > 90:
        score -= 10
        factors.append(f"{int(prev_load)}min training yesterday")

    score = max(0, min(100, round(score)))
    if score >= 75:
        band = "primed"
    elif score >= 55:
        band = "good"
    elif score >= 40:
        band = "moderate strain"
    else:
        band = "prioritise recovery"

    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "score": score,
        "band": band,
        "factors": factors[:4],
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/health/snapshot", summary="[iOS app] Push a HealthKit fitness snapshot")
async def health_snapshot(req: HealthSnapshotRequest, x_api_key: str = Header(default="")):
    """Store the latest HealthKit snapshot in Redis.

    - ``user:health:latest`` — most recent snapshot (always fresh; consumed by
      grocery + ambient agents)
    - ``user:health:history`` — ONE snapshot per calendar day (~30 days). The
      iOS app pushes on launch + every foreground, so a naive append floods the
      list with same-day snapshots and collapses the recovery baseline that Kai
      (ClassPass) and Apollo (workout) compute from it. We de-dup by day: the
      newest same-day entry is replaced, distinct days are prepended.
    - If body mass is present, ``user:profile.weight_lbs`` is updated so the
      grocery agent's TDEE calculation always uses the user's current weight.
    """
    _check_api_key(x_api_key)
    snapshot = {k: v for k, v in req.model_dump().items() if v is not None}
    now = datetime.now(timezone.utc)
    snapshot["ts"] = now.timestamp()
    today = now.strftime("%Y-%m-%d")

    try:
        _redis.set("user:health:latest", json.dumps(snapshot))

        # De-dup history by day: replace the head if it's from today, else prepend.
        head = _redis.lindex("user:health:history", 0)
        head_day = ""
        if head:
            try:
                head_ts = json.loads(head).get("ts")
                if head_ts:
                    head_day = datetime.fromtimestamp(
                        head_ts, timezone.utc
                    ).strftime("%Y-%m-%d")
            except Exception:
                pass
        if head_day == today:
            _redis.lset("user:health:history", 0, json.dumps(snapshot))
        else:
            _redis.lpush("user:health:history", json.dumps(snapshot))
        _redis.ltrim("user:health:history", 0, 29)   # ~30 distinct days

        # Authoritative wake signal → Sentry fires the morning brief immediately
        # (expires end of day so it can't linger). Keyed off the snapshot request.
        if req.just_woke:
            _redis.set("user:awake:today", now.isoformat(), ex=18 * 3600)

        # Fuse into a single readiness score for the day (sets Apollo/Kai/brief tone).
        try:
            hist = [json.loads(h) for h in _redis.lrange("user:health:history", 0, 14)]
            _redis.set("user:readiness:today",
                       json.dumps(_compute_readiness(snapshot, hist)))
        except Exception:
            pass

        # Keep the user profile's weight in sync with HealthKit body mass
        if req.body_mass_lbs:
            raw = _redis.get("user:profile") or "{}"
            try:
                profile = json.loads(raw)
            except Exception:
                profile = {}
            profile["weight_lbs"] = round(float(req.body_mass_lbs), 1)
            _redis.set("user:profile", json.dumps(profile))
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")

    return {"ok": True, "stored": list(snapshot.keys())}


# ---------------------------------------------------------------------------
# Generic webhook ingress — let external services (IFTTT, GitHub, Stripe,
# calendar/webhook providers, home automations) push events onto the JARVIS bus.
# Routed through jarvis/notify so Synapse's router decides urgent-vs-digest.
# Auth: WEBHOOK_SECRET (separate from the app's MOBILE_API_KEY) via ?secret= or
# the X-Webhook-Secret header. Leave WEBHOOK_SECRET empty to disable this route.
# ---------------------------------------------------------------------------
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


class NotifyFeedbackRequest(BaseModel):
    key: str            # the notification's dedup_key (e.g. "synapse:protein_gap")
    action: str         # "dismiss" | "act"


@app.post("/notify/feedback", summary="[iOS app] Notification acted-on/dismissed → learn")
async def notify_feedback(req: NotifyFeedbackRequest, x_api_key: str = Header(default="")):
    """Record whether the user acted on or dismissed a proactive notification.

    Synapse reads these counts and suppresses rules the user keeps dismissing, so
    the proactive layer gets quieter about things you don't care about.
    """
    _check_api_key(x_api_key)
    key = (req.key or "").strip()
    act = (req.action or "").strip().lower()
    if not key or act not in ("dismiss", "act"):
        raise HTTPException(400, "key and action (dismiss|act) required")
    try:
        _redis.hincrby(f"synapse:feedback:{key}", act, 1)
        _redis.expire(f"synapse:feedback:{key}", 60 * 60 * 24 * 60)
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")
    return {"ok": True}


@app.post("/webhook/{source}", summary="Inbound webhook → JARVIS notification bus")
async def inbound_webhook(source: str, request: Request, secret: str = "",
                          x_webhook_secret: str = Header(default="")):
    if not WEBHOOK_SECRET:
        raise HTTPException(404, "Webhook ingress disabled (set WEBHOOK_SECRET).")
    if secret != WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(401, "Invalid webhook secret.")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {"value": body}
    except Exception:
        body = {"raw": (await request.body()).decode("utf-8", "ignore")[:500]}

    title = str(body.get("title") or f"🔔 {source}")[:80]
    text = str(body.get("text") or body.get("message") or json.dumps(body))[:500]
    urgency = str(body.get("urgency") or "normal").lower()
    try:
        _mqtt_client.publish("jarvis/notify", json.dumps({
            "title": title, "text": text, "urgency": urgency, "source": source,
        }))
    except Exception as exc:
        raise HTTPException(503, f"Bus publish failed: {exc}")
    return {"ok": True, "routed": source, "urgency": urgency}


# ---------------------------------------------------------------------------
# Calendar ingestion (Month 4c) — feeds the ambient agent's countdown trigger
# ---------------------------------------------------------------------------

class NextCalendarEventRequest(BaseModel):
    title: str
    start: str                       # ISO-8601
    location: Optional[str] = None


@app.post("/calendar/next-event", summary="[iOS app] Push the next upcoming calendar event")
async def calendar_next_event(req: NextCalendarEventRequest, x_api_key: str = Header(default="")):
    """Store the next upcoming event so the ambient agent can warn before it starts.

    Written to ``jarvis:calendar:next_event`` — the exact key AmbientAgent reads.
    """
    _check_api_key(x_api_key)
    try:
        _redis.set("jarvis:calendar:next_event", json.dumps({
            "title": req.title,
            "start": req.start,
            "location": req.location or "",
        }))
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")
    return {"ok": True}


@app.get("/stream/tools", summary="[iOS app] Live tool-call stream (SSE)")
async def stream_tools(request: Request, x_api_key: str = Header(default="")):
    """Server-sent events: one JSON tool event per message, as it happens.

    Events carry ``call_id`` (pairs a "calling" with its "done") and ``turn_id``
    (groups a turn's calls under the message that caused them), so a client can
    render them inline rather than as a flat log.

    This is a live tail, not history — a client that reconnects should backfill
    from /tool-events, which keeps the durable copy.
    """
    _check_api_key(x_api_key)
    queue: asyncio.Queue = asyncio.Queue(maxsize=_TOOL_STREAM_QUEUE_MAX)
    _tool_stream_subscribers.add(queue)

    async def _events():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    # Idle comment frame: keeps proxies and iOS from tearing
                    # down a quiet connection.
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {payload}\n\n"
        finally:
            _tool_stream_subscribers.discard(queue)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # don't let a proxy buffer the stream
        },
    )


@app.get("/tool-events", summary="Recent tool call events for iOS timeline")
async def get_tool_events(x_api_key: str = Header(default=""), limit: int = 30,
                          turn_id: str = ""):
    """Recent tool events, newest first.

    ``turn_id`` narrows to a single request, which is how a client attaches a
    turn's tool calls to the message it just received. That backfill is what
    makes inline rendering correct even if the live SSE stream dropped, was
    never connected, or missed events emitted before the client knew the id.
    """
    _check_api_key(x_api_key)
    try:
        # A turn's events can sit behind newer ones, so scan deeper when
        # filtering — the stored list is capped at 100 anyway.
        depth = 100 if turn_id else min(limit, 50)
        raw = _redis.lrange("jarvis:tool_events", 0, depth - 1)
        events = []
        for item in raw:
            try:
                event = json.loads(item)
            except Exception:
                continue
            if turn_id and event.get("turn_id") != turn_id:
                continue
            events.append(event)
        if not turn_id:
            events = events[:limit]
        return {"events": events}
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")


@app.get("/ring/snapshot/{device}.jpg", summary="Latest cached Ring snapshot as a JPEG")
async def ring_snapshot(device: str, k: str = ""):
    """Serve a Ring camera's most recent snapshot (cached in Redis by the
    agent_runner). Auth via ?k=<MOBILE_API_KEY> query param because this URL
    is fetched directly by image views (no header support)."""
    expected = os.environ.get("MOBILE_API_KEY", "")
    if expected and k != expected:
        raise HTTPException(status_code=401, detail="bad key")
    snap = _redis.get(f"ring:camera:{device}:snapshot")
    if not snap:
        raise HTTPException(status_code=404, detail="no snapshot cached for this camera")
    return Response(content=base64.b64decode(snap), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/ring/snapshot/event/{event_id}.jpg",
         summary="Snapshot pinned to a specific Sentry event")
async def ring_event_snapshot(event_id: str, k: str = ""):
    """Serve the frame Sentry actually assessed for one event. The per-camera
    cache above is overwritten by every new snapshot, so alert cards fetched
    later (push feed, app relaunch) would show the WRONG moment — Sentry pins
    the assessed frame under ring:snapshot:event:{id} (48h TTL) and points its
    media_url here instead."""
    expected = os.environ.get("MOBILE_API_KEY", "")
    if expected and k != expected:
        raise HTTPException(status_code=401, detail="bad key")
    if not re.fullmatch(r"[a-f0-9]{32}", event_id):
        raise HTTPException(status_code=400, detail="bad event id")
    snap = _redis.get(f"ring:snapshot:event:{event_id}")
    if not snap:
        raise HTTPException(status_code=404, detail="event snapshot expired or unknown")
    return Response(content=base64.b64decode(snap), media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------------------
# Ring live view — on-demand RTSP → HLS relay
#
# ring-mqtt runs an RTSP server (rtsp://jarvis-ring-mqtt:8554/<device>_live,
# stream starts when a client connects). iOS AVPlayer can't play RTSP, so we
# relay to HLS with ffmpeg (already in this image): the app / glasses HUD play
# the .m3u8 natively via the same media_url path as snapshots.
# ---------------------------------------------------------------------------

RING_RTSP_BASE = os.environ.get("RING_RTSP_BASE", "rtsp://jarvis-ring-mqtt:8554")
RING_LIVE_USER = os.environ.get("RING_LIVE_USER", "jarvis")
RING_LIVE_PASS = os.environ.get("RING_LIVE_PASS", "jarvis-live-7e74a7")
RING_LIVE_DURATION_S = int(os.environ.get("RING_LIVE_DURATION_S", "300"))
_HLS_ROOT = "/tmp/ring_hls"
_live_procs: dict = {}


@app.post("/ring/live/{device}/start", summary="Start a live HLS relay for a Ring camera")
async def ring_live_start(device: str, k: str = ""):
    import subprocess, shutil, threading, re as _re

    expected = os.environ.get("MOBILE_API_KEY", "")
    if expected and k != expected:
        raise HTTPException(status_code=401, detail="bad key")
    if not _re.fullmatch(r"[a-f0-9]+", device):
        raise HTTPException(status_code=400, detail="bad device id")

    outdir = os.path.join(_HLS_ROOT, device)
    playlist = os.path.join(outdir, "index.m3u8")

    proc = _live_procs.get(device)
    if proc is None or proc.poll() is not None:
        shutil.rmtree(outdir, ignore_errors=True)
        os.makedirs(outdir, exist_ok=True)
        src = f"rtsp://{RING_LIVE_USER}:{RING_LIVE_PASS}@{RING_RTSP_BASE.split('://')[1]}/{device}_live"
        proc = subprocess.Popen(
            ["ffmpeg", "-v", "quiet", "-rtsp_transport", "tcp", "-i", src,
             "-c", "copy", "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
             "-hls_flags", "delete_segments", playlist],
        )
        _live_procs[device] = proc

        def _reaper(p=proc, d=device):
            try:
                p.wait(timeout=RING_LIVE_DURATION_S)
            except Exception:
                p.terminate()
            _live_procs.pop(d, None)
        threading.Thread(target=_reaper, daemon=True).start()

    # Wait for the playlist to materialize (stream spin-up takes a few seconds)
    for _ in range(30):
        if os.path.exists(playlist) and os.path.getsize(playlist) > 0:
            return {"ok": True, "playlist": f"/ring/live/{device}/index.m3u8",
                    "expires_in_s": RING_LIVE_DURATION_S}
        await asyncio.sleep(1)
    raise HTTPException(status_code=504,
                        detail="Stream didn't start — camera offline or RTSP auth wrong")


@app.post("/ring/snapshot/{device}/refresh", summary="Grab a FRESH frame from the camera via RTSP")
async def ring_snapshot_refresh(device: str, k: str = ""):
    """Force a fresh snapshot by pulling one frame from the camera's live RTSP
    stream (10-20s: Ring wakes the camera on connect). Updates the Redis cache
    the tools/Sentry read, so everything downstream sees the new frame.
    Battery cameras (e.g. doorbells) don't do interval snapshots — this is the
    only way to get a current frame from them without a motion event.
    """
    import subprocess, tempfile, re as _re

    expected = os.environ.get("MOBILE_API_KEY", "")
    if expected and k != expected:
        raise HTTPException(status_code=401, detail="bad key")
    if not _re.fullmatch(r"[a-f0-9]+", device):
        raise HTTPException(status_code=400, detail="bad device id")

    src = f"rtsp://{RING_LIVE_USER}:{RING_LIVE_PASS}@{RING_RTSP_BASE.split('://')[1]}/{device}_live"
    out = tempfile.mktemp(suffix=".jpg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "quiet", "-rtsp_transport", "tcp", "-i", src,
            "-frames:v", "1", "-q:v", "4", out,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=40)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=504, detail="Camera didn't produce a frame in time")
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise HTTPException(status_code=502, detail="No frame captured (camera offline?)")
        with open(out, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        _redis.set(f"ring:camera:{device}:snapshot", b64)
        _redis.set(f"ring:camera:{device}:snapshot_ts",
                   datetime.now(timezone.utc).isoformat())
        return {"ok": True, "bytes": len(b64) * 3 // 4}
    finally:
        if os.path.exists(out):
            os.unlink(out)


@app.get("/ring/live/{device}/{fname}", summary="Serve HLS playlist/segments for a live view")
async def ring_live_files(device: str, fname: str):
    import re as _re
    # Segment names are ffmpeg-generated; playlist URL carries no query params
    # once handed to a video player, so this endpoint is intentionally keyless —
    # device ids are unguessable and streams live ≤5 min.
    if not _re.fullmatch(r"[a-f0-9]+", device) or not _re.fullmatch(r"[\w.-]+", fname):
        raise HTTPException(status_code=400, detail="bad path")
    path = os.path.join(_HLS_ROOT, device, fname)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found (stream ended?)")
    media = "application/vnd.apple.mpegurl" if fname.endswith(".m3u8") else "video/mp2t"
    with open(path, "rb") as f:
        return Response(content=f.read(), media_type=media,
                        headers={"Cache-Control": "no-store"})


class PresenceLocationRequest(BaseModel):
    event: str   # "arrived" | "left"


@app.post("/presence/location", summary="[iOS app] Home geofence enter/exit")
async def presence_location(req: PresenceLocationRequest,
                            x_api_key: str = Header(default="")):
    """Phone geofence crossed home — the reliable arrival signal Sentry's lone
    indoor camera can't provide. Records presence and publishes an MQTT event
    the agent_runner turns into an arrival greeting + scene (on 'arrived') or
    updates away-state (on 'left', which routes proactive alerts to the phone).
    """
    _check_api_key(x_api_key)
    ev = req.event.strip().lower()
    if ev not in ("arrived", "left"):
        raise HTTPException(400, "event must be 'arrived' or 'left'")
    now = datetime.now(timezone.utc).isoformat()
    try:
        _redis.set("user:presence:home", "1" if ev == "arrived" else "0")
        _redis.set("user:presence:updated", now)
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")
    # Fire-and-forget MQTT so the agent_runner can react
    try:
        import paho.mqtt.publish as _pub
        _pub.single("jarvis/presence/home",
                    json.dumps({"event": ev, "ts": now}),
                    hostname=MQTT_HOST, port=MQTT_PORT)
    except Exception as e:
        print(f"[Gateway] presence publish failed: {e}")
    print(f"[Gateway] presence: {ev}")
    return {"ok": True, "event": ev}


@app.get("/agents/feed", summary="[iOS app] All agents + their latest reports (token-free reads)")
async def agents_feed(x_api_key: str = Header(default="")):
    """Dynamic agent feed for the app's Agents tab: whatever agents exist in
    Redis (driven by agents.yml — works for any user's setup), each with its
    persona, status, and most recent report. Pure Redis — zero LLM tokens.
    Structured extras (workout plan, meal plan) ride along when present.
    """
    _check_api_key(x_api_key)
    agents = []
    try:
        for key in _redis.keys("agent:*:meta"):
            name = key.split(":")[1]
            try:
                meta = json.loads(_redis.get(key) or "{}")
            except Exception:
                meta = {}
            report, ts = "", ""
            raw = _redis.lrange(f"agent:{name}:reports", 0, 0)
            if raw:
                try:
                    r0 = json.loads(raw[0])
                    report, ts = r0.get("report", ""), r0.get("timestamp", "")
                except Exception:
                    pass
            agents.append({
                "name": name,
                "display_name": meta.get("display_name", name),
                "persona": meta.get("persona_name", ""),
                "description": (meta.get("description") or "").strip(),
                "enabled": bool(meta.get("enabled")),
                "status": _redis.get(f"agent:{name}:status") or "idle",
                "last_run": ts,
                "report": report[:6000],
            })
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")
    agents.sort(key=lambda a: a["last_run"], reverse=True)
    return {"agents": agents}


@app.get("/agents/events", summary="[iOS app] Unified agent activity stream (token-free reads)")
async def agents_events(limit: int = 100, x_api_key: str = Header(default="")):
    """Newest-first stream of every agent's tool calls, thinking steps, and
    findings — the same feed the dashboard's DEV panel renders. Written by
    BaseAgent.log_event to jarvis:agent_events. Pure Redis — zero LLM tokens.
    """
    _check_api_key(x_api_key)
    limit = max(1, min(limit, 500))
    events = []
    try:
        for raw in _redis.lrange("jarvis:agent_events", 0, limit - 1):
            try:
                events.append(json.loads(raw))
            except Exception:
                pass
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")
    return {"events": events}


@app.get("/history", summary="[iOS/Mac app] Recent conversation history for chat restore")
async def get_history(x_api_key: str = Header(default=""), limit: int = 40):
    """Return the last N conversation turns so app chats survive relaunch.

    Reads the same Redis list the llm_agent maintains (conversation:{user}).
    Content blocks (tool_use/images) are flattened to plain text.
    """
    _check_api_key(x_api_key)
    user_id = os.environ.get("JARVIS_USER_ID", "default")
    try:
        raw = _redis.lrange(f"conversation:{user_id}", -min(limit, 80), -1)
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")

    messages = []
    for item in raw:
        try:
            msg = json.loads(item)
        except Exception:
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
        if not isinstance(content, str) or not content.strip():
            continue
        role = msg.get("role", "")
        if role in ("user", "assistant"):
            messages.append({"role": role, "text": content.strip()})
    return {"messages": messages}


class FaceEnrollRequest(BaseModel):
    name: str
    images: list[str]          # base64 JPEGs (app selfies)
    finalize: bool = True


@app.post("/face/enroll", summary="[iOS app] Teach Jarvis a face from selfies")
async def face_enroll(req: FaceEnrollRequest, x_api_key: str = Header(default="")):
    """Self-service face enrollment — works with zero home hardware.

    Relays each selfie to the vision service (MQTT jarvis/vision/enroll_image),
    which extracts a face embedding per usable image; finalize averages them
    into the face:{name} identity Sentry's recognition compares against.
    Re-enrolling the same name REPLACES the identity (fresh sample buffer).
    """
    _check_api_key(x_api_key)
    name = re.sub(r"[^a-z0-9_-]", "", req.name.strip().lower())
    if not name:
        raise HTTPException(400, "name must contain letters/numbers")
    if not req.images:
        raise HTTPException(400, "no images provided")
    if len(req.images) > 8:
        raise HTTPException(400, "at most 8 images per enrollment")

    _redis.delete(f"face_samples:{name}")
    for img in req.images:
        _mqtt_client.publish("jarvis/vision/enroll_image",
                             json.dumps({"name": name, "image_b64": img}))

    # Vision processes asynchronously — wait for the sample count to settle
    # (a few seconds per image on CPU), then finalize.
    samples, stable = 0, 0
    for _ in range(30):                        # up to ~15s
        await asyncio.sleep(0.5)
        n = int(_redis.llen(f"face_samples:{name}") or 0)
        stable = stable + 1 if n == samples else 0
        samples = n
        if samples >= len(req.images) or (samples > 0 and stable >= 6):
            break

    if samples == 0:
        raise HTTPException(422, "no usable face found in any image — "
                                 "try closer, well-lit, face-on selfies")
    enrolled = False
    if req.finalize:
        _mqtt_client.publish("jarvis/vision/enroll_finalize",
                             json.dumps({"name": name}))
        for _ in range(10):                    # wait for face:{name} to land
            await asyncio.sleep(0.5)
            if _redis.exists(f"face:{name}"):
                enrolled = True
                break
    return {"name": name, "samples": samples, "of": len(req.images),
            "enrolled": enrolled}


@app.get("/pushes", summary="[iOS/Mac app] Recent proactive pushes (Sentry cards etc.)")
async def get_pushes(x_api_key: str = Header(default=""), limit: int = 20):
    """Return recent surface pushes (newest last) so the app can show cards it
    missed while suspended — the WebSocket only reaches a foregrounded app.
    Items carry a stable ``id`` for client-side dedupe."""
    _check_api_key(x_api_key)
    try:
        raw = _redis.lrange("surface:pushes", 0, min(limit, 50) - 1)
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")
    pushes = []
    for item in reversed(raw):   # stored newest-first; serve oldest-first
        try:
            pushes.append(json.loads(item))
        except Exception:
            continue
    return {"pushes": pushes}


# ---------------------------------------------------------------------------
# APNs device registration (inert until APNS_* env is configured — see apns.py)
# ---------------------------------------------------------------------------

@app.post("/apns/register", summary="[iOS app] Register an APNs device token")
async def apns_register(request: Request, x_api_key: str = Header(default="")):
    _check_api_key(x_api_key)
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = str(body.get("token", "")).strip()
    if not token or len(token) < 32:
        raise HTTPException(400, "missing device token")
    _redis.sadd("apns:tokens", token)
    import apns
    return {"ok": True, "apns_configured": apns.configured(),
            "registered_devices": _redis.scard("apns:tokens")}


# ---------------------------------------------------------------------------
# Approval Inbox — list pending approvals; publish decisions to the bus.
# The agent_runner is the single executor (jarvis/approvals/resolve listener);
# this endpoint only requests the resolution, then the app polls status.
# ---------------------------------------------------------------------------

@app.get("/approvals", summary="[iOS/Mac app + dashboard] Pending approvals")
async def get_approvals(x_api_key: str = Header(default=""), history: int = 0):
    _check_api_key(x_api_key)
    try:
        pending = []
        now = datetime.now(timezone.utc).timestamp()
        for raw in (_redis.hgetall("jarvis:approvals:pending") or {}).values():
            try:
                rec = json.loads(raw)
                if float(rec.get("expires", 0)) >= now:
                    pending.append(rec)
            except Exception:
                continue
        pending.sort(key=lambda rec: rec.get("created", ""))
        out = {"pending": pending}
        if history:
            out["resolved"] = [json.loads(x) for x in
                               _redis.lrange("jarvis:approvals:log", 0,
                                             min(history, 50) - 1)]
        return out
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Redis unavailable: {exc}")


@app.post("/approvals/{approval_id}/resolve",
          summary="[iOS/Mac app + dashboard] Approve or deny a pending approval")
async def resolve_approval(approval_id: str, request: Request,
                           x_api_key: str = Header(default="")):
    _check_api_key(x_api_key)
    try:
        body = await request.json()
    except Exception:
        body = {}
    decision = str(body.get("decision", "")).lower()
    if decision not in ("approve", "approved", "deny", "denied"):
        raise HTTPException(400, "decision must be 'approve' or 'deny'")
    if not _redis.hexists("jarvis:approvals:pending", approval_id):
        status = _redis.get(f"jarvis:approvals:status:{approval_id}") or "unknown"
        return {"ok": False, "status": status,
                "detail": "not pending (already resolved or expired)"}
    _mqtt_client.publish("jarvis/approvals/resolve", json.dumps({
        "id": approval_id, "decision": decision,
        "by": body.get("by", "app"),
    }))
    return {"ok": True, "status": "resolving",
            "detail": "decision published; poll GET /approvals or the status key"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MOBILE_GATEWAY_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
