# JARVIS Build Plan

## Overview

Build a custom AI assistant with voice control, speaker recognition, smart home automation,
computer vision, and wearable integration. Five phases, each independently functional.

---

## Phase 1: Voice Core (Weeks 1-3)

> **Goal:** Say "Hey Jarvis" and get a spoken response from an LLM.

### Step 1.1 — Server Setup
- [ ] Build or configure GPU server (RTX 4090 + 64GB RAM + 2TB NVMe)
- [ ] Install Ubuntu Server 24.04 LTS
- [ ] Install NVIDIA drivers + CUDA toolkit
- [ ] Install Docker + Docker Compose
- [ ] Set up Tailscale for remote access

### Step 1.2 — Message Bus & State *(parallel with 1.1)*
- [ ] Deploy Mosquitto MQTT broker (Docker container)
- [ ] Deploy Redis (Docker container)
- [ ] Test pub/sub with `mosquitto_pub` / `mosquitto_sub`
- [ ] Configure MQTT topics per the architecture doc

### Step 1.3 — Wake Word Detection *(depends on 1.1)*
- [ ] Install OpenWakeWord (`pip install openwakeword`)
- [ ] Test with built-in "hey jarvis" model + ReSpeaker mic
- [ ] Train custom "J" wake word model via Colab notebook
- [ ] Create a systemd service / Docker container that:
  - Listens to ReSpeaker audio stream
  - Runs OpenWakeWord inference
  - Publishes wake events to `jarvis/audio/mic/{room}/wake_word`

### Step 1.4 — Speech-to-Text *(depends on 1.1)*
- [ ] Install faster-whisper (`pip install faster-whisper`)
- [ ] Download `turbo` model (~1.5GB)
- [ ] Integrate with WhisperLive for streaming
- [ ] Service: subscribes to wake word events → captures audio → transcribes → publishes to MQTT

### Step 1.5 — Speaker Verification *(parallel with 1.4)*
- [ ] Install SpeechBrain (`pip install speechbrain`)
- [ ] Record 30 seconds of your voice for enrollment
- [ ] Extract ECAPA-TDNN embedding → store in Redis
- [ ] Service: receives audio after wake word → verifies speaker → authorize/reject

### Step 1.6 — LLM Brain *(depends on 1.1)*
- [ ] Install Ollama (`curl -fsSL https://ollama.com/install.sh | sh`)
- [ ] Pull Llama 3.1 8B for local testing (`ollama pull llama3.1:8b`)
- [ ] Set up OpenAI/Anthropic API keys for cloud LLM
- [ ] Install LangGraph (`pip install langgraph langchain-openai`)
- [ ] Build initial agent with basic tools: `get_time`, `get_weather`, `web_search`
- [ ] Service: subscribes to transcription events → processes with LLM → publishes response

### Step 1.7 — Text-to-Speech *(depends on 1.1)*
- [ ] Install Coqui TTS (`pip install TTS`)
- [ ] Download XTTS v2 model
- [ ] Record 6s voice sample for JARVIS voice cloning (or find a synthetic British voice)
- [ ] Also install Piper for fast confirmations
- [ ] Service: subscribes to LLM responses → synthesizes speech → outputs to speaker

### Step 1.8 — Integration Test
- [ ] Wire all services together via MQTT
- [ ] End-to-end test: "Hey Jarvis, what time is it?"
- [ ] Measure and optimize latency (target: <2.5s)

**Phase 1 Deliverable:** A working voice assistant in one room that listens for "Hey Jarvis",
verifies your voice, processes commands through an LLM, and speaks responses.

---

## Phase 2: Smart Home (Weeks 4-6)

> **Goal:** "Jarvis, turn the lights blue" actually controls your Govee lamps.

### Step 2.1 — Home Assistant Setup
- [ ] Install Home Assistant OS on Raspberry Pi 5 (or VM on GPU server)
- [ ] Initial setup wizard, create admin account
- [ ] Generate a long-lived access token for API access
- [ ] Install MQTT integration and connect to Mosquitto broker

### Step 2.2 — Govee Integration *(depends on 2.1)*
- [ ] In Govee app: enable local LAN API for each device
- [ ] Install `govee_light_local` integration in HA → auto-discovers devices on LAN
- [ ] Add Govee cloud integration for advanced scenes/music modes
- [ ] Test: toggle lights, set colors, activate scenes from HA dashboard
- [ ] Map all Govee devices to HA areas (Bedroom, Living Room, etc.)

### Step 2.3 — LLM Smart Home Tools *(depends on 1.6 + 2.1)*
- [ ] Build LangGraph tools that call HA WebSocket API:
  - `control_device(entity_id, action, params)`
  - `get_device_states(area?)`
  - `set_scene(room, scene_name)`
  - `get_presence(person?)`
- [ ] Register tools with the LLM agent
- [ ] Test: "Jarvis, turn bedroom lights to red at 50%"

### Step 2.4 — Presence Detection *(parallel with 2.3)*
- [ ] Install HA Companion App on phone → configure GPS + WiFi presence
- [ ] Flash ESPresense firmware onto ESP32 boards (one per room)
- [ ] Configure ESPresense to track your phone's BLE beacon
- [ ] Create HA automations: arrive home → welcome scene, leave → away mode

### Step 2.5 — Environmental Automations *(depends on 2.2 + 2.4)*
- [ ] Create HA automations + LLM-managed routines:
  - Sunset → warm lights
  - Morning → bright cool lights
  - Leave home → all lights off, security mode
  - Enter room → activate room scene
  - Movie mode → dim lights
- [ ] JARVIS can dynamically create/modify automations via HA API

### Step 2.6 — Deprecate Siri Shortcuts
- [ ] Replicate all existing Siri Shortcut automations in HA
- [ ] Verify JARVIS can do everything Siri currently does
- [ ] Keep Siri as a backup but stop using for daily control

**Phase 2 Deliverable:** Full smart home control via voice. Govee lamps respond to JARVIS commands.
Presence-based automations run automatically.

---

## Phase 3: Vision & Cameras (Weeks 7-10)

> **Goal:** JARVIS sees and recognizes people via your cameras.

### Step 3.1 — Ring Camera Integration *(depends on 2.1)*
- [ ] Add Ring integration to Home Assistant
- [ ] Configure motion detection automations
- [ ] Set up snapshot capture on motion events → save to local storage

### Step 3.2 — Camera Proxy *(parallel with 3.1)*
- [ ] Deploy go2rtc (Docker container) as RTSP proxy
- [ ] If Ring RTSP is needed: install Scrypted to bridge Ring → RTSP
- [ ] Test video feed accessibility from the GPU server

### Step 3.3 — Object Detection *(depends on 3.2)*
- [ ] Install Ultralytics (`pip install ultralytics`)
- [ ] Download YOLO26m model
- [ ] Build vision service: processes RTSP feeds → detects people/objects → publishes to MQTT
- [ ] Configure to process at 5-15 FPS (balance GPU load)

### Step 3.4 — Face Recognition *(depends on 3.3)*
- [ ] Install InsightFace (`pip install insightface onnxruntime-gpu`)
- [ ] Enroll household members: 5-10 photos each → store embeddings in Redis
- [ ] Pipeline: YOLO detects person → crop face → InsightFace identifies → publish to MQTT
- [ ] JARVIS announces: "Omar is at the front door" vs "Unknown person at the front door"

### Step 3.5 — LLM Vision Tools *(depends on 3.1 + 3.4)*
- [ ] Add vision tools to LangGraph agent:
  - `get_camera_snapshot(camera)` → capture + GPT-4o vision analysis
  - `identify_person(camera)` → InsightFace lookup
  - `get_recent_detections(camera, minutes)` → query detection log
- [ ] Test: "Jarvis, who's at the front door?" / "Jarvis, is anyone in the backyard?"

### Step 3.6 — (Optional) Local PoE Cameras
- [ ] If Ring proves unreliable, add 1-2 Reolink or Amcrest PoE cameras
- [ ] Deploy Frigate NVR for 24/7 recording + detection
- [ ] Integrate Frigate with HA for events

**Phase 3 Deliverable:** JARVIS identifies people on camera, announces visitors, and answers
questions about what's happening on any camera feed.

---

## Phase 4: Wearable (Weeks 11-13)

> **Goal:** Use Brilliant Labs Frame glasses as a portable JARVIS interface.

### Step 4.1 — Frame SDK Setup
- [ ] Purchase Brilliant Labs Frame glasses
- [ ] Install SDK: `pip install frame-sdk`
- [ ] Pair with phone via Bluetooth
- [ ] Test: capture photo, record audio, display text on lens

### Step 4.2 — Phone Companion App
- [ ] Build a lightweight companion app (Python script or Flutter app) that:
  - Maintains BLE connection to Frame glasses
  - Maintains WebSocket connection to JARVIS server (via Tailscale VPN)
  - Relays camera photos, audio, and display commands between Frame and server

### Step 4.3 — Glasses Voice Commands *(depends on 4.2)*
- [ ] "Jarvis, what am I looking at?" → capture photo → GPT-4o vision → TTS + display
- [ ] "Jarvis, who is this person?" → capture photo → InsightFace → display name
- [ ] "Jarvis, are the lights on at home?" → query HA → display + TTS

### Step 4.4 — Proactive Notifications *(depends on 4.2)*
- [ ] JARVIS pushes notifications to Frame display:
  - Calendar reminders
  - "Someone is at the front door" (with photo)
  - Weather alerts
  - Smart home alerts (e.g., motion detected while away)

**Phase 4 Deliverable:** JARVIS accessible on-the-go via smart glasses with camera, mic,
and display capabilities.

---

## Phase 5: Multi-Room & Polish (Weeks 14-16)

> **Goal:** JARVIS is everywhere in your home, always listening, always ready.

### Step 5.1 — Per-Room Deployment
- [ ] Install ReSpeaker mic + speaker in each room (bedroom, living room, office)
- [ ] Deploy Snapcast server on GPU box, Snapcast clients on per-room RPi Zeros
- [ ] JARVIS routes responses to the room where the wake word was heard

### Step 5.2 — Multi-Room Audio
- [ ] "Jarvis, play music in the kitchen" → HA media player + Snapcast
- [ ] "Jarvis, announce dinner is ready" → TTS → all room speakers simultaneously
- [ ] Intercom mode: "Jarvis, tell the bedroom that dinner is ready"

### Step 5.3 — Reliability & Monitoring
- [ ] Docker Compose with `restart: always` for all services
- [ ] MQTT last-will messages for crash detection
- [ ] Grafana + Prometheus dashboard for system health
- [ ] Watchdog: auto-restart crashed services, alert on failures

### Step 5.4 — Personality & Polish
- [ ] Fine-tune JARVIS personality prompt (British butler, witty, concise)
- [ ] Custom wake word responses ("At your service", "Yes, sir?")
- [ ] Context awareness: knows time of day, your schedule, room you're in
- [ ] Proactive suggestions: "You have a meeting in 30 minutes"

**Phase 5 Deliverable:** A polished, multi-room, always-on JARVIS that feels like a real assistant.

---

## Dependency Graph

```
Phase 1 (Voice Core)
  ├── 1.1 Server ──────────┬──→ 1.3 Wake Word ──→ 1.8 Integration
  ├── 1.2 MQTT/Redis ──────┤                         ▲
  │   (parallel with 1.1)  ├──→ 1.4 STT ─────────────┤
  │                        ├──→ 1.5 Speaker Verify ───┤
  │                        ├──→ 1.6 LLM Brain ────────┤
  │                        └──→ 1.7 TTS ──────────────┘
  │
Phase 2 (Smart Home) ← depends on Phase 1
  ├── 2.1 Home Assistant
  ├── 2.2 Govee ← depends on 2.1
  ├── 2.3 LLM Tools ← depends on 1.6 + 2.1
  ├── 2.4 Presence (parallel with 2.3)
  └── 2.5 Automations ← depends on 2.2 + 2.4
  │
Phase 3 (Vision) ← depends on Phase 2
  ├── 3.1 Ring + 3.2 go2rtc (parallel)
  ├── 3.3 YOLO ← depends on 3.2
  ├── 3.4 Face ID ← depends on 3.3
  └── 3.5 LLM Vision Tools ← depends on 3.1 + 3.4
  │
Phase 4 (Wearable) ← depends on Phase 1, benefits from Phase 3
  ├── 4.1 Frame SDK
  ├── 4.2 Companion App ← depends on 4.1
  └── 4.3-4.4 Features ← depends on 4.2
  │
Phase 5 (Multi-Room) ← depends on all prior phases
  └── Deployment, polish, monitoring
```
