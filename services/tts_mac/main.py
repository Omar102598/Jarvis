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


def _strip_markdown(text: str) -> str:
    text = _MD_PATTERN.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = re.sub(r"\n{2,}", " ", text)
    return re.sub(r"\s+", " ", text).strip()


async def _speak_async(text: str) -> None:
    communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name
    try:
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
