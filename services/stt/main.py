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
SILENCE_DURATION = float(os.environ.get("STT_SILENCE_DURATION", "0.7"))  # silence AFTER speech that ends a take
# (STT_SILENCE_THRESHOLD / STT_INITIAL_GRACE are gone: the threshold now tracks the
# room noise floor and silence only counts after speech -- see MicStream below.)
# Hard ceiling on one utterance. Reached ONLY when silence is never detected
# (noise never drops below the adaptive threshold), in which case it is pure added
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


# ---------------------------------------------------------------------------
# Always-open mic + endpointing
#
# The old capture opened PyAudio fresh on every wake word, AFTER a blocking
# afplay beep, then started counting "silence" 0.6s in whether or not the user
# had said anything, against a fixed RMS of 350. This mic puts normal speech
# under 350, so every take ended at ~1.8s: "set the thermostat to 71 cool" came
# through as "that the thermostat is 71." -- first word lost to the beep/open
# gap, last words lost to the early stop.
#
# Now: one stream stays open and feeds a rolling buffer (so the words said
# right after "Hey Jarvis" are already captured when the wake event lands),
# the speech threshold tracks the room's own noise floor, and silence only
# counts once speech has actually been heard (as Hermes Agent does).
# ---------------------------------------------------------------------------
import collections
import queue
import threading

PREROLL_SECONDS = float(os.environ.get("STT_PREROLL_SECONDS", "0.8"))
# Speech must sit above threshold this long before silence starts counting.
SPEECH_CONFIRM = float(os.environ.get("STT_SPEECH_CONFIRM", "0.3"))
# Give up if no speech is heard at all within this long.
NO_SPEECH_TIMEOUT = float(os.environ.get("STT_NO_SPEECH_TIMEOUT", "4.0"))
# Adaptive threshold = noise floor x multiplier, clamped to [min, max].
NOISE_MULTIPLIER = float(os.environ.get("STT_NOISE_MULTIPLIER", "2.0"))
THRESHOLD_MIN = float(os.environ.get("STT_THRESHOLD_MIN", "150"))
THRESHOLD_MAX = float(os.environ.get("STT_THRESHOLD_MAX", "600"))
# Silero speech probability (bundled with faster-whisper) decides speech vs
# silence; loudness is only the fallback. This mic puts normal speech at
# ~1.5-2x the room's noise floor, too close for an RMS cutoff to be reliable.
VAD_THRESHOLD = float(os.environ.get("STT_VAD_THRESHOLD", "0.5"))
BEEP = os.environ.get("STT_BEEP", "false").strip().lower() in ("1", "true", "yes")

_CPS = SAMPLE_RATE // CHUNK_SIZE   # chunks per second (10)


def _rms(chunk: bytes) -> float:
    a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


class SpeechScorer:
    """Speech probability for the latest 100ms chunk, via Silero VAD over a
    ~0.5s sliding window (the model call is stateless, so it needs context)."""

    WINDOW = 8192   # 0.512s at 16kHz, a multiple of Silero's 512-sample frame

    def __init__(self):
        from faster_whisper.vad import get_vad_model
        self._model = get_vad_model()
        self._buf = np.zeros(self.WINDOW, dtype=np.float32)

    def reset(self):
        self._buf[:] = 0

    def __call__(self, chunk: bytes) -> float:
        a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, a])[-self.WINDOW:]
        probs = np.asarray(self._model(self._buf)).reshape(-1)
        return float(probs[-3:].max()) if probs.size else 0.0


class Endpointer:
    """Decides when an utterance is over, from per-chunk RMS. Pure logic, so it
    can be tested without a microphone."""

    def __init__(self, threshold: float = 0.5, no_speech_timeout: float | None = None):
        self.threshold = threshold
        self.no_speech_timeout = NO_SPEECH_TIMEOUT if no_speech_timeout is None else no_speech_timeout
        self.loud = 0          # consecutive chunks above threshold
        self.silent = 0        # consecutive chunks below, after speech
        self.speech = False
        self.n = 0

    def feed(self, level: float) -> str | None:
        """`level` is a speech probability (or RMS in fallback mode), compared
        against `threshold`. Returns None to keep going, or why it stopped:
        'silence' | 'no_speech'."""
        self.n += 1
        if level >= self.threshold:
            self.loud += 1
            self.silent = 0
            if self.loud >= max(1, int(SPEECH_CONFIRM * _CPS)):
                self.speech = True
        else:
            self.loud = 0
            if self.speech:
                self.silent += 1
                if self.silent >= int(SILENCE_DURATION * _CPS):
                    return "silence"
        if not self.speech and self.n >= int(self.no_speech_timeout * _CPS):
            return "no_speech"
        return None


class MicStream:
    """One always-open input stream. Keeps the last few seconds of RMS for the
    noise floor and the last PREROLL_SECONDS of audio for pre-roll."""

    def __init__(self):
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                                     input=True, frames_per_buffer=CHUNK_SIZE)
        self._ring = collections.deque(maxlen=max(1, int(PREROLL_SECONDS * _CPS)))
        self._levels = collections.deque(maxlen=5 * _CPS)
        self._listener: queue.Queue | None = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name="mic").start()

    def _run(self):
        while True:
            try:
                data = self._stream.read(CHUNK_SIZE, exception_on_overflow=False)
            except Exception as exc:
                print(f"[STT] mic read error: {exc}")
                time.sleep(0.5)
                continue
            with self._lock:
                self._ring.append(data)
                self._levels.append(_rms(data))
                if self._listener is not None:
                    self._listener.put(data)

    def noise_floor(self) -> float:
        lv = sorted(self._levels)
        return lv[len(lv) // 5] if lv else 0.0   # 20th percentile of the last 5s

    def start(self, preroll: bool) -> tuple[list[bytes], queue.Queue]:
        q: queue.Queue = queue.Queue()
        with self._lock:
            pre = list(self._ring) if preroll else []
            self._listener = q
        return pre, q

    def stop(self):
        with self._lock:
            self._listener = None


_mic: MicStream | None = None
_scorer: "SpeechScorer | None" = None
_scorer_failed = False


def _get_scorer():
    global _scorer, _scorer_failed
    if _scorer is None and not _scorer_failed:
        try:
            _scorer = SpeechScorer()
        except Exception as exc:
            _scorer_failed = True
            print(f"[STT] Silero VAD unavailable ({exc}) — falling back to RMS")
    return _scorer


def capture_command_audio(preroll: bool = True,
                          no_speech_timeout: float | None = None) -> io.BytesIO:
    """Record one utterance and return it as WAV. `preroll` prepends the audio
    from just before the call (the words right after the wake word)."""
    global _mic
    if _mic is None:
        _mic = MicStream()
        time.sleep(0.3)   # let the noise floor see a few chunks on first use

    floor = _mic.noise_floor()
    scorer = _get_scorer()
    if scorer is not None:
        scorer.reset()
        threshold = VAD_THRESHOLD
        level = scorer
    else:
        threshold = min(THRESHOLD_MAX, max(THRESHOLD_MIN, floor * NOISE_MULTIPLIER))
        level = _rms
    ep = Endpointer(threshold, no_speech_timeout)
    t0 = time.time()
    pre, q = _mic.start(preroll)
    frames = list(pre)
    for c in pre:          # warm the VAD's context window; doesn't drive endpointing
        if scorer is not None:
            scorer(c)
    why = "cap"
    try:
        for _ in range(int(MAX_RECORD_SECONDS * _CPS)):
            try:
                data = q.get(timeout=10)
            except queue.Empty:
                why = "mic stalled"
                break
            frames.append(data)
            r = ep.feed(level(data))
            if r:
                why = r
                break
    finally:
        _mic.stop()

    print(f"[STT] Recorded {time.time() - t0:.1f}s (+{len(pre) / _CPS:.1f}s pre-roll), "
          f"stop={why}, speech={'yes' if ep.speech else 'no'}, "
          f"{'vad' if scorer is not None else f'rms floor {floor:.0f}'} threshold {threshold:g}")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    buf.seek(0)
    return buf


# Pre-roll can catch the tail of the wake phrase itself.
_WAKE_PREFIX = re.compile(r"^\s*(hey|hi|okay|ok)?[\s,]*jarvis\b[\s,.!?]*", re.I)


def _strip_wake(text: str) -> str:
    out = _WAKE_PREFIX.sub("", text or "").strip()
    return out[:1].upper() + out[1:] if out else out


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
    if BEEP:
        _beep()

    # The window is how long to wait for the user to START a follow-up; once
    # they speak, normal endpointing ends the take.
    audio_buf = capture_command_audio(preroll=False, no_speech_timeout=FOLLOWUP_WINDOW)
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
    print(f"[STT] Wake word in '{room}', recording...")
    if BEEP:
        _beep()

    audio_buf = capture_command_audio(preroll=True)
    audio_path = cache_audio(audio_buf, room)
    audio_buf.seek(0)

    segments, info = model.transcribe(audio_buf, language="en", vad_filter=True)
    text = _strip_wake(" ".join([s.text for s in segments]).strip())

    if text and not _is_hallucination(text):
        print(f"[STT] Transcribed ({room}): '{text}'")
        _publish_speech(client, room, text, audio_path)
    else:
        reason = "hallucination filtered" if text else "no speech detected"
        print(f"[STT] {reason} in {room}: '{text}'")
        if audio_path and os.path.exists(audio_path):
            os.unlink(audio_path)


def main():
    # Open the mic at startup, not on the first wake word: the first CoreAudio
    # read can take seconds, and pre-roll needs the ring already filling.
    global _mic
    _mic = MicStream()
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
