"""JARVIS Speech-to-Text Service.

Subscribes to wake word events. When triggered, records audio from the mic
until silence is detected, then transcribes using faster-whisper and publishes
the text to MQTT.

The raw command audio is also written to a shared volume (AUDIO_DIR) and
its path is included in the MQTT payload so downstream services (speaker
verification) can analyze the same audio.

Runs on GPU or CPU:
    STT_DEVICE=auto|cuda|cpu   (default: auto — uses CUDA when available)
    STT_MODEL=turbo|small|base|... (default: turbo on GPU, small on CPU)
"""

import io
import json
import os
import re
import time
import wave

import numpy as np
import paho.mqtt.client as mqtt
import pyaudio
from faster_whisper import WhisperModel

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# Redis (for the brain's conversation-end signal). Native services reach Redis on
# host port 6380. Optional — the follow-up window still works if Redis is down.
try:
    import redis as _redis_mod
    _r = _redis_mod.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6380")),
        decode_responses=True,
    )
except Exception:
    _r = None

SAMPLE_RATE = 16000
CHUNK_SIZE = 1600  # 100ms at 16kHz
SILENCE_THRESHOLD = int(os.environ.get("STT_SILENCE_THRESHOLD", "150"))  # RMS; lower = more sensitive
SILENCE_DURATION = float(os.environ.get("STT_SILENCE_DURATION", "1.2"))  # seconds of silence to stop
INITIAL_GRACE = float(os.environ.get("STT_INITIAL_GRACE", "1.5"))  # seconds before silence detection starts
MAX_RECORD_SECONDS = 15
# Seconds after TTS finishes to keep an open mic for follow-ups (0 = disabled)
FOLLOWUP_WINDOW = float(os.environ.get("STT_FOLLOWUP_WINDOW", "25.0"))

AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio_cache")
AUDIO_CACHE_MAX_FILES = int(os.environ.get("AUDIO_CACHE_MAX_FILES", "50"))

# ---------------------------------------------------------------------------
# Hallucination filter — faster-whisper emits these on silence/noise
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
    "bye",
    "goodbye",
    "you",
    ".",
    "",
}

_REPEAT_PATTERN = re.compile(r"(.{10,}?)\1{2,}", re.DOTALL)


def _is_hallucination(text: str) -> bool:
    """Return True if the transcription looks like a faster-whisper artifact."""
    lower = text.lower().strip().rstrip(".,!?")
    if lower in _HALLUCINATION_PHRASES:
        return True
    if len(text.strip()) < 3:
        return True
    if _REPEAT_PATTERN.search(text):
        return True
    return False


def resolve_device() -> tuple:
    """Pick (device, compute_type, default_model) based on STT_DEVICE."""
    requested = os.environ.get("STT_DEVICE", "auto").lower()
    if requested == "cpu":
        return "cpu", "int8", "small"
    if requested == "cuda":
        return "cuda", "float16", "turbo"

    # auto: probe for CUDA without requiring torch
    try:
        import ctypes
        ctypes.CDLL("libcudart.so")
        return "cuda", "float16", "turbo"
    except OSError:
        return "cpu", "int8", "small"


DEVICE, COMPUTE_TYPE, DEFAULT_MODEL = resolve_device()
MODEL_NAME = os.environ.get("STT_MODEL", DEFAULT_MODEL)

print(f"[STT] Loading whisper '{MODEL_NAME}' on {DEVICE} ({COMPUTE_TYPE})...")
model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
print("[STT] Model loaded.")


def _beep():
    """Play a short confirmation tone via macOS afplay so the user knows to speak."""
    import subprocess
    import tempfile
    import struct
    import math
    # Generate a short 880 Hz beep as raw WAV and play it
    try:
        freq, duration, rate = 880, 0.12, 16000
        samples = [int(32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(rate * duration))]
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
            import wave as wv
            with wv.open(tmp, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        subprocess.run(["afplay", tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
    except Exception:
        pass  # beep is best-effort


def capture_command_audio() -> io.BytesIO:
    """Record audio until silence or max duration, return as WAV BytesIO."""
    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
        input=True, frames_per_buffer=CHUNK_SIZE,
    )

    frames = []
    silent_chunks = 0
    chunks_per_second = SAMPLE_RATE // CHUNK_SIZE
    grace_chunks = int(INITIAL_GRACE * chunks_per_second)
    chunk_index = 0

    for _ in range(int(MAX_RECORD_SECONDS * chunks_per_second)):
        data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        frames.append(data)

        # During the grace period don't check for silence — give the user
        # time to start speaking after the beep.
        if chunk_index >= grace_chunks:
            audio_data = np.frombuffer(data, dtype=np.int16)
            rms = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))

            if rms < SILENCE_THRESHOLD:
                silent_chunks += 1
            else:
                silent_chunks = 0

            if silent_chunks > int(SILENCE_DURATION * chunks_per_second):
                break

        chunk_index += 1

    stream.stop_stream()
    stream.close()
    audio.terminate()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    buf.seek(0)
    return buf


def cache_audio(audio_buf: io.BytesIO, room: str) -> str:
    """Persist command audio to the shared volume for speaker verification.

    Returns the file path, or "" if the cache dir is unavailable.
    """
    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)
        path = os.path.join(AUDIO_DIR, f"{room}-{int(time.time() * 1000)}.wav")
        with open(path, "wb") as f:
            f.write(audio_buf.getbuffer())

        # Prune oldest files beyond the cap
        wavs = sorted(
            (os.path.join(AUDIO_DIR, n) for n in os.listdir(AUDIO_DIR)
             if n.endswith(".wav")),
            key=os.path.getmtime,
        )
        for old in wavs[:-AUDIO_CACHE_MAX_FILES]:
            os.remove(old)

        return path
    except OSError as e:
        print(f"[STT] Could not cache audio: {e}")
        return ""


def _publish_speech(client, room: str, text: str, audio_path: str) -> None:
    client.publish(
        f"jarvis/audio/mic/{room}/speech",
        json.dumps({
            "text": text,
            "room": room,
            "language": "en",
            "audio_path": audio_path,
        }),
    )


def on_tts_done(client, userdata, msg):
    """After Jarvis finishes speaking, open the mic for a follow-up reply.

    The user doesn't need to say 'Hey Jarvis' — just start talking within
    FOLLOWUP_WINDOW seconds of the response ending.
    """
    if FOLLOWUP_WINDOW <= 0:
        return

    try:
        data = json.loads(msg.payload)
        room = data.get("room", "office")
    except Exception:
        room = "office"

    # Conversation-end reasoning: the brain sets jarvis:voice:end_turn:{room} when
    # the user's turn was a closer ("thanks", "that's all"). The reply just played;
    # now DON'T re-open the mic — the conversation is naturally over.
    if _r is not None:
        try:
            if _r.get(f"jarvis:voice:end_turn:{room}"):
                _r.delete(f"jarvis:voice:end_turn:{room}")
                print(f"[STT] Conversation ended (brain signalled end-of-turn) in '{room}'.")
                return
        except Exception:
            pass

    # Brief pause so the mic doesn't pick up audio reverb from the speaker
    time.sleep(0.6)
    print(f"[STT] Follow-up window open for {FOLLOWUP_WINDOW}s in '{room}'...")
    _beep()

    audio_buf = capture_command_audio()
    audio_path = cache_audio(audio_buf, room)
    audio_buf.seek(0)

    segments, info = model.transcribe(audio_buf, language="en", vad_filter=True)
    text = " ".join([s.text for s in segments]).strip()

    if text and not _is_hallucination(text):
        # Closers ("thanks", "that's all") are no longer swallowed silently here —
        # they go to the brain, which replies warmly AND sets the end-turn flag so
        # the NEXT on_tts_done won't re-open the mic. Feels like a natural sign-off.
        print(f"[STT] Follow-up ({room}): '{text}'")
        _publish_speech(client, room, text, audio_path)
    else:
        reason = "hallucination filtered" if text else "no speech detected"
        print(f"[STT] Follow-up window closed — {reason}: '{text}'")
        if audio_path and os.path.exists(audio_path):
            os.unlink(audio_path)


def on_wake_word(client, userdata, msg):
    """Handle wake word detection: record, transcribe, publish."""
    data = json.loads(msg.payload)
    room = data["room"]
    print(f"[STT] Wake word in '{room}', beeping and recording...")
    _beep()

    audio_buf = capture_command_audio()
    audio_path = cache_audio(audio_buf, room)
    audio_buf.seek(0)

    segments, info = model.transcribe(audio_buf, language="en", vad_filter=True)
    text = " ".join([s.text for s in segments]).strip()

    if text and not _is_hallucination(text):
        print(f"[STT] Transcribed ({room}): '{text}'")
        _publish_speech(client, room, text, audio_path)
    else:
        reason = "hallucination filtered" if text else "no speech detected"
        print(f"[STT] {reason} in {room}: '{text}'")
        if audio_path and os.path.exists(audio_path):
            os.unlink(audio_path)


def main():
    mqtt_client = mqtt.Client()
    mqtt_client.connect(MQTT_HOST, MQTT_PORT)
    mqtt_client.subscribe("jarvis/audio/mic/+/wake_word")
    mqtt_client.message_callback_add("jarvis/audio/mic/+/wake_word", on_wake_word)
    mqtt_client.subscribe("jarvis/tts/+/done")
    mqtt_client.message_callback_add("jarvis/tts/+/done", on_tts_done)

    print("[STT] Waiting for wake word events...")
    mqtt_client.loop_forever()


if __name__ == "__main__":
    main()
