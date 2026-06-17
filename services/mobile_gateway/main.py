"""JARVIS Mobile Gateway Service.

Provides an HTTP REST API so an iPhone (via Siri Shortcuts) can talk to JARVIS
using voice, just like saying "Hey Siri, ask JARVIS...".

Flow
----
1. iPhone records voice → POST /ask/audio  (multipart audio file)
   OR iPhone sends typed text → POST /ask/text  (JSON body)
2. Gateway transcribes audio with faster-whisper (CPU, no GPU needed)
3. Gateway publishes request to the existing MQTT LLM-agent pipeline
   (room = "mobile-<request_id>") — all smart-home / web-search tools work
4. Gateway waits for the LLM response published to jarvis/tts/mobile-<id>/speak
5. Gateway synthesizes the reply with Piper TTS
6. Gateway returns WAV audio that the Siri Shortcut plays back on iPhone

Authentication
--------------
All endpoints require the ``X-API-Key`` header to match MOBILE_API_KEY in .env.
Leave MOBILE_API_KEY empty to disable auth (local-network-only deployments).
"""

import asyncio
import base64
import json
import os
import subprocess
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from faster_whisper import WhisperModel
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
PIPER_MODEL = os.environ.get("PIPER_MODEL", "en_GB-alan-medium")
LLM_TIMEOUT = float(os.environ.get("MOBILE_LLM_TIMEOUT", "45"))
API_KEY = os.environ.get("MOBILE_API_KEY", "")

# ---------------------------------------------------------------------------
# Shared state: pending_requests maps room → asyncio.Future(response_text)
# Protected by _pending_lock for thread-safe insert / delete from MQTT thread.
# ---------------------------------------------------------------------------

_pending_lock = threading.Lock()
pending_requests: dict[str, asyncio.Future] = {}
_event_loop: asyncio.AbstractEventLoop | None = None

# ---------------------------------------------------------------------------
# STT: faster-whisper (CPU, no GPU requirement for mobile)
# ---------------------------------------------------------------------------

print("[Gateway] Loading Whisper turbo model (CPU)...")
_stt_model = WhisperModel("turbo", device="cpu", compute_type="int8")
print("[Gateway] Whisper ready.")


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.wav") -> str:
    """Transcribe raw audio bytes to text."""
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
# TTS: Piper (same engine as the main TTS service)
# ---------------------------------------------------------------------------

def synthesize_speech(text: str) -> bytes:
    """Synthesize text to WAV bytes via Piper."""
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
            print(f"[Gateway] Piper TTS failed (rc={proc.returncode})")
            raise RuntimeError("TTS synthesis failed")
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# MQTT client — publishes requests and receives LLM responses
# ---------------------------------------------------------------------------

_mqtt_client = mqtt.Client()


def _on_mqtt_connect(client, userdata, flags, rc):
    if rc != 0:
        print(f"[Gateway] MQTT connection failed (rc={rc})")
        return
    print(f"[Gateway] MQTT connected (rc={rc})")
    # Subscribe to all mobile TTS response topics
    client.subscribe("jarvis/tts/+/speak")


def _on_mqtt_message(client, userdata, msg):
    """Handle LLM responses destined for a mobile room (called in MQTT thread)."""
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


# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _event_loop
    _event_loop = asyncio.get_event_loop()

    try:
        _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except OSError as exc:
        raise RuntimeError(
            f"[Gateway] Cannot connect to MQTT broker at {MQTT_HOST}:{MQTT_PORT}: {exc}"
        ) from exc

    _mqtt_client.loop_start()
    print("[Gateway] JARVIS Mobile Gateway ready.")

    yield  # Application runs here

    _mqtt_client.loop_stop()
    _mqtt_client.disconnect()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="JARVIS Mobile Gateway",
    description="Voice/text API for iPhone Siri Shortcut integration",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _check_api_key(x_api_key: str) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# ---------------------------------------------------------------------------
# Core pipeline: text → LLM (via MQTT) → TTS → WAV bytes
# ---------------------------------------------------------------------------

async def _run_pipeline(text: str) -> bytes:
    """Send text through MQTT LLM agent and return synthesized audio."""
    request_id = uuid.uuid4().hex[:12]
    room = f"mobile-{request_id}"

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
                "source": "mobile",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }),
        )

        response_text = await asyncio.wait_for(asyncio.shield(future), timeout=LLM_TIMEOUT)
    except asyncio.TimeoutError:
        future.cancel()
        raise HTTPException(
            status_code=504,
            detail="JARVIS did not respond in time. Make sure all backend services are running.",
        )
    finally:
        with _pending_lock:
            pending_requests.pop(room, None)

    return synthesize_speech(response_text)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/ask/audio",
    response_class=Response,
    summary="Send a voice recording and receive a spoken reply",
    responses={
        200: {"content": {"audio/wav": {}}, "description": "WAV audio of JARVIS reply"},
        401: {"description": "Invalid API key"},
        422: {"description": "No speech detected in audio"},
        504: {"description": "LLM response timeout"},
    },
)
async def ask_audio(
    request: Request,
    x_api_key: str = Header(default=""),
):
    """
    Primary endpoint for the iPhone Siri Shortcut.

    Accepts audio in any of three formats iOS Shortcuts may send:
    - multipart/form-data with an 'audio' file field  (preferred)
    - application/octet-stream raw body
    - raw body with any other content-type (m4a, wav, etc.)
    """
    _check_api_key(x_api_key)

    content_type = request.headers.get("content-type", "")
    audio_bytes = b""
    filename = "audio.m4a"

    if "multipart/form-data" in content_type:
        form = await request.form()
        audio_field = form.get("audio")
        if audio_field is None:
            # Try the first field regardless of name
            for v in form.values():
                audio_field = v
                break
        if audio_field is None:
            raise HTTPException(status_code=400, detail="No audio field in form data")
        if hasattr(audio_field, "read"):
            audio_bytes = await audio_field.read()
            filename = getattr(audio_field, "filename", None) or "audio.m4a"
        else:
            # Field came through as string — iOS sometimes base64-encodes it
            import base64
            try:
                audio_bytes = base64.b64decode(str(audio_field))
            except Exception:
                audio_bytes = str(audio_field).encode("latin-1")
    else:
        # Raw binary body (common from iOS "Get Contents of URL" with file variable)
        audio_bytes = await request.body()

    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio received")

    print(f"[Gateway] Received {len(audio_bytes)} bytes, content-type={content_type!r}")
    print(f"[Gateway] First 40 bytes hex: {audio_bytes[:40].hex()}")
    print(f"[Gateway] First 40 bytes ascii: {audio_bytes[:40]!r}")

    # Auto-detect base64-encoded audio (iOS Shortcuts sometimes base64-encodes the file)
    # Base64 data is entirely printable ASCII; real audio starts with binary bytes.
    if len(audio_bytes) > 10 and all(0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D) for b in audio_bytes[:200]):
        try:
            decoded = base64.b64decode(audio_bytes.strip())
            if len(decoded) > 100:
                print(f"[Gateway] Auto-decoded base64 → {len(decoded)} bytes, first hex: {decoded[:8].hex()}")
                audio_bytes = decoded
        except Exception as exc:
            print(f"[Gateway] Base64 decode attempt failed: {exc}")

    text = transcribe_audio(audio_bytes, filename)
    if not text:
        raise HTTPException(status_code=422, detail="No speech detected in audio")

    print(f"[Gateway] Transcribed: '{text}'")
    wav_bytes = await _run_pipeline(text)
    return Response(content=wav_bytes, media_type="audio/wav")


class TextRequest(BaseModel):
    text: str


@app.post(
    "/ask/text",
    response_class=Response,
    summary="Send a text query and receive a spoken reply",
    responses={
        200: {"content": {"audio/wav": {}}, "description": "WAV audio of JARVIS reply"},
        401: {"description": "Invalid API key"},
        504: {"description": "LLM response timeout"},
    },
)
async def ask_text(
    request: TextRequest,
    x_api_key: str = Header(default=""),
):
    """
    Alternative endpoint — useful for typed Siri Shortcuts or testing with curl.

    Example::

        curl -X POST http://jarvis.local:8080/ask/text \\
             -H "X-API-Key: your-key" \\
             -H "Content-Type: application/json" \\
             -d '{"text": "Turn off the office lights"}' \\
             --output reply.wav
    """
    _check_api_key(x_api_key)

    if not request.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")

    print(f"[Gateway] Text request: '{request.text}'")
    wav_bytes = await _run_pipeline(request.text)
    return Response(content=wav_bytes, media_type="audio/wav")


@app.post("/debug/audio", summary="Echo audio metadata for debugging")
async def debug_audio(request: Request):
    """Returns metadata about what the client sent — no API key required."""
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    first_hex = body[:60].hex() if body else ""
    first_ascii = repr(body[:60]) if body else ""
    # Check if it looks like base64
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
        "headers": dict(request.headers),
        "first_60_hex": first_hex,
        "first_60_ascii": first_ascii,
        "looks_like_base64": is_b64,
        "base64_decoded_size": decoded_size,
    }


@app.get("/health", summary="Health check")
async def health():
    """Returns OK when the gateway is up."""
    return {"status": "ok", "service": "jarvis-mobile-gateway"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MOBILE_GATEWAY_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)

