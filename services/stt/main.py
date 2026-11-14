"""JARVIS Speech-to-Text Service.

Subscribes to wake word events. When triggered, records audio from the mic and
streams it to ElevenLabs Scribe (scribe.py), which transcribes as you speak and
decides when you've finished from what you said, not just from silence. If
Scribe is unavailable (no key, quota, network) or errors mid-take, the same
audio is endpointed locally on silence and transcribed with faster-whisper.
STT_ENGINE=whisper skips Scribe entirely.

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

import scribe

STT_ENGINE = os.environ.get("STT_ENGINE", "scribe").strip().lower()   # scribe | whisper
# While Scribe is deciding the turn, the local silence limit is only a safety
# net (Scribe's own silence backstop is STT_SCRIBE_VAD_SILENCE, 1.5s). If we
# reach this, we force a commit.
SCRIBE_LOCAL_SILENCE = float(os.environ.get("STT_SCRIBE_LOCAL_SILENCE", "4.0"))

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
# Hard ceiling on one utterance. With the VAD, a take normally ends on 0.7s of
# silence, so this only matters for long requests: 10s cut off a spoken meal
# log ("...30 grams of") mid-sentence. 30s fits a full meal or a list.
MAX_RECORD_SECONDS = float(os.environ.get("STT_MAX_RECORD_SECONDS", "30"))
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

    def __init__(self, threshold: float = 0.5, no_speech_timeout: float | None = None,
                 silence: float | None = None):
        self.threshold = threshold
        self.silence_chunks = int((SILENCE_DURATION if silence is None else silence) * _CPS)
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
                if self.silent >= self.silence_chunks:
                    return "silence"
        if not self.speech and self.n >= int(self.no_speech_timeout * _CPS):
            return "no_speech"
        return None



def pick_input_device(pa) -> tuple:
    """(index, name) of the mic to use. MIC_DEVICE (a name fragment) wins.
    Otherwise the system default -- unless that's a phone or Bluetooth headset
    (AirPods become the default input whenever they connect, and send pure
    silence while they're in the case), in which case the Mac's built-in mic,
    which is there docked or not."""
    want = os.environ.get("MIC_DEVICE", "").strip().lower()
    skip = [s.strip().lower() for s in os.environ.get(
        "MIC_SKIP", "airpods,iphone,beats,headphones,headset").split(",") if s.strip()]
    mics = [pa.get_device_info_by_index(i) for i in range(pa.get_device_count())]
    mics = [d for d in mics if d.get("maxInputChannels", 0) > 0]
    if want:
        for d in mics:
            if want in d["name"].lower():
                return d["index"], d["name"]
    try:
        default = pa.get_default_input_device_info()
        if not any(s in default["name"].lower() for s in skip):
            return default["index"], default["name"]
    except Exception:
        pass
    usable = [d for d in mics if not any(s in d["name"].lower() for s in skip)]
    for d in usable:
        if "macbook" in d["name"].lower() or "built-in" in d["name"].lower():
            return d["index"], d["name"]
    if usable:
        return usable[0]["index"], usable[0]["name"]
    return None, "system default"


class MicStream:
    """One always-open input stream. Keeps the last few seconds of RMS for the
    noise floor and the last PREROLL_SECONDS of audio for pre-roll."""

    def __init__(self):
        self._pa = pyaudio.PyAudio()
        idx, name = pick_input_device(self._pa)
        print(f"[STT] Mic: '{name}'")
        self._stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                                     input=True, input_device_index=idx,
                                     frames_per_buffer=CHUNK_SIZE)
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


def _wav(frames: list[bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    buf.seek(0)
    return buf


def _open_scribe(previous_text: str):
    if STT_ENGINE != "scribe" or not scribe.available():
        return None
    t = time.time()
    try:
        sess = scribe.Session(SAMPLE_RATE, previous_text=previous_text)
        print(f"[STT] Scribe connected in {time.time() - t:.2f}s")
        return sess
    except Exception as exc:
        print(f"[STT] Scribe unavailable ({type(exc).__name__}: {str(exc)[:160]}) — using Whisper")
        return None


def listen(preroll: bool = True, no_speech_timeout: float | None = None,
           previous_text: str = "") -> tuple[str, io.BytesIO, str]:
    """Record one utterance. Returns (text, wav, engine).

    `text` is Scribe's committed transcript, or "" when Whisper must
    transcribe `wav` (engine "whisper"), or "" with engine "none" when nobody
    spoke. `preroll` prepends the audio from just before the call (the words
    right after the wake word).
    """
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
    t0 = time.time()
    pre, q = _mic.start(preroll)     # the queue fills while Scribe connects
    sess = _open_scribe(previous_text)
    ep = Endpointer(threshold, no_speech_timeout,
                    silence=SCRIBE_LOCAL_SILENCE if sess else None)
    frames = list(pre)
    for c in pre:          # warm the VAD's context window; doesn't drive endpointing
        if scorer is not None:
            scorer(c)
        if sess:
            sess.send(c)
    why = "cap"
    try:
        for _ in range(int(MAX_RECORD_SECONDS * _CPS)):
            try:
                data = q.get(timeout=10)
            except queue.Empty:
                why = "mic stalled"
                break
            frames.append(data)
            if sess:
                sess.send(data)
                if sess.done.is_set():
                    if sess.text:
                        why = "scribe turn" if (sess.turn_prob or 0) >= 0.9 else "scribe silence"
                        break
                    # Scribe failed mid-take: finish this take on local silence.
                    print(f"[STT] Scribe error ({sess.error}) — finishing with Whisper")
                    sess.close()
                    sess = None
                    ep.silence_chunks = int(SILENCE_DURATION * _CPS)
            r = ep.feed(level(data))
            if r:
                why = r
                break
    finally:
        _mic.stop()

    engine = "whisper"
    text = ""
    if sess:
        if why in ("scribe turn", "scribe silence"):
            engine, text = "scribe", sess.text
        elif why == "no_speech":
            engine = "none"
        else:   # local safety net / cap: force a commit, take what Scribe has
            sess.finish()
            if sess.text:
                engine, text = "scribe", sess.text
            elif sess.error:
                print(f"[STT] Scribe error ({sess.error}) — using Whisper")
        sess.close()
    elif why == "no_speech":
        engine = "none"

    tp = f", turn_prob {sess.turn_prob:.2f}" if sess and sess.turn_prob is not None else ""
    print(f"[STT] Recorded {time.time() - t0:.1f}s (+{len(pre) / _CPS:.1f}s pre-roll), "
          f"stop={why}, engine={engine}{tp}, speech={'yes' if ep.speech else 'no'}, "
          f"{'vad' if scorer is not None else f'rms floor {floor:.0f}'} threshold {threshold:g}")
    return text, _wav(frames), engine


def _transcribe(text: str, buf: io.BytesIO, engine: str) -> str:
    if engine == "scribe":
        return text
    if engine == "none":
        return ""
    buf.seek(0)
    segments, _info = model.transcribe(buf, language="en", vad_filter=True)
    return " ".join(s.text for s in segments).strip()


def _last_reply() -> str:
    """Jarvis's last reply, as context for a follow-up (helps turn prediction
    tell an answer from a pause)."""
    if _r is None:
        return ""
    try:
        for raw in reversed(_r.lrange("conversation:default", -4, -1)):
            m = json.loads(raw)
            if m.get("role") == "assistant":
                return str(m.get("content", ""))[-500:]
    except Exception:
        pass
    return ""


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
    raw, audio_buf, engine = listen(preroll=False, no_speech_timeout=FOLLOWUP_WINDOW,
                                    previous_text=_last_reply())
    audio_path = cache_audio(audio_buf, room)
    text = _transcribe(raw, audio_buf, engine)

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

    raw, audio_buf, engine = listen(preroll=True)
    audio_path = cache_audio(audio_buf, room)
    text = _strip_wake(_transcribe(raw, audio_buf, engine))

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
