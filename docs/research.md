# JARVIS Research Findings

> Full research summary from initial technology evaluation (April 2026).
> Covers: voice stack, smart home, vision, wearables, hardware, and architecture decisions.

---

## 1. Wake Word Detection

### Options Evaluated

| Engine | Custom Wake Words | Local/Cloud | License | Status |
|--------|------------------|-------------|---------|--------|
| **OpenWakeWord** | Yes (train via Colab <1hr) | Local | Apache-2.0 (code), CC BY-NC-SA 4.0 (models) | Active (v0.6.0) |
| **Porcupine (Picovoice)** | Yes (via Picovoice Console) | Local | Apache-2.0 (code), requires AccessKey | Active (v4.0, GPU support) |
| **Mycroft Precise** | Yes | Local | Apache-2.0 | Defunct (Mycroft shut down) |
| **Snowboy** | Limited | Local | Apache-2.0 | Archived, unmaintained |
| **microWakeWord** | Yes | Local (MCU) | — | Active, ESP32-class devices |

### Decision: **OpenWakeWord**

- Ships with pre-trained **"hey jarvis"** model out of the box
- Custom models trained from **100% synthetic speech** — no real voice data collection needed
- Runs 15-20 models simultaneously on a single Raspberry Pi 3 core
- 80ms frame processing, <5% false-reject, <0.5/hr false-accept
- Built-in custom verifier models for preliminary speaker gating
- Integrated Silero VAD and Speex noise suppression
- Repository: [github.com/dscripka/openWakeWord](https://github.com/dscripka/openWakeWord) (2.1k stars)

**Alternative:** Porcupine v4.0 is best for commercial-grade reliability and MCU deployment, but requires an AccessKey and custom wake words are trained through their proprietary console.

---

## 2. Speech-to-Text (STT)

### Options Evaluated

| Engine | Local/Cloud | Latency | Accuracy | Cost |
|--------|-------------|---------|----------|------|
| **faster-whisper** | Local | Excellent | SOTA | Free |
| **OpenAI Whisper** | Local | Slow | SOTA | Free |
| **whisper.cpp** | Local | Good | SOTA | Free |
| **Deepgram Nova-3** | Cloud | ~300ms | Excellent | ~$0.0043/min |
| **AssemblyAI** | Cloud | ~500ms | Excellent | ~$0.01/min |
| **Google Cloud STT** | Cloud | ~300ms | Excellent | $0.006/min |
| **OpenAI Whisper API** | Cloud | ~1-3s | SOTA | $0.006/min |

### Decision: **faster-whisper (turbo model)**

- Reimplements Whisper using CTranslate2 — **4x faster, less memory**
- `turbo` model: 809M params, ~8x faster than `large`, only ~6GB VRAM
- Batched transcription: 13 min audio in 17 seconds (GPU, batch_size=8, fp16)
- int8 quantization cuts memory nearly in half with minimal accuracy loss
- Built-in Silero VAD for silence filtering
- Word-level timestamps supported
- Repository: [github.com/SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) (22.1k stars)

**For streaming:** Pair with WhisperLive (Collabora) for real-time adaptive-latency transcription.

**Cloud fallback:** Deepgram Nova-3 offers ~300ms streaming latency if local GPU is unavailable.

---

## 3. Speaker Identification / Verification

### Options Evaluated

| Tool | Approach | Local/Cloud | Status |
|------|----------|-------------|--------|
| **SpeechBrain** | ECAPA-TDNN, X-vectors | Local | Active (v1.1.0) |
| **pyannote.audio** | Neural diarization + embedding | Local | Active (v4.0.4) |
| **Resemblyzer** | GE2E voice encoder | Local | Stale (3+ years) |
| **Azure Speaker Recognition** | Neural embeddings | Cloud | Active |

### Decision: **SpeechBrain ECAPA-TDNN**

- Near-SOTA speaker embeddings on VoxCeleb benchmark
- Full pipeline: enrollment (record voice) → embedding extraction → cosine similarity → threshold
- 200+ training recipes, pre-trained models on HuggingFace
- Repository: [github.com/speechbrain/speechbrain](https://github.com/speechbrain/speechbrain) (11.4k stars)

**Enrollment process:**
1. Record 5-30s of the owner's voice
2. Extract ECAPA-TDNN embedding → store in Redis
3. At runtime: extract embedding from command audio → cosine similarity → authorize if above threshold

**Optional add-on:** pyannote.audio (9.7k stars) for multi-speaker diarization if you need per-person permission levels in a household.

---

## 4. Text-to-Speech (TTS)

### Options Evaluated

| Engine | Naturalness | Latency | Voice Cloning | Local/Cloud | Cost |
|--------|------------|---------|---------------|-------------|------|
| **Coqui XTTS v2** | ★★★★ | <200ms streaming | Yes (6s reference) | Local | Free (MPL-2.0) |
| **Piper** | ★★★ | <50ms | No | Local | Free (GPL-3.0) |
| **ElevenLabs** | ★★★★★ | ~500ms | Yes | Cloud | $5-$99/mo |
| **OpenAI TTS** | ★★★★½ | ~300ms | No (6 presets) | Cloud | $0.015/1K chars |
| **Bark (Suno)** | ★★★★ | ~5-10s | Limited | Local | Free (MIT) |
| **Azure Neural TTS** | ★★★★½ | ~200ms | Yes | Cloud | $0.015/1K chars |

### Decision: **XTTS v2 (primary) + Piper (fast fallback)**

**XTTS v2:**
- Voice cloning from just 6 seconds of reference audio
- Streaming output with <200ms latency
- 16 languages
- ⚠️ Coqui AI (company) shut down, but open-source codebase is community-maintained
- Repository: [github.com/coqui-ai/TTS](https://github.com/coqui-ai/TTS) (45k stars)

**Piper:**
- Extremely fast — real-time even on Raspberry Pi, <50ms latency
- C++ engine (ONNX Runtime), VITS architecture
- Dozens of pre-trained voices
- ⚠️ Original repo archived; development at [github.com/OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl)
- Used by Home Assistant's voice pipeline

**Strategy:** XTTS for voice-cloned natural speech; Piper for quick confirmations ("Done", "Lights off").

---

## 5. LLM / AI Brain

### Options Evaluated

| Model/Framework | Local/Cloud | Tool Calling | Latency |
|----------------|-------------|--------------|---------|
| **GPT-4.1** | Cloud | Native | ~500ms TTFT |
| **Claude (Anthropic)** | Cloud | Native | ~500ms TTFT |
| **Gemini 2.5** | Cloud | Native | ~400ms |
| **Llama 3.3 70B** | Local (quantized) | Supported | ~1-3s |
| **Llama 3.1 8B** | Local | Supported | ~200-500ms |
| **Qwen 3 32B** | Local | Supported | ~500ms-1s |
| **LangGraph** | Framework | Via tools | — |
| **AutoGen** | Framework | Via agents | — (maintenance mode) |
| **CrewAI** | Framework | Via roles | — |

### Decision: **GPT-4.1 (cloud) + Llama 3.3 (local fallback) + LangGraph**

**LLM:**
- Cloud primary: GPT-4.1 or Claude with native tool/function calling. ~$2.50-10/M input tokens.
- Local fallback: Llama 3.3 70B (4-bit quantized, ~35GB VRAM on dual 4090) or Llama 3.1 8B on single 4090.
- Serve local models via Ollama or vLLM.

**Orchestration:** LangGraph (part of LangChain ecosystem, 133k stars)
- Stateful, graph-based agent workflows
- Native tool integrations
- Streaming, human-in-the-loop, persistence
- Works with any LLM provider

**Avoid AutoGen:** Now in maintenance mode (early 2026). Microsoft recommends migrating to Agent Framework (MAF).

---

## 6. Smart Home — Govee Evaluation

### Govee API Capabilities

**Cloud API** (`openapi.api.govee.com`):
- REST API, API key via Govee Home App
- Capabilities: on/off, brightness, RGB color, color temperature, segment colors, 100+ dynamic scenes, music reactive modes, DreamView
- Device types: lights, air purifiers, thermometers, sockets, sensors, heaters, humidifiers
- Rate limit: 10,000 requests/account/day

**LAN API** (via Home Assistant `govee_light_local`):
- Local push (UDP multicast) — ~10-50ms vs ~200-500ms cloud
- Massive device support (hundreds of H6xxx, H7xxx, H8xxx models)
- Works without internet
- Introduced in HA 2024.2

### Govee vs Alternatives

| Brand | API Quality | Local Control | Price | Verdict |
|-------|-------------|---------------|-------|---------|
| **Govee** | Good (REST + LAN) | Yes | $$ | **Best value for RGBIC strips/panels** |
| **Philips Hue** | Excellent (mature) | Yes (Zigbee) | $$$$ | Best ecosystem, expensive |
| **LIFX** | Good (LAN) | Yes | $$$ | Good standalone bulbs |
| **Nanoleaf** | Good (REST + local) | Yes | $$$$ | Best for wall art panels |

### Decision: **Keep Govee**

- Best price-to-feature ratio
- Solid LAN API for fast local control
- Cloud API fallback for advanced features (scenes, music modes)
- Fully supported by Home Assistant

---

## 7. Home Assistant as Hub

### Why Home Assistant

- **2,800+ integrations** normalized into a unified entity model
- REST API + WebSocket API for programmatic control
- Native MQTT integration
- Built-in voice assistant pipeline (Assist) with OpenWakeWord + Whisper + Piper
- Active community, open source (Apache-2.0)

### Key APIs

| API | Protocol | Use Case |
|-----|----------|----------|
| REST (`/api/states`, `/api/services/...`) | HTTP | Query states, trigger actions |
| WebSocket (`ws://ha:8123/api/websocket`) | WebSocket | Real-time events, persistent connection |
| MQTT | MQTT | Event transport with IoT devices |

### Control Path

```
JARVIS LLM → tool call → HA WebSocket API → govee_light_local → Govee lamp (UDP LAN)
```

---

## 8. Presence Detection

| Method | Accuracy | Latency | Setup |
|--------|----------|---------|-------|
| **HA Companion App** (phone GPS + WiFi) | High | 1-30s | Low — **Primary for home/away** |
| **ESPresense** (ESP32 BLE) | Very high (room-level) | 1-3s | Medium — **Best for room tracking** |
| **Aqara FP2 mmWave** | Very high | <1s | Low — **Best for occupancy detection** |
| Network presence (nmap/ping) | Medium | 30-60s | Low — secondary confirmation |

### Decision: HA Companion App + ESPresense + optional Aqara FP2

---

## 9. Ring Camera Integration

### Status
- **No official public API.** All integrations reverse-engineer Ring's private API.
- **ring-client-api** (dgreif/ring): Active, 1.5k stars, TypeScript/Node.js, latest release Feb 2026.
- **Home Assistant Ring integration**: Official, exposes cameras/doorbells/alarm/sensors as entities.

### Capabilities
- Motion/ding event subscriptions (FCMv1 push)
- Snapshot capture on events
- Live stream (SIP-based, can proxy to RTSP via Scrypted or ring-mqtt)
- Alarm arm/disarm, sensor states
- ⚠️ No native RTSP support

### Risk
Amazon can break the unofficial API at any time. **Mitigation:** Budget for local PoE cameras (Reolink/Amcrest) with Frigate NVR as a reliable parallel system.

---

## 10. Visual Computing

### Face Recognition: **InsightFace**
- Buffalo_L/ArcFace model: 99.8%+ accuracy on LFW
- ONNX-based, runs on CPU or GPU
- NIST FRVT top performer
- Repository: [github.com/deepinsight/insightface](https://github.com/deepinsight/insightface)

### Object Detection: **YOLO26m (Ultralytics)**
- 53.1 mAP on COCO, 4.7ms/frame on GPU
- Detection, segmentation, pose estimation, tracking
- Direct RTSP URL support
- ⚠️ AGPL-3.0 license (fine for personal use)
- Repository: [github.com/ultralytics/ultralytics](https://github.com/ultralytics/ultralytics) (55.8k stars)

### Scene Understanding
- **MediaPipe**: Real-time pose, hand tracking, face mesh
- **GPT-4o vision**: High-level scene descriptions via API
- **DeepFace**: All-in-one face analysis (recognition, emotion, age, gender)

### Hardware Requirements

| Setup | Can Run | Latency |
|-------|---------|---------|
| RTX 4090 (24GB) | All models + 7B LLM simultaneously | 5-20ms inference |
| Mac Mini M4 Pro | Most models + 7B LLM via MLX | 20-50ms |
| Jetson Orin Nano | YOLO + face recognition | 10-30ms |

---

## 11. Wearable / Smart Glasses

### Meta Ray-Ban Smart Glasses: **NOT SUITABLE**
- Completely closed platform
- No third-party camera/mic access
- No custom assistant integration
- "Hey Meta" hardcoded, cannot be redirected

### Brilliant Labs Frame: **RECOMMENDED**
- Fully open-source (MIT license)
- Camera: 720p, returns raw bytes via Python SDK
- Microphone: record and stream audio
- Display: 640×400 color OLED in lens
- BLE to phone/computer
- Python SDK: `pip install frame-sdk`
- Repository: [github.com/brilliantlabs/frame-sdk-python](https://github.com/brilliantlabs/frame-sdk-python)
- **Limitation:** BLE bandwidth constrains continuous video; use periodic snapshots

### Other Options Evaluated

| Device | Open API | Camera | Display | Verdict |
|--------|----------|--------|---------|---------|
| **Brilliant Labs Frame** | ✅ Full | ✅ 720p | ✅ Color OLED | Best for custom dev |
| **Even Realities G1** | ❌ Limited | ❌ | ✅ Green LED | Display only, no vision |
| **Xreal Air 2** | ⚠️ Partial | ❌ | ✅ AR via USB-C | Display-only glasses |
| **DIY (RPi Zero + camera)** | ✅ | ✅ | Optional | Heavy, short battery |

---

## 12. System Architecture Decision

### Event Bus: **MQTT (Mosquitto)**
- Lightweight, IoT standard
- Native Home Assistant integration
- QoS levels, retained messages
- Runs on Jetson/Pi
- Better fit than RabbitMQ (overkill) or Redis Pub/Sub (not IoT-native)

### State Store: **Redis**
- Conversation history, speaker/face embeddings, room occupancy, context
- Use alongside MQTT, not as a replacement

### Process Management: **Docker Compose**
- All services as containers with `restart: always`
- NVIDIA Container Toolkit for GPU services
- MQTT last-will messages for crash detection

### Remote Access: **Tailscale**
- Zero-config VPN, works through NAT
- Enables away-from-home JARVIS access via phone or glasses

---

## 13. Key Repositories Reference

| Component | Repository | Stars | License |
|-----------|-----------|-------|---------|
| Wake word | [dscripka/openWakeWord](https://github.com/dscripka/openWakeWord) | 2.1k | Apache-2.0 |
| STT | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 22.1k | MIT |
| Speaker verify | [speechbrain/speechbrain](https://github.com/speechbrain/speechbrain) | 11.4k | Apache-2.0 |
| TTS (quality) | [coqui-ai/TTS](https://github.com/coqui-ai/TTS) | 45k | MPL-2.0 |
| TTS (speed) | [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl) | 10.8k | GPL-3.0 |
| LLM orchestration | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | 133k | MIT |
| Object detection | [ultralytics/ultralytics](https://github.com/ultralytics/ultralytics) | 55.8k | AGPL-3.0 |
| Face recognition | [deepinsight/insightface](https://github.com/deepinsight/insightface) | — | MIT |
| Face analysis | [serengil/deepface](https://github.com/serengil/deepface) | — | MIT |
| Smart home | [home-assistant.io](https://www.home-assistant.io/) | — | Apache-2.0 |
| Glasses SDK | [brilliantlabs/frame-sdk-python](https://github.com/brilliantlabs/frame-sdk-python) | — | MIT |
| Ring API | [dgreif/ring](https://github.com/dgreif/ring) | 1.5k | MIT |
| NVR | [blakeblackshear/frigate](https://github.com/blakeblackshear/frigate) | — | MIT |
| RTSP proxy | [AlexxIT/go2rtc](https://github.com/AlexxIT/go2rtc) | — | MIT |
| Multi-room audio | [badaix/snapcast](https://github.com/badaix/snapcast) | — | GPL-3.0 |
| Room presence | [ESPresense/ESPresense](https://github.com/ESPresense/ESPresense) | — | MIT |
| Smart home protocol | [Matter/Thread](https://csa-iot.org/all-solutions/matter/) | — | — |
