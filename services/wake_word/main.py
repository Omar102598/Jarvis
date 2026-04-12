"""JARVIS Wake Word Detection Service.

Listens continuously on the microphone for "Hey Jarvis" (or custom wake words).
On detection, publishes an event to MQTT so downstream services can begin processing.
"""

import json
import os
from datetime import datetime, timezone

import numpy as np
import paho.mqtt.client as mqtt
import pyaudio
from openwakeword.model import Model

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
ROOM = os.environ.get("ROOM_NAME", "default")
THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))

SAMPLE_RATE = 16000
CHUNK_SIZE = 1280  # 80ms at 16kHz


def main():
    # Load wake word models — "hey jarvis" is built-in
    oww_model = Model(
        wakeword_models=["hey jarvis"],
        inference_framework="onnx",
    )

    # MQTT connection
    mqtt_client = mqtt.Client()
    mqtt_client.connect(MQTT_HOST, MQTT_PORT)
    mqtt_client.loop_start()

    # Open audio stream
    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    print(f"[WakeWord] Listening in '{ROOM}' (threshold={THRESHOLD})...")

    try:
        while True:
            audio_frame = np.frombuffer(stream.read(CHUNK_SIZE), dtype=np.int16)
            prediction = oww_model.predict(audio_frame)

            for model_name, score in prediction.items():
                if score > THRESHOLD:
                    print(f"[WakeWord] Detected '{model_name}' (score: {score:.3f})")

                    mqtt_client.publish(
                        f"jarvis/audio/mic/{ROOM}/wake_word",
                        json.dumps({
                            "model": model_name,
                            "score": float(score),
                            "room": ROOM,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }),
                    )
                    oww_model.reset()
    except KeyboardInterrupt:
        print("[WakeWord] Shutting down...")
    finally:
        stream.stop_stream()
        stream.close()
        audio.terminate()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
