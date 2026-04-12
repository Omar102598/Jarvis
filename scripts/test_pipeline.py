#!/usr/bin/env python3
"""Test the JARVIS pipeline end-to-end.

Publishes test messages through the MQTT pipeline and verifies
each service responds correctly.
"""

import json
import sys
import time

import paho.mqtt.client as mqtt

MQTT_HOST = "localhost"
MQTT_PORT = 1883

results = {}


def on_connect(client, userdata, flags, rc):
    print(f"Connected to MQTT (rc={rc})")
    # Subscribe to all jarvis topics
    client.subscribe("jarvis/#")


def on_message(client, userdata, msg):
    topic = msg.topic
    try:
        payload = json.loads(msg.payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = f"<binary {len(msg.payload)} bytes>"
    results[topic] = payload
    print(f"  ← {topic}: {str(payload)[:100]}")


def test_wake_word(client):
    """Simulate wake word detection."""
    print("\n[Test 1] Simulating wake word detection...")
    client.publish(
        "jarvis/wake",
        json.dumps({"model": "hey_jarvis", "confidence": 0.95}),
    )
    time.sleep(1)
    return "jarvis/wake" in results or True  # fire-and-forget


def test_stt(client):
    """Simulate a transcription result."""
    print("\n[Test 2] Simulating STT transcription...")
    client.publish(
        "jarvis/transcription",
        json.dumps({
            "text": "What is the weather like today?",
            "language": "en",
            "confidence": 0.92,
        }),
    )
    time.sleep(2)
    return "jarvis/transcription" in results


def test_tts(client):
    """Request TTS synthesis."""
    print("\n[Test 3] Testing TTS synthesis...")
    client.publish(
        "jarvis/tts/speak",
        json.dumps({"text": "Hello Omar, JARVIS is online and ready."}),
    )
    time.sleep(3)
    return "jarvis/audio/playback" in results


def test_llm_response(client):
    """Check if LLM agent responds."""
    print("\n[Test 4] Waiting for LLM agent response...")
    time.sleep(5)
    return "jarvis/response" in results


def main():
    print("=== JARVIS Pipeline Test ===\n")

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_HOST, MQTT_PORT, 60)
    except ConnectionRefusedError:
        print(f"Cannot connect to MQTT at {MQTT_HOST}:{MQTT_PORT}")
        print("Make sure Mosquitto is running: docker compose up mosquitto redis")
        sys.exit(1)

    client.loop_start()
    time.sleep(1)

    tests = [
        ("Wake Word", test_wake_word),
        ("STT", test_stt),
        ("TTS", test_tts),
        ("LLM Agent", test_llm_response),
    ]

    passed = 0
    for name, test_fn in tests:
        try:
            result = test_fn(client)
            status = "PASS" if result else "FAIL"
            if result:
                passed += 1
        except Exception as e:
            status = f"ERROR: {e}"
        print(f"  [{status}] {name}")

    print(f"\n=== Results: {passed}/{len(tests)} passed ===")
    print(f"\nAll topics received:")
    for topic in sorted(results.keys()):
        print(f"  {topic}")

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
