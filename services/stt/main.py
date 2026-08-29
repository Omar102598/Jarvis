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
SILENCE_DURATION = float(os.environ.get("STT_SILENCE_DURATION", "0.7"))  # seconds of silence to stop
INITIAL_GRACE = float(os.environ.get("STT_INITIAL_GRACE", "0.6"))  # seconds before silence detection starts
# Hard ceiling on one utterance. Reached ONLY when silence is never detected
# (ambient noise above SILENCE_THRESHOLD), in which case it is pure added
# latency on every turn — so keep it tight and watch the "hit cap" warning.
MAX_RECORD_SECONDS = float(os.environ.get("STT_MAX_RECORD_SECONDS", "10"))
# Seconds after TTS finishes to keep an open mic for follow-ups (0 = disabled)
FOLLOWUP_WINDOW = float(os.environ.get("STT_FOLLOWUP_WINDOW", "25.0"))

AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio_cache")
AUDIO_CACHE_MAX_FILES = int(os.environ.get("AUDIO_CACHE_MAX_FILES", "50"))

# Rooms with a real physical mic (mirrors tts_mac's PHYSICAL_ROOMS). The
# follow-up window is keyed off whatever room string arrives on
# jarvis/tts/{room}/done — with only ONE physical mic on this Mac, a /done for
# a synthetic bus-check room (Vega's "qa-<hash>", Miles' "miles-<hash>") would
# still open a REAL recording window and could pick up TTS self-echo if it
# ever got physically spoken. This is defense-in-depth alongside tts_mac's own
# allowlist, not a duplicate of the same bug.
PHYSICAL_ROOMS = {r.strip() for r in
                  os.environ.get("PHYSICAL_ROOMS", os.environ.get("ROOM_NAME", "office")).split(",")
                  if r.strip()}

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

# Keyword-combination fallback: real hallucinations often chain/modify stock
# phrases ("Thanks SO MUCH for watching", "...watching.  Have a great day.")
# which break exact-match against _HALLUCINATION_PHRASES entirely — caught
# THREE separate times (2026-07-19/20) because the filter only checked exact
# equality. Each tuple below is a set of keywords that, ALL present anywhere
# in the transcript, mark it as a near-certain YouTube-outro artifact rather
# than something a user would plausibly say to a voice assistant.
_HALLUCINATION_KEYWORD_GROUPS = [
    ("thank", "watch"),          # "thank(s) (so much) for watching"
    ("subscribe",),
    ("see you", "next video"),
    ("see you", "next time"),
]

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
    if any(all(kw in lower for kw in group)
           for group in _HALLUCINATION_KEYWORD_GROUPS):
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

    import time as _time
    _t0 = _time.time()
    _rms_floor = None
    _stopped_on_silence = False

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

            _rms_floor = rms if _rms_floor is None else min(_rms_floor, rms)

            if rms < SILENCE_THRESHOLD:
                silent_chunks += 1
            else:
                silent_chunks = 0

            if silent_chunks > int(SILENCE_DURATION * chunks_per_second):
                _stopped_on_silence = True
                break

        chunk_index += 1

    stream.stop_stream()
    stream.close()
    audio.terminate()

    _elapsed = _time.time() - _t0
    if _stopped_on_silence:
        print(f"[STT] Recorded {_elapsed:.1f}s (stopped on silence; "
              f"quietest RMS {_rms_floor or 0:.0f} vs threshold {SILENCE_THRESHOLD})")
    else:
        print(f"[STT] Recorded {_elapsed:.1f}s — HIT THE {MAX_RECORD_SECONDS}s CAP: "
              f"silence never detected (quietest RMS {_rms_floor or 0:.0f} never fell "
              f"below threshold {SILENCE_THRESHOLD}). Raise STT_SILENCE_THRESHOLD "
              f"above the room's noise floor to stop paying this on every turn.")

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

    if room not in PHYSICAL_ROOMS:
        return   # synthetic/virtual room — no real mic to follow up on

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
        # Media guard: while music/TV is playing (flag set by the spotify /
        # apple_tv tools), an open follow-up mic hears the SPEAKERS — Whisper
        # transcribes lyrics/dialogue as commands and Jarvis loops talking to
        # the music. Skip the window; the wake word still works during playback.
        try:
            src = _r.get("jarvis:media:playing")
            if src:
                print(f"[STT] Media playing ({src}) — follow-up window skipped "
                      "(wake word still active).")
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

    # Subscribe in on_connect (NOT once in main): paho only restores
    # subscriptions on reconnect when they're made here. Subscribing in main
    # meant a mosquitto restart left this service connected but DEAF — wake
    # words fired, but STT never heard the events (voice "stopped working").
    def on_connect(client, userdata, flags, rc):
        client.subscribe("jarvis/audio/mic/+/wake_word")
        client.subscribe("jarvis/tts/+/done")
        print(f"[STT] MQTT connected (rc={rc}), subscriptions active.")

    mqtt_client.on_connect = on_connect
    mqtt_client.message_callback_add("jarvis/audio/mic/+/wake_word", on_wake_word)
    mqtt_client.message_callback_add("jarvis/tts/+/done", on_tts_done)
    # Back off between reconnect attempts rather than hammering a broker
    # that is still coming back up.
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    mqtt_client.connect(MQTT_HOST, MQTT_PORT)

    print("[STT] Waiting for wake word events...")
    # A broker restart or network blip used to raise straight out of
    # loop_forever and kill this process — the whole voice stack then stayed
    # dead until someone noticed. Keep retrying instead; paho reconnects and
    # re-subscribes through its on_connect handler.
    while True:
        try:
            mqtt_client.loop_forever(retry_first_connection=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[mqtt] connection lost ({exc}) — retrying in 5s", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
