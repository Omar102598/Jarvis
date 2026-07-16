"""JARVIS TTS for macOS — Microsoft Edge TTS (neural voices).

Subscribes to jarvis/tts/+/speak and uses edge-tts to synthesise speech
through the Mac speakers. Defaults to en-GB-RyanNeural — a deep British male
voice that sounds close to the Iron Man Jarvis.

Audio playback runs in a dedicated worker thread so the MQTT loop is never
blocked. This prevents keepalive timeouts on long multi-sentence responses and
ensures the /done signal fires only after the last sentence has fully played.

Environment variables:
  TTS_VOICE   — edge-tts voice name (default: en-GB-RyanNeural)
  TTS_RATE    — speech rate offset, e.g. +0% / -10% (default: -5%)
  TTS_PITCH   — pitch offset, e.g. -5Hz (default: -5Hz, deeper)
"""

import asyncio
import json
import os
import queue
import re
import subprocess
import tempfile
import threading

import edge_tts
import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
TTS_VOICE = os.environ.get("TTS_VOICE", "en-GB-RyanNeural")
TTS_RATE  = os.environ.get("TTS_RATE",  "-5%")
TTS_PITCH = os.environ.get("TTS_PITCH", "-5Hz")

_MD_PATTERN = re.compile(
    r"\*{1,3}(.+?)\*{1,3}"    # bold / italic / bold-italic
    r"|`{1,3}[^`]*`{1,3}"     # inline code / code blocks
    r"|#{1,6}\s*"              # markdown headers
    r"|\[([^\]]*)\]\([^)]*\)" # [text](url)
    r"|>\s*"                   # blockquotes
    r"|-{3,}|_{3,}|\*{3,}"    # hr / horizontal rules
    , re.DOTALL
)


# Emoji / pictographs / dingbats / symbols that TTS would otherwise read aloud by
# name ("money bag", "check mark") — stripped for SPEECH only (display keeps them).
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoji, symbols & pictographs, supplemental
    "\U00002600-\U000027BF"   # miscellaneous symbols + dingbats
    "\U0001F1E6-\U0001F1FF"   # regional indicators
    "\U00002190-\U000021FF"   # arrows
    "\U00002B00-\U00002BFF"   # misc symbols and arrows
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "]+",
    flags=re.UNICODE,
)
# Box-drawing / block / bullet characters used in agent reports (─ ═ • ✓ ✦ etc.)
_DECOR_PATTERN = re.compile(r"[─-╿▀-▟■-◿•‣⁃∙·✓✗✔✘✦✧▶►]")


def _strip_markdown(text: str) -> str:
    """Clean text for SPEECH: strip markdown, emoji, and report decoration so the
    voice never reads 'asterisk' or emoji names aloud. Display paths are untouched."""
    text = _MD_PATTERN.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = _EMOJI_PATTERN.sub("", text)
    text = _DECOR_PATTERN.sub(" ", text)
    text = re.sub(r"[*_`~#>|]", "", text)     # residual / unpaired markdown symbols
    text = re.sub(r"\n{2,}", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# ElevenLabs — premium voice tier (optional). Chain: ElevenLabs → edge-tts
# (Ryan) → nothing. Quota/auth failures trip a 10-minute cooldown so we don't
# hammer a drained account on every sentence.
# ---------------------------------------------------------------------------
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")  # "Daniel" — deep British
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")
_el_down_until = 0.0


async def _synth_elevenlabs(text: str, out_path: str) -> bool:
    """Write ElevenLabs mp3 to out_path. False → caller falls back to edge-tts."""
    global _el_down_until
    import time as _time
    if not ELEVENLABS_API_KEY:
        return False   # unconfigured — edge-tts is the intended voice
    if _time.time() < _el_down_until:
        print("[TTS] ElevenLabs in cooldown — using Ryan")
        return False
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
                json={"text": text, "model_id": ELEVENLABS_MODEL,
                      "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status in (401, 402, 429):
                    print(f"[TTS] ElevenLabs quota/auth ({r.status}) — "
                          "using Ryan for the next 10 minutes")
                    _el_down_until = _time.time() + 600
                    return False
                if r.status != 200:
                    print(f"[TTS] ElevenLabs error {r.status} — falling back")
                    return False
                data = await r.read()
        with open(out_path, "wb") as f:
            f.write(data)
        print("[TTS] via ElevenLabs (Daniel)")
        return True
    except Exception as e:
        print(f"[TTS] ElevenLabs failed ({e}) — falling back")
        return False


async def _speak_async(text: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name
    try:
        if not await _synth_elevenlabs(text, tmp_path):
            communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
            await communicate.save(tmp_path)
        subprocess.run(["afplay", tmp_path], check=False, timeout=180)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def speak(text: str) -> None:
    asyncio.run(_speak_async(text))


# ---------------------------------------------------------------------------
# Worker queue — decouples MQTT receive from blocking audio playback
# ---------------------------------------------------------------------------

# Each item: (mqtt_client, room, text, is_final)
_tts_queue: queue.Queue = queue.Queue()


def _tts_worker() -> None:
    """Drain the TTS queue sequentially; publish /done after the last sentence."""
    while True:
        try:
            mqtt_client, room, text, is_final = _tts_queue.get()
            if text:
                print(f"[TTS] Speaking (is_final={is_final}): {text[:80]}...")
                speak(text)
            # Only signal after the very last sentence so STT opens the follow-up
            # window only after the complete response has been spoken.
            if is_final:
                # Small settling pause to let room acoustics clear before mic opens
                import time; time.sleep(0.4)
                mqtt_client.publish(
                    f"jarvis/tts/{room}/done",
                    json.dumps({"room": room}),
                )
                print(f"[TTS] /done published for room '{room}'")
        except Exception as e:
            print(f"[TTS] Worker error: {e}")
        finally:
            _tts_queue.task_done()


# Start the worker thread (daemon so it exits with the process)
threading.Thread(target=_tts_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# MQTT callbacks
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, rc):
    print(f"[TTS] Connected (rc={rc}), voice={TTS_VOICE}")
    client.subscribe("jarvis/tts/+/speak")


def on_message(client, userdata, msg):
    """Enqueue the TTS job — never block the MQTT loop."""
    try:
        payload  = json.loads(msg.payload.decode())
        raw      = payload.get("text", "").strip()
        room     = payload.get("room", "office")
        is_final = payload.get("is_final", True)
        # This speaker serves PHYSICAL rooms only. mobile-*/glasses-*/siri-*
        # rooms are phone/HUD/Siri surfaces — the mobile_gateway (or Siri
        # itself) handles their audio; the Mac echoing them aloud was a
        # wildcard-subscription leak.
        if room.startswith(("mobile-", "glasses-", "siri-")):
            return
        if raw:
            clean = _strip_markdown(raw)
            _tts_queue.put((client, room, clean, is_final))
        elif is_final:
            # Empty final message — still need to publish /done
            _tts_queue.put((client, room, "", True))
    except Exception as e:
        print(f"[TTS] on_message error: {e}")


def main():
    print(f"[TTS] Starting (voice={TTS_VOICE}, rate={TTS_RATE}, pitch={TTS_PITCH})...")
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=120)
    client.loop_forever()


if __name__ == "__main__":
    main()
