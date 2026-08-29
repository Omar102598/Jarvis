"""JARVIS Speaker Verification Service.

Receives speech events, verifies the speaker's identity against enrolled
embeddings in Redis, then forwards authorized requests to the LLM agent.

The STT service writes the raw command audio to a shared volume and includes
its path in the MQTT payload (audio_path). This service loads that audio,
extracts an ECAPA-TDNN embedding, and compares it against every enrolled
speaker profile.

Modes (VERIFY_MODE):
    off     — skip verification entirely, forward everything (default when
              no speakers are enrolled)
    log     — verify and log the result, but forward regardless (default)
    enforce — reject requests whose speaker doesn't match an enrolled profile
"""

import json
import os
import time

import numpy as np
import paho.mqtt.client as mqtt
import redis

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
THRESHOLD = float(os.environ.get("VERIFY_THRESHOLD", "0.25"))
VERIFY_MODE = os.environ.get("VERIFY_MODE", "log").lower()
MODEL_DIR = os.environ.get("MODEL_DIR", "/models/speaker_model")

# Was defaulting to 6379 (port ignored) → "connection refused" spam and broken
# verification, since Redis is exposed on 6380 for native services.
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)

_classifier = None


def get_classifier():
    """Lazily load the ECAPA-TDNN encoder (downloads on first use)."""
    global _classifier
    if _classifier is None:
        from speechbrain.inference.speaker import EncoderClassifier

        print("[Verify] Loading ECAPA-TDNN speaker model...")
        _classifier = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=MODEL_DIR,
        )
        print("[Verify] Model loaded.")
    return _classifier


def extract_embedding(audio_path: str) -> np.ndarray:
    """Compute the speaker embedding for a WAV file."""
    import torch
    import torchaudio

    signal, sample_rate = torchaudio.load(audio_path)
    if sample_rate != 16000:
        signal = torchaudio.functional.resample(signal, sample_rate, 16000)
    if signal.shape[0] > 1:
        signal = signal.mean(dim=0, keepdim=True)

    with torch.no_grad():
        embedding = get_classifier().encode_batch(signal).squeeze().cpu().numpy()
    return embedding.astype(np.float32)


def load_enrolled_speakers() -> dict:
    """Return {name: embedding} for every enrolled speaker in Redis."""
    speakers = {}
    for key in r.scan_iter("speaker:*:embedding"):
        name = key.decode().split(":")[1]
        stored = r.get(key)
        dim_raw = r.get(f"speaker:{name}:dim")
        dim = int(dim_raw) if dim_raw else 192
        speakers[name] = np.frombuffer(stored, dtype=np.float32).reshape(dim)
    return speakers


def verify_speaker(audio_path: str) -> tuple:
    """Match the audio against all enrolled profiles.

    Returns:
        (is_authorized, best_match_name, best_similarity)
    """
    speakers = load_enrolled_speakers()
    if not speakers:
        return True, None, 0.0  # nothing enrolled — open access

    embedding = extract_embedding(audio_path)

    best_name, best_sim = None, -1.0
    for name, stored in speakers.items():
        sim = float(
            np.dot(stored, embedding)
            / (np.linalg.norm(stored) * np.linalg.norm(embedding) + 1e-8)
        )
        if sim > best_sim:
            best_name, best_sim = name, sim

    return best_sim > THRESHOLD, best_name, best_sim


def on_speech(client, userdata, msg):
    """Handle transcribed speech: verify speaker, forward to LLM."""
    data = json.loads(msg.payload)
    room = data["room"]
    text = data["text"]
    audio_path = data.get("audio_path", "")

    verified, speaker, score = True, None, 0.0

    if VERIFY_MODE != "off" and audio_path and os.path.exists(audio_path):
        try:
            verified, speaker, score = verify_speaker(audio_path)
            print(
                f"[Verify] {'VERIFIED' if verified else 'REJECTED'} "
                f"speaker={speaker} score={score:.3f} from {room}: '{text}'"
            )
        except Exception as e:
            # Never let verification failures take down the pipeline
            print(f"[Verify] Verification error ({e}), passing through")
            verified = True
        finally:
            # Clean up audio file after verification
            try:
                os.unlink(audio_path)
            except OSError:
                pass
    else:
        print(f"[Verify] No audio to verify (mode={VERIFY_MODE}), passing: '{text}'")

    if verified or VERIFY_MODE != "enforce":
        client.publish(
            "jarvis/llm/request",
            json.dumps({
                "text": text,
                "room": room,
                "verified": verified,
                "speaker": speaker,
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

    # Subscribe in on_connect so a mosquitto restart re-subscribes automatically
    # (subscribing once in main left the bridge connected but deaf after a
    # broker bounce — transcripts never reached the brain).
    def on_connect(client, userdata, flags, rc):
        client.subscribe("jarvis/audio/mic/+/speech")
        print(f"[Verify] MQTT connected (rc={rc}), subscription active.")

    mqtt_client.on_connect = on_connect
    mqtt_client.message_callback_add("jarvis/audio/mic/+/speech", on_speech)
    # Back off between reconnect attempts rather than hammering a broker
    # that is still coming back up.
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    mqtt_client.connect(MQTT_HOST, MQTT_PORT)

    print(f"[Verify] Ready (mode={VERIFY_MODE}, threshold={THRESHOLD})")
    # A broker restart or network blip used to raise straight out of
    # loop_forever and kill this process — the whole voice stack then stayed
    # dead until someone noticed. Keep retrying instead; paho reconnects and
    # re-subscribes through its on_connect handler.
    while True:
        try:
            mqtt_client.loop_forever(retry_first_connection=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[mqtt] connection lost ({exc}) — retrying in 5s", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
