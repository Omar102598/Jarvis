"""ElevenLabs Scribe realtime streaming, with semantic end-of-turn.

One WebSocket per utterance. Audio is streamed as it is captured; the server
decides when the turn is over (commit_strategy=turn_prediction: it commits once
a partial transcript's `turn_probability` reaches 0.9, and also after
`vad_silence_threshold_secs` of silence as a backstop). So "30 grams of…" keeps
the mic open, and "what time is it" ends quickly.

This module only talks to the API. main.py owns the mic, the local safety
limits, and the Whisper fallback, so this file is the same on the Mac and on
the VM.

Docs: elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime
and .../realtime/transcripts-and-commit-strategies.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from urllib.parse import urlencode

API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
URL = os.environ.get("STT_SCRIBE_URL", "wss://api.elevenlabs.io/v1/speech-to-text/realtime")
# turn_prediction exists only on scribe_v3_live; other models reject it.
MODEL = os.environ.get("STT_SCRIBE_MODEL", "scribe_v3_live")
COMMIT_STRATEGY = os.environ.get("STT_SCRIBE_COMMIT", "turn_prediction")
# The silence backstop when turn prediction isn't confident. Turn prediction
# usually commits well before this; it only decides how long a genuine
# mid-sentence pause can be. Docs allow 0.192-3.0.
VAD_SILENCE_SECS = os.environ.get("STT_SCRIBE_VAD_SILENCE", "1.5")
# Unset = the server's default. These are the knobs to turn if Scribe misses
# quiet speech (lower vad_threshold) or commits on breaths/clicks (raise
# min_speech_duration_ms).
VAD_THRESHOLD = os.environ.get("STT_SCRIBE_VAD_THRESHOLD", "")
MIN_SPEECH_MS = os.environ.get("STT_SCRIBE_MIN_SPEECH_MS", "")
MIN_SILENCE_MS = os.environ.get("STT_SCRIBE_MIN_SILENCE_MS", "")
# Server-side suppression of background voices (TV, music). Off by default
# until tested: it changes the VAD defaults (vad_threshold 0.15).
FILTER_BACKGROUND = os.environ.get("STT_SCRIBE_FILTER_BACKGROUND", "false").lower() in ("1", "true", "yes")
# Pin the language: auto-detection turned a short pause into Japanese ("はい。").
# "" = auto-detect.
LANGUAGE = os.environ.get("STT_SCRIBE_LANGUAGE", "en").strip()
CONNECT_TIMEOUT = float(os.environ.get("STT_SCRIBE_CONNECT_TIMEOUT", "3"))
# After an auth/quota error, skip Scribe for this long instead of paying a
# failed connection on every request.
COOLDOWN_SECS = float(os.environ.get("STT_SCRIBE_COOLDOWN", "600"))

_FATAL = {"auth_error", "quota_exceeded", "unaccepted_terms", "invalid_request",
          "resource_exhausted", "rate_limited"}
_cooldown_until = 0.0


def available() -> bool:
    if not API_KEY or COMMIT_STRATEGY == "off":
        return False
    try:
        import websockets.sync.client  # noqa: F401
    except Exception:
        return False
    return time.time() >= _cooldown_until


def _url(sample_rate: int) -> str:
    q = {"model_id": MODEL, "audio_format": f"pcm_{sample_rate}",
         "commit_strategy": COMMIT_STRATEGY, "vad_silence_threshold_secs": VAD_SILENCE_SECS}
    if LANGUAGE:
        q["language_code"] = LANGUAGE
    if VAD_THRESHOLD:
        q["vad_threshold"] = VAD_THRESHOLD
    if MIN_SPEECH_MS:
        q["min_speech_duration_ms"] = MIN_SPEECH_MS
    if MIN_SILENCE_MS:
        q["min_silence_duration_ms"] = MIN_SILENCE_MS
    if FILTER_BACKGROUND:
        q["filter_background_audio"] = "true"
    return f"{URL}?{urlencode(q)}"


class Session:
    """One utterance. Call send() per audio chunk; poll `.done`."""

    def __init__(self, sample_rate: int, previous_text: str = ""):
        from websockets.sync.client import connect
        self.sample_rate = sample_rate
        self.text = ""            # committed transcript
        self.partial = ""
        self.turn_prob = None     # last partial's turn_probability
        self.error = ""           # set when the server reports an error / closes
        self.done = threading.Event()
        self._prev = previous_text[:500]
        self._first = True
        self._lock = threading.Lock()
        self._ws = connect(_url(sample_rate), additional_headers={"xi-api-key": API_KEY},
                           open_timeout=CONNECT_TIMEOUT, close_timeout=1)
        threading.Thread(target=self._recv, daemon=True, name="scribe-recv").start()

    def _recv(self):
        global _cooldown_until
        try:
            for raw in self._ws:
                msg = json.loads(raw)
                kind = msg.get("message_type", "")
                if kind == "partial_transcript":
                    self.partial = msg.get("text", "")
                    if msg.get("turn_probability") is not None:
                        self.turn_prob = float(msg["turn_probability"])
                elif kind == "committed_transcript":
                    t = (msg.get("text") or "").strip()
                    if t:
                        self.text = t
                        self.done.set()
                        return
                elif "error" in msg or kind in _FATAL:
                    self.error = f"{kind}: {str(msg.get('error', ''))[:200]}"
                    if kind in _FATAL:
                        _cooldown_until = time.time() + COOLDOWN_SECS
                    self.done.set()
                    return
        except Exception as exc:
            if not self.done.is_set():
                self.error = f"connection: {type(exc).__name__}"
        if not self.done.is_set():
            self.error = self.error or "closed without a transcript"
            self.done.set()

    def send(self, pcm: bytes, commit: bool = False) -> None:
        if self.done.is_set():
            return
        msg = {"message_type": "input_audio_chunk",
               "audio_base_64": base64.b64encode(pcm).decode(),
               "commit": commit, "sample_rate": self.sample_rate}
        if self._first and self._prev:
            msg["previous_text"] = self._prev    # only allowed on the first chunk
        self._first = False
        with self._lock:
            try:
                self._ws.send(json.dumps(msg))
            except Exception as exc:
                self.error = self.error or f"send: {type(exc).__name__}"
                self.done.set()

    def finish(self, wait: float = 2.0) -> None:
        """Force a commit (local limit reached) and wait briefly for it."""
        if not self.done.is_set():
            self.send(b"", commit=True)
            self.done.wait(wait)

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass
