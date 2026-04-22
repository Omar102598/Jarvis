#!/usr/bin/env python3
"""Enroll a speaker's voice for JARVIS speaker verification.

Usage:
    python scripts/enroll_speaker.py --name omar --samples 5
"""

import argparse
import sys
import time

import numpy as np
import redis
import sounddevice as sd

SAMPLE_RATE = 16000
DURATION = 5  # seconds per sample


def record_sample(duration: int = DURATION) -> np.ndarray:
    """Record audio from the default microphone."""
    print(f"  Recording for {duration} seconds... speak now!")
    audio = sd.rec(
        int(duration * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    print("  Done.")
    return audio.flatten()


def main():
    parser = argparse.ArgumentParser(description="Enroll speaker voice for JARVIS")
    parser.add_argument("--name", required=True, help="Speaker name (e.g., omar)")
    parser.add_argument("--samples", type=int, default=5, help="Number of voice samples")
    parser.add_argument("--redis-host", default="localhost", help="Redis host")
    parser.add_argument("--redis-port", type=int, default=6379, help="Redis port")
    args = parser.parse_args()

    r = redis.Redis(host=args.redis_host, port=args.redis_port)

    try:
        r.ping()
    except redis.ConnectionError:
        print(f"Cannot connect to Redis at {args.redis_host}:{args.redis_port}")
        sys.exit(1)

    print(f"\n=== JARVIS Speaker Enrollment ===")
    print(f"Enrolling: {args.name}")
    print(f"Samples: {args.samples}")
    print(f"Say different phrases each time.\n")

    embeddings = []

    try:
        from speechbrain.inference.speaker import EncoderClassifier

        classifier = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir="models/speaker_model",
        )
    except ImportError:
        print("SpeechBrain not installed. Install with: pip install speechbrain")
        sys.exit(1)

    for i in range(args.samples):
        print(f"\nSample {i+1}/{args.samples}")
        input("  Press Enter when ready...")
        audio = record_sample()

        # Get embedding
        import torch
        signal = torch.tensor(audio).unsqueeze(0)
        embedding = classifier.encode_batch(signal).squeeze().numpy()
        embeddings.append(embedding)
        print(f"  Embedding shape: {embedding.shape}")

        if i < args.samples - 1:
            time.sleep(1)

    # Average embeddings for a more robust profile
    avg_embedding = np.mean(embeddings, axis=0).astype(np.float32)

    # Store in Redis
    key = f"speaker:{args.name}:embedding"
    r.set(key, avg_embedding.tobytes())
    r.set(f"speaker:{args.name}:dim", str(avg_embedding.shape[0]))

    print(f"\n✓ Enrolled '{args.name}' with {args.samples} samples")
    print(f"  Stored as Redis key: {key}")
    print(f"  Embedding dim: {avg_embedding.shape[0]}")


if __name__ == "__main__":
    main()
