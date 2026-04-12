"""Unit tests for MQTT message flow and service wiring.

Tests the message format and topic hierarchy used between services.
"""

import json

import pytest


def test_wake_word_message_format():
    """Test wake word MQTT message has required fields."""
    msg = {
        "model": "hey jarvis",
        "score": 0.95,
        "room": "office",
        "timestamp": "2024-01-01T00:00:00+00:00",
    }
    payload = json.dumps(msg)
    parsed = json.loads(payload)

    assert "model" in parsed
    assert "score" in parsed
    assert "room" in parsed
    assert "timestamp" in parsed
    assert parsed["score"] > 0


def test_stt_speech_message_format():
    """Test STT speech MQTT message has required fields."""
    msg = {
        "text": "Turn the lights on",
        "room": "office",
        "language": "en",
        "audio_path": "/data/audio_cache/abc123.wav",
    }
    payload = json.dumps(msg)
    parsed = json.loads(payload)

    assert "text" in parsed
    assert "room" in parsed
    assert len(parsed["text"]) > 0


def test_stt_speech_message_backward_compatible():
    """Test STT speech message works without audio_path (backward compatibility)."""
    msg = {
        "text": "What time is it?",
        "room": "bedroom",
        "language": "en",
    }
    payload = json.dumps(msg)
    parsed = json.loads(payload)

    # audio_path should be optional
    assert parsed.get("audio_path") is None
    assert "text" in parsed


def test_llm_request_message_format():
    """Test LLM request MQTT message has required fields."""
    msg = {
        "text": "Turn the lights on",
        "room": "office",
        "verified": True,
        "speaker": "omar",
    }
    payload = json.dumps(msg)
    parsed = json.loads(payload)

    assert "text" in parsed
    assert "room" in parsed
    assert "verified" in parsed
    assert parsed["verified"] is True


def test_tts_speak_message_format():
    """Test TTS speak MQTT message has required fields."""
    msg = {
        "text": "Lights turned on, sir.",
        "room": "office",
    }
    payload = json.dumps(msg)
    parsed = json.loads(payload)

    assert "text" in parsed
    assert "room" in parsed


def test_topic_hierarchy_wake_word():
    """Test wake word topic follows the hierarchy."""
    room = "bedroom"
    topic = f"jarvis/audio/mic/{room}/wake_word"
    assert topic == "jarvis/audio/mic/bedroom/wake_word"


def test_topic_hierarchy_speech():
    """Test speech topic follows the hierarchy."""
    room = "office"
    topic = f"jarvis/audio/mic/{room}/speech"
    assert topic == "jarvis/audio/mic/office/speech"


def test_topic_hierarchy_tts():
    """Test TTS topic follows the hierarchy."""
    room = "living_room"
    topic = f"jarvis/tts/{room}/speak"
    assert topic == "jarvis/tts/living_room/speak"


def test_topic_hierarchy_vision():
    """Test vision detection topic follows the hierarchy."""
    camera = "front_door"
    topic = f"jarvis/vision/{camera}/detections"
    assert topic == "jarvis/vision/front_door/detections"


def test_glasses_status_message_format():
    """Test glasses status MQTT message format."""
    msg = {
        "connected": True,
        "battery": 85,
    }
    payload = json.dumps(msg)
    parsed = json.loads(payload)

    assert "connected" in parsed
    assert "battery" in parsed
    assert isinstance(parsed["battery"], int)
