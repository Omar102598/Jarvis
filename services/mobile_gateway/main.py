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
from fastapi.responses import Response
from faster_whisper import WhisperModel
from pydantic import BaseModel

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
        return " ".join(s.text for s in segments).strip()
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

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


def _on_surface_push(client, userdata, msg):
    """Relay llm_agent surface fanout to all connected WebSocket clients."""
    global _event_loop
    try:
        data = json.loads(msg.payload.decode())
        text = data.get("text", "")
        if not text:
            return
        payload = DisplayPayload("text", "Jarvis", text[:200], None, text)
        if _event_loop is not None:
            asyncio.run_coroutine_threadsafe(_push_display_payload(payload), _event_loop)
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


_mqtt_client.on_connect = _on_mqtt_connect
_mqtt_client.on_message = _on_mqtt_message
_mqtt_client.message_callback_add("jarvis/surfaces/iphone/push", _on_surface_push)


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

async def _run_pipeline_audio(text: str, source: str = "mobile") -> tuple[str, bytes]:
    """Send text through MQTT agent; return (response_text, wav_bytes)."""
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
        response_text = await asyncio.wait_for(asyncio.shield(future), timeout=LLM_TIMEOUT)
    except asyncio.TimeoutError:
        future.cancel()
        raise HTTPException(status_code=504, detail="JARVIS did not respond in time.")
    finally:
        with _pending_lock:
            pending_requests.pop(room, None)

    wav_bytes = synthesize_speech(response_text)
    return response_text, wav_bytes


async def _run_pipeline_json(text: str) -> dict:
    """Run the full pipeline and return a JSON-friendly dict with display payload.

    Used by the native iOS app endpoints.  Sends source='glasses' so the LLM
    knows to format the response with DISPLAY:/BODY: headers.
    """
    response_text, wav_bytes = await _run_pipeline_audio(text, source="glasses")
    payload = parse_display_response(response_text)

    # Push display payload to any connected WebSocket clients immediately
    asyncio.create_task(_push_display_payload(payload))

    return {
        "text": payload.tts_text,
        "audio_b64": base64.b64encode(wav_bytes).decode(),
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

    if "multipart/form-data" in content_type:
        form = await request.form()
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

    print(f"[Gateway] /ask/audio transcribed: '{text}'")
    _, wav_bytes = await _run_pipeline_audio(text, source="mobile")
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


@app.post("/ask/query", summary="[iOS app] Text query → JSON with display payload")
async def ask_query(request: QueryRequest, x_api_key: str = Header(default="")):
    """
    Used by the native iOS Jarvis app for text and typed chat queries.
    Returns structured JSON so the app can update the HUD and play TTS directly.
    """
    _check_api_key(x_api_key)
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")
    print(f"[Gateway] /ask/query: '{request.text}'")
    return await _run_pipeline_json(request.text)


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

    # Wrap image as a data URI so the LLM agent's vision tool can receive it
    data_uri = f"data:image/jpeg;base64,{request.image_b64}"
    composite_text = f"[GLASSES_CAMERA_IMAGE: {data_uri}]\n{request.text}"

    print(f"[Gateway] /ask/image: prompt='{request.text}', image_len={len(request.image_b64)}")
    return await _run_pipeline_json(composite_text)


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
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MOBILE_GATEWAY_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
