"""JARVIS Speech-to-Text Service.

Subscribes to wake word events. When triggered, records audio from the mic
until silence is detected, then transcribes using faster-whisper and publishes
the text to MQTT. Raw audio is saved to a shared volume for speaker verification.
"""

import io
import json
import os
import uuid
import wave

import numpy as np
import paho.mqtt.client as mqtt
import pyaudio
from faster_whisper import WhisperModel

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio_cache")

SAMPLE_RATE = 16000
CHUNK_SIZE = 1600  # 100ms at 16kHz
SILENCE_THRESHOLD = 500  # RMS threshold for silence
SILENCE_DURATION = 1.5  # seconds of silence to stop
MAX_RECORD_SECONDS = 10

# Ensure audio cache directory exists
os.makedirs(AUDIO_DIR, exist_ok=True)

# Load model on startup (downloads if not cached)
print("[STT] Loading whisper turbo model...")
model = WhisperModel("turbo", device="cuda", compute_type="float16")
print("[STT] Model loaded.")


def capture_command_audio() -> tuple[io.BytesIO, bytes]:
    """Record audio until silence or max duration, return as WAV BytesIO and raw bytes."""
    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
        input=True, frames_per_buffer=CHUNK_SIZE,
    )

    frames = []
    silent_chunks = 0
    chunks_per_second = SAMPLE_RATE // CHUNK_SIZE

    for _ in range(int(MAX_RECORD_SECONDS * chunks_per_second)):
        data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        frames.append(data)

        audio_data = np.frombuffer(data, dtype=np.int16)
        rms = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))

        if rms < SILENCE_THRESHOLD:
            silent_chunks += 1
        else:
            silent_chunks = 0

        if silent_chunks > int(SILENCE_DURATION * chunks_per_second):
            break

    stream.stop_stream()
    stream.close()
    audio.terminate()

    raw_bytes = b"".join(frames)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw_bytes)
    buf.seek(0)
    return buf, raw_bytes


def on_wake_word(client, userdata, msg):
    """Handle wake word detection: record, transcribe, publish."""
    data = json.loads(msg.payload)
    room = data["room"]
    print(f"[STT] Wake word in '{room}', recording...")

    audio_buf, raw_bytes = capture_command_audio()

    # Save raw audio to shared volume for speaker verification
    audio_id = uuid.uuid4().hex[:12]
    audio_path = os.path.join(AUDIO_DIR, f"{audio_id}.wav")
    with wave.open(audio_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw_bytes)

    segments, info = model.transcribe(audio_buf, language="en", vad_filter=True)
    text = " ".join([s.text for s in segments]).strip()

    if text:
        print(f"[STT] Transcribed ({room}): '{text}'")
        client.publish(
            f"jarvis/audio/mic/{room}/speech",
            json.dumps({
                "text": text,
                "room": room,
                "language": info.language,
                "audio_path": audio_path,
            }),
        )
    else:
        print(f"[STT] No speech detected in {room}")
        # Clean up audio file when no speech detected
        if os.path.exists(audio_path):
            os.unlink(audio_path)


def main():
    mqtt_client = mqtt.Client()
    mqtt_client.connect(MQTT_HOST, MQTT_PORT)
    mqtt_client.subscribe("jarvis/audio/mic/+/wake_word")
    mqtt_client.message_callback_add("jarvis/audio/mic/+/wake_word", on_wake_word)

    print("[STT] Waiting for wake word events...")
    mqtt_client.loop_forever()


if __name__ == "__main__":
    main()
