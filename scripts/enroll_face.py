#!/usr/bin/env python3
"""Enroll a face for JARVIS face recognition.

Usage:
    python scripts/enroll_face.py --name omar --images data/face_enrollment/omar/
"""

import argparse
import os
import sys

import cv2
import numpy as np
import redis
from insightface.app import FaceAnalysis


def main():
    parser = argparse.ArgumentParser(description="Enroll face for JARVIS recognition")
    parser.add_argument("--name", required=True, help="Person's name")
    parser.add_argument("--images", required=True, help="Directory of face images")
    parser.add_argument("--redis-host", default="localhost", help="Redis host")
    parser.add_argument("--redis-port", type=int, default=6379, help="Redis port")
    args = parser.parse_args()

    if not os.path.isdir(args.images):
        print(f"Directory not found: {args.images}")
        sys.exit(1)

    r = redis.Redis(host=args.redis_host, port=args.redis_port)
    try:
        r.ping()
    except redis.ConnectionError:
        print(f"Cannot connect to Redis at {args.redis_host}:{args.redis_port}")
        sys.exit(1)

    print(f"\n=== JARVIS Face Enrollment ===")
    print(f"Enrolling: {args.name}")
    print(f"Image dir: {args.images}\n")

    # Initialize InsightFace
    app = FaceAnalysis(
        name="buffalo_l",
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    app.prepare(ctx_id=0, det_size=(640, 640))

    embeddings = []
    image_files = [
        f for f in os.listdir(args.images)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ]

    if not image_files:
        print(f"No image files found in {args.images}")
        sys.exit(1)

    print(f"Found {len(image_files)} images")

    for img_file in image_files:
        img_path = os.path.join(args.images, img_file)
        img = cv2.imread(img_path)
        if img is None:
            print(f"  Skipping (unreadable): {img_file}")
            continue

        faces = app.get(img)
        if len(faces) == 0:
            print(f"  Skipping (no face): {img_file}")
            continue
        if len(faces) > 1:
            print(f"  Warning: {len(faces)} faces in {img_file}, using largest")
            faces = sorted(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]), reverse=True)

        embeddings.append(faces[0].embedding)
        print(f"  ✓ {img_file}")

    if not embeddings:
        print("\nNo valid face embeddings extracted.")
        sys.exit(1)

    # Average embeddings
    avg_embedding = np.mean(embeddings, axis=0).astype(np.float32)

    # Store in Redis
    key = f"face:{args.name}"
    r.set(key, avg_embedding.tobytes())

    print(f"\n✓ Enrolled '{args.name}' from {len(embeddings)} images")
    print(f"  Stored as Redis key: {key}")
    print(f"  Embedding dim: {avg_embedding.shape[0]}")


if __name__ == "__main__":
    main()
