# JARVIS Shopping List

> **You can start JARVIS today for $0.** The voice pipeline runs on CPU (faster-whisper
> small + Piper TTS are CPU-friendly) and the LLM brain is a cloud API. A GPU only
> becomes worthwhile when you want faster transcription, local LLMs, or the vision
> pipeline. Buy hardware in tiers, only when the previous tier feels limiting.
>
> **Realistic total if you eventually buy everything: $1,400 – $2,100** (vs. the old
> $2,400–3,300 plan — the RTX 4090 is no longer the right buy in 2026).

---

## Tier 0: Start Today — $0

Run the whole stack on a machine you already own (any laptop/desktop with 16GB+ RAM).

| Item | Price | Notes |
|------|-------|-------|
| Your existing laptop/desktop | $0 | `docker compose up -d` — CPU mode is the default. STT uses whisper `small` (int8), TTS uses Piper. Expect ~3–5s end-to-end latency. |
| Your laptop's mic + speakers | $0 | Built-in audio works for testing in a quiet room. |
| Your iPhone | $0 | Siri Shortcut → mobile gateway (`docs/iphone-setup.md`) gives you portable JARVIS immediately. |
| OpenAI API key | ~$5–20/mo | The LLM brain. GPT-4.1-mini keeps costs low while you iterate. |
| Tavily API key | Free | 1,000 searches/month free tier covers the background agents. |

**What you get:** full voice pipeline, background agents (newsletter, job monitor),
dashboard, iPhone integration. This is the MVP — prove you actually use it daily
before spending a dollar on hardware.

---

## Tier 1: Dedicated Always-On Box + Real Audio — ~$200–650

When you're tired of running JARVIS on your laptop, give it a home.

| Item | Est. Price | Notes |
|------|-----------|-------|
| **Used mini PC / SFF desktop** (Ryzen 5 / i5, 32GB RAM, 1TB NVMe) | ~$150–400 | A used Dell/Lenovo/HP SFF or a Beelink/Minisforum mini PC handles every CPU service 24/7 at ~15–40W. eBay "Dell OptiPlex 7080" or similar. |
| **ReSpeaker USB Mic Array v2.0** | ~$25 | 4-mic far-field array. Night-and-day better than a laptop mic for wake word across the room. [Seeed Studio](https://www.seeedstudio.com/ReSpeaker-Mic-Array-v2-0.html) |
| **Powered desktop speaker (USB/3.5mm)** | ~$20–40 | Anker Soundcore or JBL Go. |
| *(Alternative)* **Raspberry Pi 5 (16GB) kit** | ~$150 | Can run everything except STT comfortably; whisper `base` on Pi 5 is usable but slow (~5–8s). The used x86 mini PC is the better value. |

**Tier 1 subtotal: ~$200–650**

---

## Tier 2: GPU for Speed, Local LLMs & Vision — ~$450–900

Only buy this when CPU latency annoys you or you want the vision pipeline /
local (API-free) LLMs. **Do not buy an RTX 4090** — 2026 prices make these the
right picks:

| Option | Est. Price | VRAM | Notes |
|--------|-----------|------|-------|
| **RTX 5060 Ti 16GB** (recommended) | ~$449–549 new | 16GB GDDR7 | Best price/performance for local AI in 2026. Runs whisper turbo + Piper/XTTS + YOLO + InsightFace simultaneously, and 7–14B local LLMs. 180W, fits any PSU. |
| **Used RTX 3090** | ~$700 | 24GB | The only sub-$1,000 card that runs 30B+ local LLMs well (936 GB/s bandwidth). Pick this if going fully local (Llama, zero API costs) is the goal. ~350W — needs a real PSU. |
| **Used RTX 3060 12GB** (budget) | ~$200–250 | 12GB | Enough for whisper turbo + vision pipeline. Skip local LLMs. |

If your Tier 1 box can't take a GPU (mini PC), budget another ~$400–500 for a basic
ATX build around it (B650 board + Ryzen 5 + 32GB DDR5 + 650–850W PSU + case) —
still far cheaper than the old 4090 build.

**Tier 2 subtotal: ~$450–900 (GPU only) or ~$850–1,400 (GPU + new build)**

---

## Tier 3: Smart Home — ~$25–125

| Item | Est. Price | Notes |
|------|-----------|-------|
| **Home Assistant** on your Tier 1 box (Docker) | $0 | No Pi needed — run HA Container/Supervised on the existing server. |
| **ESP32-C3 boards (×3)** + USB-C cables | ~$25 | ESPresense BLE room-level presence tracking. ~$5/board. |
| **Zigbee USB Coordinator** *(optional)* | ~$25–30 | SONOFF Zigbee 3.0 Dongle Plus-E, only if you add Zigbee sensors. |
| **Aqara FP2 mmWave presence sensor** *(optional)* | ~$50–80 | Detects stationary presence; better than PIR. |
| **Already owned:** Govee lamps, Ring cameras | $0 | Govee via HA `govee_light_local`; Ring via HA Ring integration. |

**Tier 3 subtotal: ~$25–125**

---

## Tier 4: Vision Cameras — ~$0–220

| Item | Est. Price | Notes |
|------|-----------|-------|
| **Ring cameras** (owned) | $0 | Event-driven snapshots via HA. Unofficial API — can break. |
| **Reolink RLC-810A PoE (×2)** *(when Ring frustrates you)* | ~$100–130 | 4K, native RTSP, no cloud. Pair with Frigate NVR. |
| **8-port PoE switch + Cat6 cables** | ~$65–90 | TP-Link TL-SG1008P. Only with PoE cams. |

Requires Tier 2 GPU for real-time YOLO + face recognition.

---

## Tier 5: Wearables & Multi-Room — ~$500–650 (optional, last)

| Item | Est. Price | Notes |
|------|-----------|-------|
| **Brilliant Labs Frame glasses** | ~$349 | Open-source smart glasses; `glasses_bridge` service is scaffolded for it. Verify availability/successor before ordering — the mobile gateway + iPhone covers most of this use case for $0. |
| **ReSpeaker mic (×2) + Pi Zero 2 W (×2) + USB audio + speakers (×2)** | ~$130–165 | Per-room satellites with Snapcast audio. |

---

## Summary

| Tier | What it unlocks | Cost | When to buy |
|------|----------------|------|-------------|
| **0** | Full voice MVP + agents + iPhone | **$0** | **Today** |
| **1** | Always-on box, far-field audio | $200–650 | When the MVP sticks |
| **2** | Fast STT, local LLMs, vision | $450–900 | When CPU latency annoys you |
| **3** | Smart home control | $25–125 | Anytime after Tier 1 |
| **4** | Camera AI | $0–220 | After Tier 2 |
| **5** | Glasses + multi-room | $500–650 | Last |
| **Total (everything)** | | **~$1,400–2,100** | Spread over months |

---

## Monthly Ongoing Costs

| Service | Cost/month | Notes |
|---------|-----------|-------|
| OpenAI API | ~$5–30 | GPT-4.1-mini for routine queries keeps this at the low end. |
| Anthropic API *(optional)* | ~$10–30 | Alternative/backup LLM. |
| Tavily Search | Free | 1,000 searches/month free tier. |
| Electricity | ~$2–5 (Tier 1) / ~$10–25 (Tier 2 GPU) | Mini PC ≈ 20W idle; GPU box more. |
| **Total** | **~$10–60** | |

> **$0/month path:** Tier 2 with a used 3090 + Llama (Ollama/vLLM) + faster-whisper +
> Piper = zero API costs. Quality drops slightly vs GPT-4.1.

---

## Buying Order (if spreading purchases)

1. **Nothing** — run Tier 0 on your laptop for 2–4 weeks first.
2. **ReSpeaker mic + speaker** (~$50) — biggest UX jump per dollar.
3. **Used mini PC** (~$150–400) — always-on JARVIS.
4. **ESP32 boards** (~$25) — presence tracking; HA runs free on the mini PC.
5. **GPU** (~$450–700) — RTX 5060 Ti 16GB, or used 3090 if going local-LLM.
6. **PoE cameras** (~$165–220) — reliable vision.
7. **Glasses / multi-room** — only once everything else is something you use daily.
