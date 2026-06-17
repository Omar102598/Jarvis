"""JARVIS TTS for macOS — Microsoft Edge TTS (neural voices).

Subscribes to jarvis/tts/+/speak and uses edge-tts to synthesise speech
through the Mac speakers. Defaults to en-GB-RyanNeural — a deep British male
voice that sounds close to the Iron Man Jarvis.

Environment variables:
  TTS_VOICE   — edge-tts voice name (default: en-GB-RyanNeural)
  TTS_RATE    — speech rate offset, e.g. +0% / -10% (default: -5%)
  TTS_PITCH   — pitch offset, e.g. -5Hz (default: -5Hz, deeper)
"""

import asyncio
import json
import os
import re
import subprocess
import tempfile

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
    """Remove common markdown syntax so TTS reads cleanly."""
    text = _MD_PATTERN.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = re.sub(r"\n{2,}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def _speak_async(text: str) -> None:
    """Generate audio with edge-tts and play via afplay."""
    communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name
    try:
        await communicate.save(tmp_path)
        subprocess.run(["afplay", tmp_path], check=False, timeout=120)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def speak(text: str) -> None:
    asyncio.run(_speak_async(text))


def on_connect(client, userdata, flags, rc):
    print(f"[TTS] Connected (rc={rc}), voice={TTS_VOICE}")
    client.subscribe("jarvis/tts/+/speak")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        raw = payload.get("text", "").strip()
        room = payload.get("room", "office")
        if raw:
            clean = _strip_markdown(raw)
            print(f"[TTS] Speaking: {clean[:80]}...")
            speak(clean)
            # Signal STT that TTS is done so it can listen for a follow-up
            client.publish(
                f"jarvis/tts/{room}/done",
                json.dumps({"room": room}),
            )
    except Exception as e:
        print(f"[TTS] Error: {e}")


def main():
    print(f"[TTS] Starting (voice={TTS_VOICE}, rate={TTS_RATE}, pitch={TTS_PITCH})...")
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
