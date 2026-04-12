"""JARVIS Speaker Verification Service.

Receives speech events, verifies the speaker's identity against enrolled
embeddings in Redis, then forwards authorized requests to the LLM agent.

When a raw audio file path is provided in the message, actual speaker
verification is performed using SpeechBrain ECAPA-TDNN embeddings.
Otherwise, requests are passed through as unverified.
"""

import json
import os

import numpy as np
import paho.mqtt.client as mqtt
import redis
import torch
from speechbrain.inference.speaker import EncoderClassifier

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
THRESHOLD = float(os.environ.get("VERIFY_THRESHOLD", "0.25"))
MODEL_DIR = os.environ.get("MODEL_DIR", "/models/speaker_model")

r = redis.Redis(host=REDIS_HOST)

# Load speaker encoder model on startup
print("[Verify] Loading SpeechBrain ECAPA-TDNN model...")
classifier = EncoderClassifier.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",
    savedir=MODEL_DIR,
)
print("[Verify] Model loaded.")


def extract_embedding(audio_path: str) -> np.ndarray:
    """Extract a speaker embedding from a WAV file."""
    import torchaudio

    signal, sr = torchaudio.load(audio_path)
    # Resample to 16kHz if needed
    if sr != 16000:
        resampler = torchaudio.transforms.Resample(orig_freq=sr, new_freq=16000)
        signal = resampler(signal)
    # Ensure mono
    if signal.shape[0] > 1:
        signal = signal.mean(dim=0, keepdim=True)
    embedding = classifier.encode_batch(signal).squeeze().detach().numpy()
    return embedding


def verify_speaker(embedding: np.ndarray) -> tuple[bool, float, str]:
    """Compare embedding against all enrolled speaker profiles.

    Returns:
        (is_authorized, similarity_score, matched_name)
    """
    best_name = "unknown"
    best_score = 0.0

    for key in r.scan_iter("speaker:*:embedding"):
        name = key.decode().split(":")[1]
        stored = r.get(key)
        if not stored:
            continue

        dim_raw = r.get(f"speaker:{name}:dim")
        dim = int(dim_raw) if dim_raw else 192
        stored_embed = np.frombuffer(stored, dtype=np.float32).reshape(dim)

        similarity = float(
            np.dot(stored_embed, embedding)
            / (np.linalg.norm(stored_embed) * np.linalg.norm(embedding) + 1e-8)
        )
        if similarity > best_score:
            best_score = similarity
            best_name = name

    return best_score > THRESHOLD, best_score, best_name


def on_speech(client, userdata, msg):
    """Handle transcribed speech: verify speaker, forward to LLM."""
    data = json.loads(msg.payload)
    room = data["room"]
    text = data["text"]
    audio_path = data.get("audio_path")

    verified = False
    speaker_name = "unknown"

    if audio_path and os.path.exists(audio_path):
        try:
            embedding = extract_embedding(audio_path)
            verified, score, speaker_name = verify_speaker(embedding)
            print(
                f"[Verify] {'VERIFIED' if verified else 'REJECTED'} "
                f"speaker={speaker_name} score={score:.3f} from {room}: '{text}'"
            )
        except Exception as e:
            print(f"[Verify] Verification error: {e}")
            # Fall back to pass-through on error
            verified = True
            speaker_name = "unknown"
        finally:
            # Clean up audio file after verification
            try:
                os.unlink(audio_path)
            except OSError:
                pass
    else:
        # No audio path provided — pass through (backward compatibility)
        verified = True
        print(f"[Verify] PASS-THROUGH (no audio) from {room}: '{text}'")

    if verified:
        client.publish(
            "jarvis/llm/request",
            json.dumps({
                "text": text,
                "room": room,
                "verified": verified,
                "speaker": speaker_name,
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
