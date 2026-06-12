# JARVIS — Personal AI Assistant

An event-driven, microservices-based AI assistant inspired by Iron Man's JARVIS.
Voice control, smart home automation, computer vision, and wearable integration.

## Features

- **Voice Control** — Wake word detection ("Hey Jarvis" / "J"), speech-to-text, natural language understanding, text-to-speech
- **iPhone / Siri** — Talk to JARVIS from your iPhone via a Siri Shortcut; say "Hey Siri, Ask JARVIS" to activate hands-free
- **Speaker Verification** — Recognizes your voice; rejects unauthorized commands
- **Smart Home** — Controls Govee lights (and more) via Home Assistant with local LAN priority
- **Vision** — Person detection (YOLO), face recognition (InsightFace), camera snapshot analysis (GPT-4o)
- **Wearable** — Brilliant Labs Frame smart glasses integration (camera, mic, OLED display)
- **Multi-Room** — Microphone + speaker per room with synchronized audio (Snapcast)
- **Internet Access** — Web search, weather, calendar, and more via LLM tool-calling

## Architecture

See [docs/architecture.md](docs/architecture.md) for full system diagrams.

```
Voice → OpenWakeWord → faster-whisper → SpeechBrain → LangGraph Agent → XTTS/Piper → Speaker
                                                            │
                                                    ┌───────┼───────┐
                                                    │       │       │
                                               Smart Home  Web   Vision
                                               (Home Asst) Search (YOLO+
                                                                  InsightFace)
```

## Quick Start

**No GPU required.** The stack runs in CPU mode by default (whisper `small` +
Piper TTS); an NVIDIA GPU just makes it faster and unlocks the vision pipeline.
See [docs/shopping-list.md](docs/shopping-list.md) for the tiered hardware plan —
Tier 0 is your existing laptop.

### Prerequisites
- Docker + Docker Compose
- Python 3.11+
- An OpenAI API key (and optionally Tavily for web search / background agents)

### 1. Clone & Configure

```bash
git clone https://github.com/YOUR_USER/Jarvis.git
cd Jarvis
cp .env.example .env
# Edit .env with your API keys and configuration
```

### 2. Start Services

```bash
# CPU mode (any machine):
docker compose up -d

# With an NVIDIA GPU (requires nvidia-container-toolkit):
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

### 3. Enroll Your Voice

```bash
python scripts/enroll_speaker.py --name omar
```

Speaker verification runs in `log` mode by default (it identifies the speaker
but never blocks). Set `VERIFY_MODE=enforce` in `.env` once enrollment scores
look good in the logs.

### 4. Test

```bash
python scripts/test_pipeline.py
```

## Project Structure

```
Jarvis/
├── README.md
├── docker-compose.yml
├── .env.example
├── .gitignore
├── docs/
│   ├── architecture.md
│   ├── Plan.md
│   ├── implementation.md
│   ├── shopping-list.md
│   └── research.md
├── config/
│   ├── mosquitto.conf
│   └── redis.conf
├── services/
│   ├── wake_word/
│   ├── stt/
│   ├── speaker_verify/
│   ├── llm_agent/
│   │   └── tools/
│   ├── tts/
│   ├── mobile_gateway/
│   ├── vision/
│   └── glasses_bridge/
├── scripts/
│   ├── enroll_speaker.py
│   ├── enroll_face.py
│   └── test_pipeline.py
├── models/            (gitignored — downloaded model weights)
└── data/
    ├── speaker_enrollment/
    ├── face_enrollment/
    └── snapshots/
```

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | System design, component diagrams, data flow |
| [Plan](docs/Plan.md) | Phased build plan with checkboxes |
| [Implementation](docs/implementation.md) | Step-by-step setup and code guide |
| [iPhone / Siri Setup](docs/iphone-setup.md) | Use JARVIS hands-free from your iPhone |
| [Shopping List](docs/shopping-list.md) | Hardware & costs breakdown |
| [Research](docs/research.md) | Full technology evaluation findings |

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Wake word | OpenWakeWord |
| STT | faster-whisper (turbo) |
| Speaker verify | SpeechBrain ECAPA-TDNN |
| LLM | GPT-4.1 / Claude / Llama 3.3 |
| Agent framework | LangGraph |
| TTS | Coqui XTTS v2 + Piper |
| Smart home | Home Assistant |
| Object detection | YOLO26 (Ultralytics) |
| Face recognition | InsightFace |
| Message bus | MQTT (Mosquitto) |
| State store | Redis |
| Wearable | Brilliant Labs Frame |

## License

MIT
