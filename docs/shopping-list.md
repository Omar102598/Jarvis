# JARVIS Shopping List

> **Total estimated budget: $2,400 – $3,300**
> Organized by build phase. Start with Phase 1 to get a working voice assistant.

---

## Phase 1: Voice Core — ~$1,800-2,300

### GPU Server Build

| Item | Est. Price | Link | Notes |
|------|-----------|------|-------|
| **NVIDIA RTX 4090 24GB** | ~$1,599 | [Amazon](https://www.amazon.com/s?k=rtx+4090) / [Newegg](https://www.newegg.com/p/pl?d=rtx+4090) | The heart of JARVIS. Runs LLM + STT + TTS + vision simultaneously. MSI, ASUS, or Gigabyte are all fine. |
| **AMD Ryzen 7 7800X3D** or **Intel i7-14700K** | ~$300-350 | [Amazon (Ryzen)](https://www.amazon.com/s?k=ryzen+7+7800x3d) / [Amazon (Intel)](https://www.amazon.com/s?k=i7-14700k) | CPU handles orchestration, MQTT, Redis. Either brand works. |
| **64GB DDR5 RAM (2×32GB)** | ~$130-160 | [Amazon](https://www.amazon.com/s?k=64gb+ddr5+ram) | Corsair Vengeance or G.Skill Trident recommended. |
| **2TB NVMe SSD** | ~$120-150 | [Amazon](https://www.amazon.com/s?k=2tb+nvme+ssd) | Samsung 990 Pro or WD Black SN850X. Stores models + recordings. |
| **750W+ ATX PSU (80+ Gold)** | ~$90-120 | [Amazon](https://www.amazon.com/s?k=750w+80+gold+psu) | Corsair RM750 or EVGA SuperNOVA. Must support 4090 power requirements. |
| **ATX Case** | ~$70-100 | [Amazon](https://www.amazon.com/s?k=atx+mid+tower+case) | Good airflow. Fractal Design Meshify C or NZXT H7 Flow. |
| **Motherboard (AM5 or LGA1700)** | ~$150-200 | [Amazon](https://www.amazon.com/s?k=am5+motherboard+b650) | Match to CPU. B650 for AMD, B760 for Intel. |
| **CPU Cooler** | ~$30-50 | [Amazon](https://www.amazon.com/s?k=tower+cpu+cooler) | Thermalright Peerless Assassin or be quiet! Dark Rock 4. |

**Server subtotal: ~$1,490-2,130**

> **Alternative:** If you already own a Mac Studio M4 Ultra ($3,999+) it can handle most workloads via MLX. Slower GPU inference but much quieter.

### Audio Hardware (First Room)

| Item | Est. Price | Link | Notes |
|------|-----------|------|-------|
| **ReSpeaker USB Mic Array v2.0** | ~$25 | [Seeed Studio](https://www.seeedstudio.com/ReSpeaker-Mic-Array-v2-0.html) / [Amazon](https://www.amazon.com/s?k=respeaker+mic+array+v2) | 4-mic array, far-field capture, USB plug-and-play. |
| **Desktop Speaker (USB/3.5mm)** | ~$20-40 | [Amazon](https://www.amazon.com/s?k=usb+desktop+speaker) | Any powered speaker. Anker Soundcore or JBL Go. |

**Audio subtotal: ~$45-65**

---

## Phase 2: Smart Home — ~$100-175

| Item | Est. Price | Link | Notes |
|------|-----------|------|-------|
| **Raspberry Pi 5 (8GB)** | ~$80 | [PiShop](https://www.pishop.us/product/raspberry-pi-5-8gb/) / [Amazon](https://www.amazon.com/s?k=raspberry+pi+5+8gb) | Runs Home Assistant OS. Or run HA as VM/Docker on GPU server (free). |
| **RPi 5 power supply + case + SD card** | ~$30-40 | [Amazon](https://www.amazon.com/s?k=raspberry+pi+5+starter+kit) | 27W USB-C PSU, official case, 64GB+ microSD. |
| **ESP32-C3 boards (×3)** | ~$12-15 | [Amazon](https://www.amazon.com/s?k=esp32-c3+dev+board) | For ESPresense BLE room tracking. ~$4-5 each. |
| **USB-C cables for ESP32s** | ~$10 | — | Power the ESP32 boards via USB. |
| **Zigbee USB Coordinator** *(optional)* | ~$25-30 | [Amazon](https://www.amazon.com/s?k=sonoff+zigbee+3.0+usb+dongle+plus+e) | SONOFF Zigbee 3.0 USB Dongle Plus-E. Only needed if adding Zigbee sensors. |

**Smart home subtotal: ~$100-175**

> **You already own Govee lamps** — no purchase needed. They integrate via Home Assistant's `govee_light_local`.

---

## Phase 3: Vision & Cameras — ~$0-220

| Item | Est. Price | Link | Notes |
|------|-----------|------|-------|
| **Ring cameras** | $0 | *(already owned)* | Use via HA Ring integration. Event-driven snapshots. |
| **Reolink RLC-810A PoE Camera (×2)** *(recommended upgrade)* | ~$100-130 | [Amazon](https://www.amazon.com/s?k=reolink+rlc-810a) / [Reolink Store](https://reolink.com/product/rlc-810a/) | 4K, native RTSP, no cloud dependency. ~$55-65 each. Use with Frigate NVR. |
| **PoE Network Switch (8-port)** *(if adding PoE cams)* | ~$50-70 | [Amazon](https://www.amazon.com/s?k=8+port+poe+network+switch) | TP-Link TL-SG1008P. Powers cameras via ethernet. |
| **Ethernet cables (Cat6)** | ~$15-20 | [Amazon](https://www.amazon.com/s?k=cat6+ethernet+cable+pack) | For PoE cameras. |

**Vision subtotal: ~$0 (Ring only) or ~$165-220 (with PoE cameras)**

> PoE cameras are optional but strongly recommended. Ring's unofficial API can break at any time.

---

## Phase 4: Wearable — ~$350

| Item | Est. Price | Link | Notes |
|------|-----------|------|-------|
| **Brilliant Labs Frame** | ~$349 | [Brilliant Labs Store](https://www.brilliantlabs.com/products/frame) | Open-source smart glasses. Camera, mic, OLED display, BLE. MIT-licensed Python SDK. |

**Wearable subtotal: ~$350**

> **Consider this optional / later.** A phone app can serve as a portable JARVIS interface in the meantime.

---

## Phase 5: Multi-Room — ~$150-250

| Item | Est. Price | Link | Notes |
|------|-----------|------|-------|
| **ReSpeaker USB Mic Array v2.0 (×2 more)** | ~$50 | [Seeed Studio](https://www.seeedstudio.com/ReSpeaker-Mic-Array-v2-0.html) | One per additional room. |
| **Raspberry Pi Zero 2 W (×2)** | ~$30 | [PiShop](https://www.pishop.us/product/raspberry-pi-zero-2-w/) / [Amazon](https://www.amazon.com/s?k=raspberry+pi+zero+2+w) | Snapcast audio clients. ~$15 each. |
| **USB audio adapters (×2)** | ~$10-15 | [Amazon](https://www.amazon.com/s?k=usb+audio+adapter) | For Pi Zero speaker output. |
| **Speakers (×2 more rooms)** | ~$40-60 | [Amazon](https://www.amazon.com/s?k=small+powered+speaker) | One per additional room. |
| **Aqara FP2 mmWave Presence Sensor (×1-2)** *(optional)* | ~$50-80 | [Amazon](https://www.amazon.com/s?k=aqara+fp2+presence+sensor) | Detects presence even when stationary. Better than PIR motion sensors. |

**Multi-room subtotal: ~$150-250**

---

## Summary

| Phase | Description | Cost | Priority |
|-------|-------------|------|----------|
| **Phase 1** | GPU Server + First Room Voice | $1,800-2,300 | **Start here** |
| **Phase 2** | Smart Home Hub + Presence | $100-175 | High |
| **Phase 3** | Vision (Ring first, PoE later) | $0-220 | Medium |
| **Phase 4** | Brilliant Labs Frame Glasses | $350 | Optional / Later |
| **Phase 5** | Multi-Room Expansion | $150-250 | After core works |
| **TOTAL** | | **$2,400-3,300** | |

---

## Monthly Ongoing Costs

| Service | Cost/month | Notes |
|---------|-----------|-------|
| OpenAI API (GPT-4.1) | ~$10-30 | LLM brain, usage-based |
| Anthropic API (Claude) | ~$10-30 | Alternative/backup LLM |
| ElevenLabs *(optional)* | $5-22 | Premium TTS. Free tier available. |
| Tavily Search API | Free | 1,000 searches/month on free tier |
| Electricity (GPU server) | ~$15-30 | RTX 4090 at ~300W average |
| **TOTAL** | **~$20-80** | |

> **$0/month option:** Run everything locally with Llama 3.3 + faster-whisper + XTTS + Piper. Quality drops slightly but zero API costs.

---

## Savings Priority Order

If you need to spread purchases over time, buy in this order:

1. **RTX 4090** — $1,599 — Biggest single expense. Watch for sales / refurbished.
2. **Rest of server** — $400-530 — CPU, RAM, SSD, PSU, case, motherboard, cooler.
3. **ReSpeaker mic + speaker** — $45-65 — Gets you started immediately.
4. **ESP32 boards (×3)** — $15 — Flash ESPresense for room tracking.
5. **Raspberry Pi 5 for HA** — $110 — Or run HA free on the GPU server.
6. **PoE cameras (×2)** — $165-220 — When you want reliable vision AI.
7. **Brilliant Labs Frame** — $349 — When you want portable JARVIS.
8. **Multi-room gear** — $150-250 — After everything else is solid.
