#!/usr/bin/env python3
"""JARVIS room satellite — per-room ears for a Raspberry Pi (Zero 2 W +
ReSpeaker 2-Mics HAT or any USB mic/speaker).

Architecture: the satellite only does WAKE WORD + RECORD + PLAY. Everything
heavy (Whisper STT, the brain, TTS) runs on the Jarvis server via the mobile
gateway's /ask/audio endpoint — so a $15 Pi Zero 2 W is plenty.

    mic → openWakeWord ("hey jarvis") → beep → record until silence
        → POST /ask/audio (multipart: audio + room=satellite-<ROOM>)
        → play the WAV reply → short follow-up listen → back to wake word

The room field makes replies route as THIS room's voice surface (the Mac's
tts_mac ignores satellite-* rooms, so audio only plays here).

Config (env or /etc/jarvis-satellite.env via systemd):
    GATEWAY_URL     e.g. http://<mac-lan-ip>:8080  (or the Tailscale URL)
    MOBILE_API_KEY  same key the iOS app uses (empty if auth disabled)
    JARVIS_ROOM     room name, e.g. livingroom / bedroom / kitchen
    WAKE_THRESHOLD  openWakeWord score threshold (default 0.5)

Install on the Pi (see satellite/README.md for full setup):
    pip install openwakeword sounddevice soundfile numpy requests
"""

from __future__ import annotations

import io
import os
import time
import wave

import numpy as np
import requests
import sounddevice as sd

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.environ.get("MOBILE_API_KEY", "")
ROOM = os.environ.get("JARVIS_ROOM", "satellite").strip().lower().replace(" ", "")
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))

SAMPLE_RATE = 16000
CHUNK = 1280                      # 80 ms frames — what openWakeWord expects
SILENCE_RMS = int(os.environ.get("SILENCE_RMS", "300"))
SILENCE_SECS = float(os.environ.get("SILENCE_SECS", "1.2"))
MAX_RECORD_SECS = 15
FOLLOWUP_WINDOW = float(os.environ.get("FOLLOWUP_WINDOW", "6.0"))


def log(msg: str) -> None:
    print(f"[Satellite:{ROOM}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def beep(freq=880, dur=0.12) -> None:
    t = np.linspace(0, dur, int(SAMPLE_RATE * dur), False)
    tone = (0.25 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    try:
        sd.play(tone, SAMPLE_RATE, blocking=True)
    except Exception:
        pass


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))


def record_command(initial_grace=1.5) -> np.ndarray | None:
    """Record int16 mono until SILENCE_SECS of quiet (None if no speech)."""
    frames: list[np.ndarray] = []
    quiet = 0.0
    spoke = False
    start = time.time()
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK) as stream:
        while True:
            data, _ = stream.read(CHUNK)
            frame = data.reshape(-1)
            frames.append(frame)
            elapsed = time.time() - start
            level = rms(frame)
            if level >= SILENCE_RMS:
                spoke = True
                quiet = 0.0
            elif elapsed > initial_grace:
                quiet += CHUNK / SAMPLE_RATE
                if spoke and quiet >= SILENCE_SECS:
                    break
                if not spoke and elapsed > 5.0:
                    return None           # nobody said anything
            if elapsed > MAX_RECORD_SECS:
                break
    return np.concatenate(frames) if spoke else None


def to_wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


def play_wav_bytes(data: bytes) -> None:
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            sr = w.getframerate()
            n = w.getnframes()
            pcm = np.frombuffer(w.readframes(n), dtype=np.int16)
            if w.getnchannels() == 2:
                pcm = pcm.reshape(-1, 2)
        sd.play(pcm, sr, blocking=True)
    except Exception as exc:
        log(f"playback failed: {exc}")


# ---------------------------------------------------------------------------
# Gateway round-trip
# ---------------------------------------------------------------------------

def ask_jarvis(audio: np.ndarray) -> bool:
    """POST the recording; play the spoken reply. True if a reply played."""
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    try:
        resp = requests.post(
            f"{GATEWAY_URL}/ask/audio",
            headers=headers,
            files={"audio": ("command.wav", to_wav_bytes(audio), "audio/wav")},
            data={"room": f"satellite-{ROOM}"},
            timeout=120,
        )
    except Exception as exc:
        log(f"gateway unreachable: {exc}")
        return False
    if resp.status_code == 422:
        log("no speech detected by server")
        return False
    if resp.status_code != 200:
        log(f"gateway error {resp.status_code}: {resp.text[:120]}")
        return False
    play_wav_bytes(resp.content)
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    from openwakeword.model import Model

    log(f"loading wake-word model… (gateway={GATEWAY_URL})")
    oww = Model(wakeword_models=["hey_jarvis"], inference_framework="tflite")
    log("listening for 'hey jarvis'")

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK) as stream:
        while True:
            data, _ = stream.read(CHUNK)
            frame = data.reshape(-1)
            score = oww.predict(frame).get("hey_jarvis", 0.0)
            if score < WAKE_THRESHOLD:
                continue

            log(f"wake word (score {score:.2f})")
            oww.reset()
            # Release the wake stream while recording/playing (one device user)
            stream.stop()
            try:
                beep()
                audio = record_command()
                if audio is not None:
                    replied = ask_jarvis(audio)
                    # Follow-up window: talk again without the wake word.
                    while replied and FOLLOWUP_WINDOW > 0:
                        beep(660, 0.08)
                        follow = record_command(initial_grace=FOLLOWUP_WINDOW)
                        replied = ask_jarvis(follow) if follow is not None else False
                else:
                    log("no speech after wake")
            finally:
                stream.start()


if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log(f"crashed ({exc}) — restarting in 5s")
            time.sleep(5)
