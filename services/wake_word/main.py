"""JARVIS Wake Word Detection Service.

Listens continuously on the microphone for "Hey Jarvis" (or custom wake words).
On detection, publishes an event to MQTT so downstream services can begin processing.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import numpy as np
import paho.mqtt.client as mqtt
import pyaudio
import redis
from openwakeword.model import Model

MQTT_HOST    = os.environ.get("MQTT_HOST",    "localhost")
MQTT_PORT    = int(os.environ.get("MQTT_PORT",    "1883"))
REDIS_HOST   = os.environ.get("REDIS_HOST",   "localhost")
REDIS_PORT   = int(os.environ.get("REDIS_PORT",   "6380"))
ROOM         = os.environ.get("ROOM_NAME",    "default")
THRESHOLD    = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
COOLDOWN     = float(os.environ.get("WAKE_COOLDOWN",  "8.0"))

SAMPLE_RATE = 16000
CHUNK_SIZE = 1280  # 80ms at 16kHz


def main():
    oww_model = Model(
        wakeword_models=["hey jarvis"],
        inference_framework="onnx",
    )

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    mqtt_client = mqtt.Client()
    # Back off between reconnect attempts instead of hammering a broker that is
    # still coming back up.
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT)
    except Exception as exc:
        # The mic is the point of this process — start listening even if the
        # broker is down, and let the supervisor below reconnect underneath us.
        # Exiting here is how the whole voice stack used to stay dead after an
        # outage.
        print(f"[WakeWord] MQTT unavailable at startup ({exc}) — will keep retrying",
              flush=True)

    # Supervise the network loop instead of calling loop_start() and trusting it.
    # reconnect_delay_set only covers a clean disconnect; when a read times out
    # mid-packet the exception escapes paho's loop thread and that thread simply
    # dies. The process stays alive and the mic keeps detecting, so nothing looks
    # broken — but every publish lands on a dead client and the rest of the voice
    # stack never hears a wake word. That is exactly what happened on
    # 2026-10-20: "TimeoutError: [Errno 60] Operation timed out" in _sock_recv,
    # after which wake_word.log kept logging detections for 14 hours while STT
    # recorded nothing. Same supervised-retry shape STT already uses.
    def _mqtt_supervisor():
        while True:
            try:
                mqtt_client.loop_forever(retry_first_connection=True)
            except Exception as exc:
                print(f"[WakeWord] MQTT loop died ({exc}) — retrying in 5s",
                      flush=True)
                time.sleep(5)

    threading.Thread(target=_mqtt_supervisor, daemon=True).start()

    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    print(f"[WakeWord] Listening in '{ROOM}' (threshold={THRESHOLD}, cooldown={COOLDOWN}s)...")

    last_trigger = 0.0

    try:
        while True:
            # exception_on_overflow=False drops frames instead of crashing
            raw = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            audio_frame = np.frombuffer(raw, dtype=np.int16)

            now = time.monotonic()
            if now - last_trigger < COOLDOWN:
                # In cooldown — feed frames to keep model state fresh but skip publish
                oww_model.predict(audio_frame)
                continue

            prediction = oww_model.predict(audio_frame)

            for model_name, score in prediction.items():
                if score > THRESHOLD:
                    print(f"[WakeWord] Detected '{model_name}' (score: {score:.3f})")
                    last_trigger = now
                    oww_model.reset()

                    # Signal dashboard: user is speaking to Jarvis
                    try:
                        r.set(f"jarvis:voice:state:{ROOM}", "listening", ex=20)
                    except Exception:
                        pass

                    # A failed publish must not take down the listener; the
                    # next wake word should still get a chance.
                    try:
                        mqtt_client.publish(
                            f"jarvis/audio/mic/{ROOM}/wake_word",
                            json.dumps({
                                "model": model_name,
                                "score": float(score),
                                "room": ROOM,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }),
                        )
                    except Exception as exc:
                        print(f"[WakeWord] publish failed ({exc})", flush=True)
                    break  # one event per detection window

    except KeyboardInterrupt:
        print("[WakeWord] Shutting down...")
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except OSError:
            pass
        audio.terminate()
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
