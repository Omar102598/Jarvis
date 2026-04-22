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
import json
import os
import subprocess
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
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
    client.subscribe("jarvis/tts/mobile-+/speak")


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
    audio: UploadFile = File(..., description="Voice recording (WAV, M4A, MP3, OGG, …)"),
    x_api_key: str = Header(default=""),
):
    """
    Primary endpoint for the iPhone Siri Shortcut.

    The Shortcut should:
    1. Use the **Record Audio** action to capture the user's voice.
    2. POST that file to ``/ask/audio`` with the ``X-API-Key`` header.
    3. Play the returned WAV audio with the **Play Sound** action.
    """
    _check_api_key(x_api_key)

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")

    text = transcribe_audio(audio_bytes, audio.filename or "audio.wav")
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

