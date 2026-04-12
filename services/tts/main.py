"""JARVIS Text-to-Speech service.

Uses Piper for fast confirmations and can be extended with
Coqui XTTS v2 for voice-cloned high-quality speech.
"""

import io
import json
import os
import subprocess
import tempfile

import paho.mqtt.client as mqtt
import soundfile as sf

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
PIPER_MODEL = os.environ.get("PIPER_MODEL", "en_US-lessac-medium")


def synthesize_piper(text: str) -> bytes:
    """Synthesize speech using Piper TTS (fast, local)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        proc = subprocess.run(
            [
                "piper",
                "--model", PIPER_MODEL,
                "--output_file", tmp_path,
            ],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            print(f"[TTS] Piper error: {proc.stderr.decode()}")
            return b""

        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def on_connect(client, userdata, flags, rc):
    print(f"[TTS] Connected to MQTT (rc={rc})")
    client.subscribe("jarvis/tts/speak")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        text = payload.get("text", "")
        if not text:
            return

        print(f"[TTS] Synthesizing: {text[:80]}...")
        audio_bytes = synthesize_piper(text)

        if audio_bytes:
            # Publish audio back for playback
            client.publish(
                "jarvis/audio/playback",
                audio_bytes,
            )
            print(f"[TTS] Published {len(audio_bytes)} bytes of audio")
        else:
            print("[TTS] Synthesis failed — no audio produced")

    except Exception as e:
        print(f"[TTS] Error: {e}")


def main():
    print("[TTS] Starting JARVIS TTS service...")
    print(f"[TTS] Piper model: {PIPER_MODEL}")

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
