# JARVIS Implementation Guide

## Prerequisites

- Ubuntu Server 24.04 LTS (or similar Linux distro)
- NVIDIA GPU with CUDA support (RTX 4090 recommended)
- Python 3.11+
- Docker & Docker Compose
- Git

---

## 1. Server Foundation

### 1.1 NVIDIA Drivers & CUDA

```bash
# Install NVIDIA driver
sudo apt update && sudo apt install -y nvidia-driver-550
sudo reboot

# Verify
nvidia-smi

# Install CUDA toolkit
# Follow: https://developer.nvidia.com/cuda-downloads
```

### 1.2 Docker with GPU Support

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Install NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### 1.3 Project Structure

```
jarvis/
├── docker-compose.yml          # All services
├── .env                        # API keys, tokens (NEVER commit)
├── config/
│   ├── mosquitto.conf          # MQTT broker config
│   └── redis.conf              # Redis config
├── services/
│   ├── wake_word/              # OpenWakeWord service
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── main.py
│   ├── stt/                    # faster-whisper service
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── main.py
│   ├── speaker_verify/         # SpeechBrain service
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── main.py
│   ├── llm_agent/              # LangGraph orchestrator
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── main.py
│   │   ├── tools/
│   │   │   ├── smart_home.py
│   │   │   ├── web_search.py
│   │   │   ├── vision.py
│   │   │   └── calendar.py
│   │   └── prompts/
│   │       └── system.txt
│   ├── tts/                    # XTTS + Piper service
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── main.py
│   ├── vision/                 # YOLO + InsightFace
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── main.py
│   └── glasses_bridge/         # Frame WebSocket bridge
│       ├── Dockerfile
│       ├── requirements.txt
│       └── main.py
├── models/                     # Downloaded model weights (gitignored)
├── data/
│   ├── speaker_enrollment/     # Your voice samples
│   ├── face_enrollment/        # Household face photos
│   └── snapshots/              # Camera snapshots
└── scripts/
    ├── enroll_speaker.py       # Record & store voice embedding
    ├── enroll_face.py          # Register face embeddings
    └── test_pipeline.py        # End-to-end test
```

---

## 2. Core Infrastructure

### 2.1 Mosquitto MQTT Broker

**Config** (`config/mosquitto.conf`):
```
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest stdout
```

> **Note:** For production, add authentication via a password file or TLS certificates.

### 2.2 Redis

Default config is fine for development. In production, set `maxmemory` and `maxmemory-policy allkeys-lru`.

---

## 3. Service Implementations

### 3.1 Wake Word Service

```python
# services/wake_word/main.py
import openwakeword
from openwakeword.model import Model
import pyaudio
import numpy as np
import paho.mqtt.client as mqtt
import os
import json
from datetime import datetime, timezone

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
ROOM = os.environ.get("ROOM_NAME", "default")
THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))

# Load models — "hey jarvis" is built-in
oww_model = Model(
    wakeword_models=["hey jarvis"],
    inference_framework="onnx"
)

# MQTT client
mqtt_client = mqtt.Client()
mqtt_client.connect(MQTT_HOST, 1883)
mqtt_client.loop_start()

# Audio stream from ReSpeaker (or any USB mic)
audio = pyaudio.PyAudio()
stream = audio.open(
    format=pyaudio.paInt16,
    channels=1,
    rate=16000,
    input=True,
    frames_per_buffer=1280  # 80ms at 16kHz
)

print(f"[WakeWord] Listening in '{ROOM}'...")

while True:
    audio_frame = np.frombuffer(stream.read(1280), dtype=np.int16)
    prediction = oww_model.predict(audio_frame)

    for model_name, score in prediction.items():
        if score > THRESHOLD:
            print(f"[WakeWord] Detected '{model_name}' (score: {score:.2f})")
            mqtt_client.publish(
                f"jarvis/audio/mic/{ROOM}/wake_word",
                json.dumps({
                    "model": model_name,
                    "score": float(score),
                    "room": ROOM,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            )
            oww_model.reset()
```

### 3.2 Speech-to-Text Service

```python
# services/stt/main.py
import numpy as np
import pyaudio
import io
import wave
from faster_whisper import WhisperModel
import paho.mqtt.client as mqtt
import json
import os

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
SILENCE_THRESHOLD = 500
SILENCE_DURATION = 1.5  # seconds of silence to stop recording
MAX_RECORD_SECONDS = 10

model = WhisperModel("turbo", device="cuda", compute_type="float16")
mqtt_client = mqtt.Client()


def capture_command_audio(duration=MAX_RECORD_SECONDS):
    """Record audio until silence is detected or max duration reached."""
    audio = pyaudio.PyAudio()
    stream = audio.open(format=pyaudio.paInt16, channels=1, rate=16000,
                        input=True, frames_per_buffer=1600)
    frames = []
    silent_chunks = 0
    chunks_per_second = 16000 // 1600

    for _ in range(int(duration * chunks_per_second)):
        data = stream.read(1600)
        frames.append(data)
        audio_data = np.frombuffer(data, dtype=np.int16)
        if np.abs(audio_data).mean() < SILENCE_THRESHOLD:
            silent_chunks += 1
        else:
            silent_chunks = 0
        if silent_chunks > int(SILENCE_DURATION * chunks_per_second):
            break

    stream.stop_stream()
    stream.close()

    # Convert to WAV bytes
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b''.join(frames))
    buf.seek(0)
    return buf


def on_wake_word(client, userdata, msg):
    data = json.loads(msg.payload)
    room = data["room"]
    print(f"[STT] Wake word in {room}, recording...")

    audio_buf = capture_command_audio()
    segments, info = model.transcribe(audio_buf, language="en", vad_filter=True)
    text = " ".join([s.text for s in segments]).strip()

    if text:
        print(f"[STT] Transcribed: '{text}'")
        mqtt_client.publish(
            f"jarvis/audio/mic/{room}/speech",
            json.dumps({
                "text": text,
                "room": room,
                "language": info.language,
            })
        )


mqtt_client.connect(MQTT_HOST, 1883)
mqtt_client.subscribe("jarvis/audio/mic/+/wake_word")
mqtt_client.message_callback_add("jarvis/audio/mic/+/wake_word", on_wake_word)
print("[STT] Waiting for wake word events...")
mqtt_client.loop_forever()
```

### 3.3 Speaker Verification Service

```python
# services/speaker_verify/main.py
import torch
import torchaudio
import numpy as np
import redis
import paho.mqtt.client as mqtt
import json
import os

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
THRESHOLD = float(os.environ.get("VERIFY_THRESHOLD", "0.25"))

# Load pre-trained ECAPA-TDNN
from speechbrain.pretrained import SpeakerRecognition
verifier = SpeakerRecognition.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",
    savedir="/models/ecapa"
)

r = redis.Redis(host=REDIS_HOST)
mqtt_client = mqtt.Client()


def enroll_speaker(name: str, audio_path: str):
    """One-time: extract and store speaker embedding."""
    signal, sr = torchaudio.load(audio_path)
    if sr != 16000:
        signal = torchaudio.transforms.Resample(sr, 16000)(signal)
    embedding = verifier.encode_batch(signal).squeeze().numpy()
    r.set(f"speaker:{name}:embedding", embedding.tobytes())
    r.set(f"speaker:{name}:dim", str(len(embedding)))
    print(f"[Verify] Enrolled speaker: {name} (dim={len(embedding)})")


def verify_speaker(audio_data: np.ndarray, expected_name: str = "omar") -> tuple:
    """Returns (is_authorized: bool, similarity: float)."""
    stored = r.get(f"speaker:{expected_name}:embedding")
    if not stored:
        return False, 0.0

    dim = int(r.get(f"speaker:{expected_name}:dim"))
    stored_embed = np.frombuffer(stored, dtype=np.float32).reshape(dim)

    signal = torch.tensor(audio_data).unsqueeze(0).float()
    new_embed = verifier.encode_batch(signal).squeeze().numpy()

    similarity = float(np.dot(stored_embed, new_embed) / (
        np.linalg.norm(stored_embed) * np.linalg.norm(new_embed)
    ))
    return similarity > THRESHOLD, similarity


def on_speech(client, userdata, msg):
    data = json.loads(msg.payload)
    room = data["room"]
    text = data["text"]

    # In a full implementation, the raw audio would also be passed
    # via a shared file or binary MQTT payload for verification.
    # For now, we pass through and let the LLM handle it.
    print(f"[Verify] Passing through from {room}: '{text}'")
    mqtt_client.publish(
        f"jarvis/llm/request",
        json.dumps({
            "text": text,
            "room": room,
            "verified": True,  # TODO: actual verification with audio
        })
    )


mqtt_client.connect(MQTT_HOST, 1883)
mqtt_client.subscribe("jarvis/audio/mic/+/speech")
mqtt_client.message_callback_add("jarvis/audio/mic/+/speech", on_speech)
print("[Verify] Waiting for speech events...")
mqtt_client.loop_forever()
```

### 3.4 LLM Agent Service

```python
# services/llm_agent/main.py
import json
import os
from datetime import datetime, timezone

from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
import paho.mqtt.client as mqtt
import redis

from tools.smart_home import control_device, get_device_states, set_scene, get_presence
from tools.web_search import web_search
from tools.vision import get_camera_snapshot

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")

SYSTEM_PROMPT = """You are JARVIS, an advanced AI assistant inspired by the AI from Iron Man.
You are British, witty, efficient, and loyal. You address your user as "sir" occasionally but
not excessively. You have access to smart home controls, cameras, web search, and more.

Be concise but helpful. When controlling devices, confirm the action briefly.
If you don't know something, say so honestly.

Current time: {time}
User location: {room}
"""

# Tools
tools = [control_device, get_device_states, set_scene, get_presence,
         web_search, get_camera_snapshot]

# LLM
llm = ChatOpenAI(
    model=os.environ.get("LLM_MODEL", "gpt-4.1"),
    temperature=0.3,
).bind_tools(tools)

# Redis for conversation history
r = redis.Redis(host=REDIS_HOST, decode_responses=True)

# Build LangGraph agent
tool_node = ToolNode(tools)


def should_continue(state):
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


def call_model(state):
    messages = state["messages"]
    response = llm.invoke(messages)
    return {"messages": messages + [response]}


workflow = StateGraph(dict)
workflow.add_node("agent", call_model)
workflow.add_node("tools", tool_node)
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
workflow.add_edge("tools", "agent")
agent = workflow.compile()


def process_request(text: str, room: str) -> str:
    """Process a user request through the LLM agent."""
    system = SystemMessage(content=SYSTEM_PROMPT.format(
        time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        room=room
    ))

    # Get conversation history from Redis (last 20 messages)
    history_key = f"conversation:{room}"
    history_raw = r.lrange(history_key, -20, -1)
    history = []
    for h in history_raw:
        msg = json.loads(h)
        if msg["role"] == "user":
            history.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            history.append(AIMessage(content=msg["content"]))

    messages = [system] + history + [HumanMessage(content=text)]

    result = agent.invoke({"messages": messages})
    response_text = result["messages"][-1].content

    # Store in history
    r.rpush(history_key, json.dumps({"role": "user", "content": text}))
    r.rpush(history_key, json.dumps({"role": "assistant", "content": response_text}))
    r.ltrim(history_key, -40, -1)  # Keep last 40 entries

    return response_text


def on_llm_request(client, userdata, msg):
    data = json.loads(msg.payload)
    text = data["text"]
    room = data["room"]

    print(f"[LLM] Processing: '{text}' from {room}")
    response = process_request(text, room)
    print(f"[LLM] Response: '{response}'")

    mqtt_client.publish(
        f"jarvis/tts/{room}/speak",
        json.dumps({"text": response, "room": room})
    )


mqtt_client = mqtt.Client()
mqtt_client.connect(MQTT_HOST, 1883)
mqtt_client.subscribe("jarvis/llm/request")
mqtt_client.message_callback_add("jarvis/llm/request", on_llm_request)
print("[LLM] Agent ready, waiting for requests...")
mqtt_client.loop_forever()
```

### 3.5 Smart Home Tools

```python
# services/llm_agent/tools/smart_home.py
from langchain_core.tools import tool
import aiohttp
import os

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")
HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json"
}


@tool
async def control_device(entity_id: str, action: str, params: dict = None) -> str:
    """Control a smart home device.

    Args:
        entity_id: The device entity ID, e.g. 'light.bedroom_govee', 'switch.fan'
        action: One of 'turn_on', 'turn_off', 'toggle'
        params: Optional dict with brightness (0-255), rgb_color ([r,g,b]),
                color_temp_kelvin (2000-9000), effect name
    """
    domain = entity_id.split(".")[0]
    payload = {"entity_id": entity_id}
    if params:
        payload.update(params)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HA_URL}/api/services/{domain}/{action}",
            headers=HEADERS,
            json=payload
        ) as resp:
            if resp.status == 200:
                return f"Done. {entity_id} → {action}"
            else:
                error = await resp.text()
                return f"Error controlling {entity_id}: {resp.status} - {error}"


@tool
async def get_device_states(area: str = None) -> str:
    """Get current state of smart home devices, optionally filtered by room/area name.

    Args:
        area: Optional room name to filter by, e.g. 'bedroom', 'living room'
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{HA_URL}/api/states", headers=HEADERS) as resp:
            states = await resp.json()
            relevant = []
            for s in states:
                friendly = s.get("attributes", {}).get("friendly_name", "")
                if area and area.lower() not in friendly.lower():
                    continue
                eid = s["entity_id"]
                if eid.startswith(("light.", "switch.", "sensor.", "climate.", "media_player.")):
                    attrs = s.get("attributes", {})
                    info = f"{friendly}: {s['state']}"
                    if "brightness" in attrs:
                        info += f" (brightness: {round(attrs['brightness']/255*100)}%)"
                    if "rgb_color" in attrs:
                        info += f" (color: {attrs['rgb_color']})"
                    relevant.append(info)
            return "\n".join(relevant[:30]) or "No devices found."


@tool
async def set_scene(room: str, scene_name: str) -> str:
    """Activate a lighting/environment scene in a specific room.

    Args:
        room: Room name, e.g. 'bedroom', 'living_room', 'office'
        scene_name: Scene name, e.g. 'movie_mode', 'bedtime', 'energize', 'relax'
    """
    scene_id = f"scene.{room}_{scene_name}".lower().replace(" ", "_")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HA_URL}/api/services/scene/turn_on",
            headers=HEADERS,
            json={"entity_id": scene_id}
        ) as resp:
            if resp.status == 200:
                return f"Scene '{scene_name}' activated in {room}."
            else:
                return f"Scene not found: {scene_id}. Check available scenes."


@tool
async def get_presence(person: str = None) -> str:
    """Check who is home and which room they're in.

    Args:
        person: Optional person name to check, or None for all
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{HA_URL}/api/states", headers=HEADERS) as resp:
            states = await resp.json()
            results = []
            for s in states:
                if s["entity_id"].startswith("person."):
                    name = s["attributes"].get("friendly_name", s["entity_id"])
                    if person and person.lower() not in name.lower():
                        continue
                    state = s["state"]
                    results.append(f"{name}: {state}")
            return "\n".join(results) or "No presence data available."
```

### 3.6 Web Search Tool

```python
# services/llm_agent/tools/web_search.py
from langchain_core.tools import tool
import aiohttp
import os

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")


@tool
async def web_search(query: str) -> str:
    """Search the internet for current information.

    Args:
        query: The search query, e.g. 'weather in Austin today' or 'latest SpaceX launch'
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
            }
        ) as resp:
            if resp.status != 200:
                return f"Search failed: {resp.status}"
            data = await resp.json()
            results = []
            for r in data.get("results", []):
                results.append(f"**{r['title']}**\n{r['content'][:300]}\nSource: {r['url']}")
            return "\n\n".join(results) if results else "No results found."
```

### 3.7 Vision Tool

```python
# services/llm_agent/tools/vision.py
from langchain_core.tools import tool
import aiohttp
import base64
import os

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

HA_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
}


@tool
async def get_camera_snapshot(camera: str, question: str = "What do you see?") -> str:
    """Capture a snapshot from a camera and analyze it with vision AI.

    Args:
        camera: Camera entity ID, e.g. 'camera.front_door', 'camera.backyard'
        question: What to look for in the image, e.g. 'Is anyone there?' or 'What's happening?'
    """
    # Get snapshot from HA
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{HA_URL}/api/camera_proxy/{camera}",
            headers=HA_HEADERS
        ) as resp:
            if resp.status != 200:
                return f"Failed to get snapshot from {camera}: {resp.status}"
            image_bytes = await resp.read()

    # Send to GPT-4o vision
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{b64_image}"
                        }}
                    ]
                }],
                "max_tokens": 300
            }
        ) as resp:
            if resp.status != 200:
                return f"Vision analysis failed: {resp.status}"
            data = await resp.json()
            return data["choices"][0]["message"]["content"]
```

### 3.8 TTS Service

```python
# services/tts/main.py
import json
import os
import subprocess
import tempfile

import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
TTS_ENGINE = os.environ.get("TTS_ENGINE", "piper")  # "piper" or "xtts"
PIPER_MODEL = os.environ.get("PIPER_MODEL", "en_GB-alan-medium")

mqtt_client = mqtt.Client()


def speak_piper(text: str, output_file: str):
    """Fast local TTS using Piper."""
    cmd = f'echo "{text}" | piper --model {PIPER_MODEL} --output_file {output_file}'
    subprocess.run(cmd, shell=True, check=True)


def speak_xtts(text: str, output_file: str):
    """High-quality TTS using Coqui XTTS v2."""
    from TTS.api import TTS
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    tts.tts_to_file(
        text=text,
        speaker_wav="/data/jarvis_voice.wav",  # 6s reference sample
        language="en",
        file_path=output_file
    )


def on_tts_request(client, userdata, msg):
    data = json.loads(msg.payload)
    text = data["text"]
    room = data["room"]

    print(f"[TTS] Speaking in {room}: '{text[:80]}...'")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        output_path = f.name

    if TTS_ENGINE == "xtts":
        speak_xtts(text, output_path)
    else:
        speak_piper(text, output_path)

    # Play audio (in production, route via Snapcast to the correct room)
    subprocess.run(["aplay", output_path], check=False)
    os.unlink(output_path)


mqtt_client.connect(MQTT_HOST, 1883)
mqtt_client.subscribe("jarvis/tts/+/speak")
mqtt_client.message_callback_add("jarvis/tts/+/speak", on_tts_request)
print("[TTS] Ready, waiting for speech requests...")
mqtt_client.loop_forever()
```

### 3.9 Vision Pipeline Service

```python
# services/vision/main.py
import json
import os
import time

import cv2
import numpy as np
import redis
import paho.mqtt.client as mqtt
from ultralytics import YOLO
from insightface.app import FaceAnalysis

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
CAMERA_URLS = json.loads(os.environ.get("CAMERA_URLS", "{}"))
# Example: {"front_door": "rtsp://...", "backyard": "rtsp://..."}

PROCESS_FPS = int(os.environ.get("PROCESS_FPS", "5"))
FACE_THRESHOLD = float(os.environ.get("FACE_THRESHOLD", "0.4"))

# Models
yolo = YOLO("yolo26m.pt")
face_app = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider"])
face_app.prepare(ctx_id=0, det_size=(640, 640))

r = redis.Redis(host=REDIS_HOST)
mqtt_client = mqtt.Client()
mqtt_client.connect(MQTT_HOST, 1883)
mqtt_client.loop_start()


def identify_face(embedding: np.ndarray) -> tuple:
    """Compare face embedding against enrolled faces in Redis."""
    best_name = "unknown"
    best_score = 0.0

    for key in r.scan_iter("face:*:embedding"):
        name = key.decode().split(":")[1]
        stored = np.frombuffer(r.get(key), dtype=np.float32)
        score = float(np.dot(embedding, stored) / (
            np.linalg.norm(embedding) * np.linalg.norm(stored)
        ))
        if score > best_score and score > FACE_THRESHOLD:
            best_score = score
            best_name = name

    return best_name, best_score


def process_camera(camera_id: str, url: str):
    """Process frames from a single camera."""
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        print(f"[Vision] Failed to open camera: {camera_id} ({url})")
        return

    frame_interval = 1.0 / PROCESS_FPS

    while True:
        start = time.time()
        ret, frame = cap.read()
        if not ret:
            print(f"[Vision] Lost connection to {camera_id}, reconnecting...")
            cap.release()
            time.sleep(5)
            cap = cv2.VideoCapture(url)
            continue

        # Object detection
        results = yolo(frame, conf=0.5, classes=[0], verbose=False)  # class 0 = person
        persons = results[0].boxes

        if len(persons) > 0:
            detection_data = {
                "camera_id": camera_id,
                "person_count": len(persons),
                "timestamp": time.time()
            }
            mqtt_client.publish(
                f"jarvis/vision/camera/{camera_id}/detections",
                json.dumps(detection_data)
            )

            # Face recognition on each detected person
            for box in persons:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                person_crop = frame[y1:y2, x1:x2]

                if person_crop.size == 0:
                    continue

                faces = face_app.get(person_crop)
                for face in faces:
                    name, confidence = identify_face(face.embedding)
                    mqtt_client.publish(
                        f"jarvis/vision/camera/{camera_id}/faces",
                        json.dumps({
                            "name": name,
                            "confidence": float(confidence),
                            "camera_id": camera_id,
                            "timestamp": time.time()
                        })
                    )

        # Maintain target FPS
        elapsed = time.time() - start
        if elapsed < frame_interval:
            time.sleep(frame_interval - elapsed)


if __name__ == "__main__":
    import threading

    print(f"[Vision] Starting with cameras: {list(CAMERA_URLS.keys())}")

    for cam_id, cam_url in CAMERA_URLS.items():
        t = threading.Thread(target=process_camera, args=(cam_id, cam_url), daemon=True)
        t.start()

    # Keep main thread alive
    while True:
        time.sleep(60)
```

---

## 4. Home Assistant Configuration

### 4.1 Govee Local API Setup

1. Open **Govee Home app** → Settings → "LAN Control" → Enable for each device
2. In Home Assistant: **Settings → Devices & Services → Add Integration → "Govee Light Local"**
3. HA auto-discovers Govee devices on your LAN via UDP multicast
4. Rename entities to friendly names: `light.bedroom_strip`, `light.office_lamp`, etc.
5. Organize entities into Areas: Bedroom, Living Room, Office

### 4.2 MQTT Integration

In HA: **Settings → Devices & Services → Add Integration → MQTT**
- Broker: `localhost` (or GPU server IP)
- Port: `1883`
- No username/password (for dev; add auth for production)

### 4.3 Example Automations

```yaml
# In Home Assistant configuration.yaml or automations.yaml

automation:
  - alias: "JARVIS: Welcome Home"
    trigger:
      - platform: state
        entity_id: person.omar
        to: "home"
    action:
      - service: light.turn_on
        target:
          area_id: hallway
        data:
          brightness: 200
          color_temp_kelvin: 3500
      - service: mqtt.publish
        data:
          topic: "jarvis/tts/hallway/speak"
          payload: '{"text": "Welcome home, sir.", "room": "hallway"}'

  - alias: "JARVIS: Away Mode"
    trigger:
      - platform: state
        entity_id: person.omar
        to: "not_home"
        for: "00:05:00"
    action:
      - service: light.turn_off
        target:
          entity_id: all
```

---

## 5. Enrollment Scripts

### 5.1 Speaker Enrollment

```python
# scripts/enroll_speaker.py
"""Record your voice and store the embedding for speaker verification."""
import sys
import pyaudio
import wave
import numpy as np
import redis
from speechbrain.pretrained import SpeakerRecognition
import torchaudio

REDIS_HOST = "localhost"
DURATION = 30  # seconds
SAMPLE_RATE = 16000

def record_audio(filename, duration):
    audio = pyaudio.PyAudio()
    stream = audio.open(format=pyaudio.paInt16, channels=1,
                        rate=SAMPLE_RATE, input=True, frames_per_buffer=1024)
    print(f"Recording for {duration} seconds... Speak naturally.")
    frames = [stream.read(1024) for _ in range(int(SAMPLE_RATE / 1024 * duration))]
    stream.stop_stream()
    stream.close()
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b''.join(frames))
    print(f"Saved to {filename}")

def enroll(name, audio_file):
    verifier = SpeakerRecognition.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir="./models/ecapa"
    )
    signal, sr = torchaudio.load(audio_file)
    if sr != 16000:
        signal = torchaudio.transforms.Resample(sr, 16000)(signal)
    embedding = verifier.encode_batch(signal).squeeze().numpy()

    r = redis.Redis(host=REDIS_HOST)
    r.set(f"speaker:{name}:embedding", embedding.tobytes())
    r.set(f"speaker:{name}:dim", str(len(embedding)))
    print(f"Enrolled '{name}' with {len(embedding)}-dim embedding")

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "omar"
    wav_file = f"data/speaker_enrollment/{name}.wav"
    record_audio(wav_file, DURATION)
    enroll(name, wav_file)
```

### 5.2 Face Enrollment

```python
# scripts/enroll_face.py
"""Register face embeddings from photos for face recognition."""
import sys
import os
import numpy as np
import redis
from insightface.app import FaceAnalysis
import cv2

REDIS_HOST = "localhost"

face_app = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider"])
face_app.prepare(ctx_id=0, det_size=(640, 640))


def enroll(name: str, photo_dir: str):
    embeddings = []
    for fname in os.listdir(photo_dir):
        if not fname.lower().endswith(('.jpg', '.jpeg', '.png')):
            continue
        img = cv2.imread(os.path.join(photo_dir, fname))
        faces = face_app.get(img)
        if faces:
            embeddings.append(faces[0].embedding)
            print(f"  Found face in {fname}")
        else:
            print(f"  No face found in {fname}")

    if not embeddings:
        print(f"No faces found for {name}!")
        return

    # Average all embeddings for a robust representation
    avg_embedding = np.mean(embeddings, axis=0).astype(np.float32)
    avg_embedding = avg_embedding / np.linalg.norm(avg_embedding)  # L2 normalize

    r = redis.Redis(host=REDIS_HOST)
    r.set(f"face:{name}:embedding", avg_embedding.tobytes())
    print(f"Enrolled '{name}' from {len(embeddings)} photos")


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "omar"
    photo_dir = f"data/face_enrollment/{name}"
    if not os.path.isdir(photo_dir):
        print(f"Create {photo_dir}/ and add 5-10 photos of {name}")
        sys.exit(1)
    enroll(name, photo_dir)
```

---

## 6. Testing

### 6.1 End-to-End Pipeline Test

```python
# scripts/test_pipeline.py
"""Simulate the full JARVIS pipeline via MQTT messages."""
import paho.mqtt.client as mqtt
import json
import time
import sys

client = mqtt.Client()
responses = []

def on_message(c, u, msg):
    data = json.loads(msg.payload)
    topic = msg.topic
    responses.append((topic, data))
    print(f"  [{topic}] {json.dumps(data, indent=2)[:200]}")

client.on_message = on_message
client.connect("localhost", 1883)
client.subscribe("jarvis/#")
client.loop_start()

# Test 1: Simulate wake word
print("\n=== Test 1: Wake Word → STT → LLM → TTS ===")
client.publish("jarvis/audio/mic/office/wake_word", json.dumps({
    "model": "hey jarvis",
    "score": 0.95,
    "room": "office",
    "timestamp": "2026-04-12T12:00:00Z"
}))
time.sleep(8)

# Test 2: Direct LLM request (skip voice pipeline)
print("\n=== Test 2: Direct LLM Request ===")
client.publish("jarvis/llm/request", json.dumps({
    "text": "What time is it?",
    "room": "office",
    "verified": True
}))
time.sleep(5)

# Test 3: Smart home command
print("\n=== Test 3: Smart Home Command ===")
client.publish("jarvis/llm/request", json.dumps({
    "text": "Turn the bedroom lights to blue at 50%",
    "room": "bedroom",
    "verified": True
}))
time.sleep(5)

print(f"\n=== Summary: {len(responses)} messages received ===")
client.loop_stop()
```

---

## 7. Key Environment Variables

```bash
# .env (NEVER commit this file — it's in .gitignore)

# LLM API Keys
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=gpt-4.1

# Home Assistant
HA_URL=http://192.168.1.100:8123
HA_TOKEN=eyJ...

# Govee (for direct cloud API if needed)
GOVEE_API_KEY=...

# Search
TAVILY_API_KEY=tvly-...

# Ring (for ring-client-api if used directly)
RING_REFRESH_TOKEN=...

# Infrastructure
MQTT_HOST=localhost
REDIS_HOST=localhost

# Room Configuration
ROOM_NAME=office

# Vision
CAMERA_URLS={"front_door": "rtsp://...", "backyard": "rtsp://..."}
PROCESS_FPS=5
FACE_THRESHOLD=0.4

# Speaker Verification
VERIFY_THRESHOLD=0.25

# Wake Word
WAKE_THRESHOLD=0.5

# TTS
TTS_ENGINE=piper
PIPER_MODEL=en_GB-alan-medium
```

---

## 8. Startup

```bash
# Start all core services
docker compose up -d

# Check logs
docker compose logs -f wake_word
docker compose logs -f stt
docker compose logs -f llm_agent
docker compose logs -f tts

# Test MQTT manually
mosquitto_pub -h localhost -t "jarvis/tts/office/speak" \
  -m '{"text": "Systems online. All services operational.", "room": "office"}'

# Run full pipeline test
python scripts/test_pipeline.py
```
