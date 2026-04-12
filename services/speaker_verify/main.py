"""JARVIS Speaker Verification Service.

Receives speech events, verifies the speaker's identity against enrolled
embeddings in Redis, then forwards authorized requests to the LLM agent.

TODO: Currently passes through all requests. Full implementation requires
the raw audio to be shared (via file reference or binary MQTT payload)
alongside the transcription.
"""

import json
import os

import numpy as np
import paho.mqtt.client as mqtt
import redis

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
THRESHOLD = float(os.environ.get("VERIFY_THRESHOLD", "0.25"))

r = redis.Redis(host=REDIS_HOST)


def verify_speaker(embedding: np.ndarray, expected_name: str = "omar") -> tuple:
    """Compare embedding against stored speaker profile.

    Returns:
        (is_authorized, similarity_score)
    """
    stored = r.get(f"speaker:{expected_name}:embedding")
    if not stored:
        return False, 0.0

    dim_raw = r.get(f"speaker:{expected_name}:dim")
    dim = int(dim_raw) if dim_raw else 192
    stored_embed = np.frombuffer(stored, dtype=np.float32).reshape(dim)

    similarity = float(
        np.dot(stored_embed, embedding)
        / (np.linalg.norm(stored_embed) * np.linalg.norm(embedding))
    )
    return similarity > THRESHOLD, similarity


def on_speech(client, userdata, msg):
    """Handle transcribed speech: verify speaker, forward to LLM."""
    data = json.loads(msg.payload)
    room = data["room"]
    text = data["text"]

    # TODO: Implement actual speaker verification when raw audio is available.
    # For now, pass through all requests as verified.
    verified = True
    print(f"[Verify] {'VERIFIED' if verified else 'REJECTED'} from {room}: '{text}'")

    if verified:
        client.publish(
            "jarvis/llm/request",
            json.dumps({
                "text": text,
                "room": room,
                "verified": verified,
            }),
        )
    else:
        client.publish(
            f"jarvis/tts/{room}/speak",
            json.dumps({
                "text": "I'm sorry, I don't recognize your voice.",
                "room": room,
            }),
        )


def main():
    mqtt_client = mqtt.Client()
    mqtt_client.connect(MQTT_HOST, MQTT_PORT)
    mqtt_client.subscribe("jarvis/audio/mic/+/speech")
    mqtt_client.message_callback_add("jarvis/audio/mic/+/speech", on_speech)

    print(f"[Verify] Ready (threshold={THRESHOLD})")
    mqtt_client.loop_forever()


if __name__ == "__main__":
    main()
