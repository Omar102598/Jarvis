"""JARVIS Audio Player — macOS native TTS playback.

Subscribes to jarvis/audio/playback, receives WAV bytes published by the TTS
service, writes them to a temp file, and plays them through macOS speakers via
afplay (built-in, no extra deps).
"""

import os
import subprocess
import tempfile

import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
VOLUME = float(os.environ.get("PLAYER_VOLUME", "1.0"))  # 0.0–1.0


def on_connect(client, userdata, flags, rc):
    print(f"[Player] Connected to MQTT (rc={rc})")
    client.subscribe("jarvis/audio/playback")


def on_message(client, userdata, msg):
    audio_bytes = msg.payload
    if not audio_bytes:
        return

    print(f"[Player] Playing {len(audio_bytes)} bytes of audio...")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        vol_arg = str(int(VOLUME * 255))  # afplay --volume is 0–255
        subprocess.run(
            ["afplay", "--volume", vol_arg, tmp_path],
            check=False,
            timeout=60,
        )
    except FileNotFoundError:
        print("[Player] afplay not found — are you on macOS?")
    except subprocess.TimeoutExpired:
        print("[Player] Playback timed out")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def main():
    print("[Player] JARVIS Audio Player starting (macOS afplay)...")
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
