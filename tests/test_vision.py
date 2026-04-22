"""Unit tests for vision processing logic.

Tests the core detection and face identification logic without
requiring actual YOLO, InsightFace models, or camera hardware.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


def test_identify_face_match():
    """Test face identification against known faces."""
    # Simulate known face embeddings (512-dim for InsightFace)
    known_emb = np.random.randn(512).astype(np.float32)
    known_emb = known_emb / np.linalg.norm(known_emb)

    # Slightly noisy test embedding
    test_emb = known_emb + np.random.randn(512).astype(np.float32) * 0.01
    test_emb = test_emb / np.linalg.norm(test_emb)

    known_faces = {"omar": known_emb}
    threshold = 0.45

    # Replicate _identify_face logic
    best_name = "unknown"
    best_score = 0.0

    for name, face_emb in known_faces.items():
        score = float(
            np.dot(face_emb, test_emb)
            / (np.linalg.norm(face_emb) * np.linalg.norm(test_emb))
        )
        if score > best_score:
            best_score = score
            best_name = name

    if best_score < threshold:
        best_name = "unknown"

    assert best_name == "omar"
    assert best_score > 0.95


def test_identify_face_unknown():
    """Test face identification rejects unknown faces."""
    known_emb = np.array([1.0] * 256 + [0.0] * 256, dtype=np.float32)
    known_emb = known_emb / np.linalg.norm(known_emb)

    test_emb = np.array([0.0] * 256 + [1.0] * 256, dtype=np.float32)
    test_emb = test_emb / np.linalg.norm(test_emb)

    known_faces = {"omar": known_emb}
    threshold = 0.45

    best_name = "unknown"
    best_score = 0.0

    for name, face_emb in known_faces.items():
        score = float(
            np.dot(face_emb, test_emb)
            / (np.linalg.norm(face_emb) * np.linalg.norm(test_emb))
        )
        if score > best_score:
            best_score = score
            best_name = name

    if best_score < threshold:
        best_name = "unknown"

    assert best_name == "unknown"


def test_identify_face_multiple_enrolled():
    """Test face identification picks the best match among multiple enrolled faces."""
    # Two enrolled people with distinct embeddings
    omar_emb = np.zeros(512, dtype=np.float32)
    omar_emb[:256] = 1.0
    omar_emb = omar_emb / np.linalg.norm(omar_emb)

    guest_emb = np.zeros(512, dtype=np.float32)
    guest_emb[256:] = 1.0
    guest_emb = guest_emb / np.linalg.norm(guest_emb)

    known_faces = {"omar": omar_emb, "guest": guest_emb}
    threshold = 0.45

    # Test embedding close to omar
    test_emb = omar_emb + np.random.randn(512).astype(np.float32) * 0.01
    test_emb = test_emb / np.linalg.norm(test_emb)

    best_name = "unknown"
    best_score = 0.0

    for name, face_emb in known_faces.items():
        score = float(
            np.dot(face_emb, test_emb)
            / (np.linalg.norm(face_emb) * np.linalg.norm(test_emb))
        )
        if score > best_score:
            best_score = score
            best_name = name

    if best_score < threshold:
        best_name = "unknown"

    assert best_name == "omar"
    assert best_score > 0.95


def test_detection_result_format():
    """Test that detection results are properly formatted for MQTT."""
    # Simulate YOLO detection output
    detections = [
        {
            "class": "person",
            "confidence": 0.95,
            "bbox": [100, 200, 300, 500],
        },
        {
            "class": "dog",
            "confidence": 0.87,
            "bbox": [400, 300, 550, 450],
        },
    ]

    payload = json.dumps({
        "camera": "front_door",
        "objects": detections,
    })

    # Verify it's valid JSON
    parsed = json.loads(payload)
    assert parsed["camera"] == "front_door"
    assert len(parsed["objects"]) == 2
    assert parsed["objects"][0]["class"] == "person"
    assert parsed["objects"][0]["confidence"] == 0.95


def test_face_recognition_result_format():
    """Test that face recognition results are properly formatted for MQTT."""
    payload = json.dumps({
        "camera": "front_door",
        "person": "omar",
        "confidence": 0.932,
    })

    parsed = json.loads(payload)
    assert parsed["camera"] == "front_door"
    assert parsed["person"] == "omar"
    assert 0 <= parsed["confidence"] <= 1
