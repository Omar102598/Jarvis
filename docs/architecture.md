# JARVIS Architecture

## System Overview

JARVIS is an event-driven, microservices-based AI assistant built on five pillars:
**Voice**, **Intelligence**, **Smart Home**, **Vision**, and **Wearable**.

All components communicate through an MQTT message bus with Redis for state persistence.

---

## High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        CENTRAL GPU SERVER                            │
│                                                                      │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌──────────────┐  │
│  │  LLM Brain │  │  Vision    │  │  Voice     │  │  Orchestrator│  │
│  │ (Ollama /  │  │ (YOLO26 + │  │ (Whisper + │  │  (LangGraph  │  │
│  │  vLLM /    │  │ InsightFace│  │  XTTS +    │  │  + FastAPI + │  │
│  │  Cloud API)│  │ + DeepFace)│  │  Piper)    │  │  Tools)      │  │
│  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘  └──────┬───────┘  │
│        │               │               │                 │          │
│  ┌─────┴───────────────┴───────────────┴─────────────────┴───────┐  │
│  │                    MQTT Broker (Mosquitto)                     │  │
│  └───────────────────────────┬───────────────────────────────────┘  │
│                              │                                      │
│  ┌───────────────────────────┴───────────────────────────────────┐  │
│  │                    Redis (State Store)                         │  │
│  │   • Conversation history    • Speaker embeddings              │  │
│  │   • Room occupancy          • Face embeddings                 │  │
│  │   • Device state cache      • Scheduled reminders             │  │
│  └───────────────────────────────────────────────────────────────┘  │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
            ┌──────────────┼──────────────────────────┐
            │              │                          │
     ┌──────┴──────┐ ┌────┴──────────┐  ┌───────────┴──────────┐
     │ Room Nodes  │ │ Camera Nodes  │  │  Wearable (Glasses)  │
     │ (per room)  │ │               │  │                      │
     │ • ReSpeaker │ │ • Ring (HA)   │  │ • Brilliant Labs     │
     │   mic array │ │ • PoE cams    │  │   Frame              │
     │ • Speaker   │ │   (Frigate)   │  │ • BLE → Phone →     │
     │   (Snapcast)│ │ • go2rtc      │  │   WebSocket → Server │
     │ • ESP32     │ │   RTSP proxy  │  │                      │
     │  (presence) │ └───────────────┘  └──────────────────────┘
     └──────┬──────┘
            │
     ┌──────┴──────────────────────────────────────────┐
     │           Home Assistant (Smart Home Hub)        │
     │                                                  │
     │  • Govee lights (LAN API — UDP, ~10-50ms)       │
     │  • Govee cloud (REST fallback for scenes)        │
     │  • Ring cameras & alarm                          │
     │  • Zigbee sensors (ZHA / Zigbee2MQTT)            │
     │  • ESPresense (BLE room tracking)                │
     │  • HA Companion App (GPS presence)               │
     │  • Weather, calendar, media players              │
     └─────────────────────────────────────────────────┘
```

---

## Component Detail

### 1. Voice Pipeline

```
Mic (ReSpeaker) ─→ OpenWakeWord ─→ faster-whisper ─→ SpeechBrain ─→ LLM ─→ TTS ─→ Speaker
   (always-on)     "Hey Jarvis"     (STT, ~300ms)     (verify       (process   (XTTS/    (Snapcast
    audio stream     / "J"            turbo model       speaker,      intent,    Piper)     routes to
                     ~80ms/frame                        ~200ms)       call                  correct
                                                                      tools)                room)
```

| Stage | Component | Runs On | Latency |
|-------|-----------|---------|---------|
| Wake word | OpenWakeWord | CPU (mic node or server) | ~80ms |
| Speech-to-text | faster-whisper (turbo) | GPU (server) | ~300ms |
| Speaker verify | SpeechBrain ECAPA-TDNN | CPU/GPU (server) | ~200ms |
| LLM reasoning | GPT-4.1 / Claude / Llama 3.3 | Cloud or GPU | ~500-1000ms |
| Text-to-speech | XTTS v2 (streaming) or Piper | GPU/CPU (server) | ~200ms / ~50ms |
| **Total end-to-end** | | | **~1.5-2.5s** |

### 2. Intelligence Layer (LLM + Agent)

```
                    ┌─────────────────────────┐
                    │    LangGraph Agent       │
                    │                          │
                    │  ┌────────────────────┐  │
                    │  │  LLM (GPT-4.1 /   │  │
                    │  │  Claude / Llama)   │  │
                    │  └────────┬───────────┘  │
                    │           │               │
                    │  ┌────────┴───────────┐  │
                    │  │   Tool Router      │  │
                    │  └────────┬───────────┘  │
                    └───────────┼───────────────┘
                                │
          ┌─────────┬───────────┼───────────┬──────────┐
          │         │           │           │          │
     ┌────┴───┐ ┌───┴────┐ ┌───┴────┐ ┌────┴───┐ ┌───┴─────┐
     │ Smart  │ │  Web   │ │ Vision │ │Calendar│ │Reminder │
     │ Home   │ │ Search │ │ Query  │ │  API   │ │Scheduler│
     │ (HA)   │ │(Tavily)│ │(camera)│ │(Google)│ │ (Redis) │
     └────────┘ └────────┘ └────────┘ └────────┘ └─────────┘
```

**Tools the LLM can invoke:**

| Tool | Description | Backend |
|------|-------------|---------|
| `control_device` | Turn on/off, set color, brightness, scene | HA WebSocket API |
| `get_device_states` | Query current state of devices/areas | HA REST API |
| `set_scene` | Activate a lighting/environment scene | HA scene service |
| `get_camera_snapshot` | Capture and analyze a camera image | HA camera proxy → Vision |
| `get_presence` | Who is home, which room | HA person + ESPresense |
| `web_search` | Search the internet | Tavily / Brave Search API |
| `get_weather` | Current/forecast weather | HA weather integration |
| `set_reminder` | Schedule a future reminder | Redis sorted set |
| `get_calendar` | Read upcoming events | Google Calendar API |
| `send_notification` | Push notification to phone | HA notify service |
| `analyze_image` | Describe what's in an image | GPT-4o / Claude vision |
| `identify_person` | Identify who is in a photo | InsightFace pipeline |

### 3. Vision Pipeline

```
Camera feed ─→ go2rtc (RTSP proxy) ─→ YOLO26m ─→ Person detected? ─→ InsightFace
  (Ring /                                (object       │                  (face ID)
   PoE cam)                              detect,     No → log/ignore        │
                                         ~5ms/frame)                    Known? ─→ Announce
                                                                          │
                                                                        Unknown → Alert
```

| Component | Model | Speed (GPU) | Purpose |
|-----------|-------|-------------|---------|
| Object detection | YOLO26m | 4.7ms/frame | Detect people, objects, pets |
| Face recognition | InsightFace Buffalo_L | ~10ms/face | Identify household members |
| Scene understanding | GPT-4o vision API | ~1-2s | "What's happening?" queries |
| Pose estimation | MediaPipe / YOLO26-pose | ~5ms | Gesture recognition, fall detection |

### 4. Smart Home Layer

```
JARVIS LLM
    │
    │  WebSocket (persistent connection)
    ▼
Home Assistant ──── govee_light_local ──── Govee lamps (UDP LAN, ~10-50ms)
    │           │── govee (cloud) ──────── Govee cloud (REST, scenes/music modes)
    │           │── ring ──────────────── Ring cameras & alarm
    │           │── zha ──────────────── Zigbee sensors (USB coordinator)
    │           │── esphome ───────────── ESP32 devices
    │           │── mobile_app ────────── Phone presence (GPS/WiFi)
    │           └── weather ───────────── Weather data
    │
    │  MQTT
    ▼
Mosquitto broker ◄──── ESPresense (room-level BLE tracking)
                 ◄──── Custom JARVIS services
```

**Govee control paths (priority order):**
1. **LAN API** (via `govee_light_local`) — fastest, no internet needed
2. **Cloud REST API** — for scenes, music modes, segment colors not available locally
3. **Siri Shortcuts (deprecate)** — replaced entirely by JARVIS → HA → Govee

### 5. Wearable Pipeline (Brilliant Labs Frame)

```
Frame glasses
    │
    │ BLE (Bluetooth Low Energy)
    ▼
Phone (companion app)
    │
    │ WebSocket (over WiFi / cellular / VPN)
    ▼
JARVIS Server
    │
    ├─→ Camera photo → Vision pipeline → LLM → response
    ├─→ Audio recording → STT → LLM → TTS → response
    └─→ Display text/image → phone → BLE → Frame OLED
```

**Bandwidth consideration:** BLE limits continuous video streaming. Design for:
- Periodic snapshots (on voice command or gesture trigger)
- Audio streaming for voice commands
- Text/small image push to 640×400 OLED display

### 6. Network Architecture

```
Internet
    │
    │ (WireGuard VPN / Tailscale for remote access)
    ▼
Router (2.4GHz + 5GHz WiFi)
    │
    ├── VLAN 1: Trusted ──── GPU Server, Home Assistant, Phone
    ├── VLAN 2: IoT ──────── Govee, Ring, Zigbee coordinator, ESP32s
    └── VLAN 3: Cameras ──── PoE cameras (if added)

Firewall rules:
  • IoT VLAN → Internet: allow (Govee cloud, Ring cloud)
  • IoT VLAN → Trusted: deny (except HA on specific ports)
  • Trusted → IoT: allow
  • Camera VLAN → Internet: deny (local only)
  • Camera VLAN → Trusted: allow (Frigate/go2rtc ports only)
```

### 7. Data Flow — Example Command

**"Hey Jarvis, turn the bedroom lights to ocean blue and dim to 40%"**

```
1.  ReSpeaker mic (bedroom) → always-on audio stream
2.  OpenWakeWord detects "Hey Jarvis" → publishes to jarvis/audio/mic/bedroom/wake_word
3.  Audio buffer (post-wake-word) → sent to faster-whisper on GPU server
4.  faster-whisper transcribes: "turn the bedroom lights to ocean blue and dim to 40%"
5.  SpeechBrain verifies speaker identity → match (Omar) → authorized
6.  Transcription + context → LangGraph agent → LLM
7.  LLM decides to call tool: control_device("light.bedroom_govee", "turn_on",
       {rgb: [0,119,190], brightness: 102})
8.  Tool executes → POST to HA WebSocket API → HA calls govee_light_local
9.  govee_light_local sends UDP command to Govee lamp → lamp changes instantly
10. LLM generates response: "Bedroom lights set to ocean blue at 40%"
11. XTTS v2 synthesizes speech → Snapcast routes audio to bedroom speaker
12. Total time: ~2 seconds
```

---

## MQTT Topic Hierarchy

```
jarvis/
├── audio/
│   └── mic/{room}/
│       ├── wake_word          # Wake word detections
│       └── speech             # Transcribed text
├── llm/
│   ├── request                # Prompt to LLM
│   └── response               # LLM response
├── tts/
│   └── {room}/speak           # Text to speak in room
├── vision/
│   └── camera/{camera_id}/
│       ├── detections         # Object/person detections
│       └── faces              # Recognized faces
├── glasses/
│   ├── camera/photo           # Photo from smart glasses
│   └── display/text           # Text to show on glasses
├── presence/
│   └── {room}                 # Room occupancy updates
└── homeassistant/
    └── command                # Smart home commands
```

---

## Technology Stack Summary

| Layer | Technology | License |
|-------|-----------|---------|
| Message bus | Mosquitto MQTT | EPL-2.0 |
| State store | Redis | BSD-3 |
| Wake word | OpenWakeWord | Apache-2.0 |
| STT | faster-whisper (turbo) | MIT |
| Speaker verify | SpeechBrain ECAPA-TDNN | Apache-2.0 |
| LLM (cloud) | GPT-4.1 / Claude | Commercial API |
| LLM (local) | Llama 3.3 via Ollama | Llama license |
| Agent framework | LangGraph | MIT |
| TTS (quality) | Coqui XTTS v2 | MPL-2.0 |
| TTS (speed) | Piper | GPL-3.0 |
| Smart home hub | Home Assistant | Apache-2.0 |
| Object detection | YOLO26 (Ultralytics) | AGPL-3.0 |
| Face recognition | InsightFace | MIT |
| Camera proxy | go2rtc | MIT |
| NVR | Frigate | MIT |
| Multi-room audio | Snapcast | GPL-3.0 |
| Room presence | ESPresense | MIT |
| Wearable | Brilliant Labs Frame SDK | MIT |
| Process mgmt | Docker Compose | Apache-2.0 |
| Remote access | Tailscale / WireGuard | BSD-3 / GPL-2.0 |
