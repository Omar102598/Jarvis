# iPhone / Siri Shortcut Setup Guide

This guide lets you talk to JARVIS from your iPhone exactly like Siri — press
the side button (or say "Hey Siri, ask JARVIS…"), speak your command, and hear
JARVIS reply in his voice.

---

## How it works

```
iPhone mic → Siri Shortcut → POST /ask/audio → JARVIS mobile_gateway
                                                     │
                             faster-whisper (STT) ◄──┘
                                     │
                             MQTT → llm_agent (GPT-4.1 + tools)
                                     │
                             Piper TTS → WAV audio
                                     │
                         iPhone plays the audio ◄─── HTTP response
```

The `mobile_gateway` Docker service exposes an HTTPS-ready REST API.
No app installation is required — only a Siri Shortcut.

---

## Prerequisites

| Item | Details |
|------|---------|
| JARVIS backend running | `docker compose up -d` |
| Gateway reachable from iPhone | Same Wi-Fi network, or port-forwarded / behind a reverse proxy |
| Gateway URL | `http://<your-server-ip>:8080` (replace with your server's LAN IP) |
| API key set | `MOBILE_API_KEY` in your `.env` |

---

## Step 1 — Configure the backend

In your `.env` file set a strong random secret:

```env
MOBILE_API_KEY=replace-with-a-long-random-string
MOBILE_GATEWAY_PORT=8080
```

Then restart the gateway:

```bash
docker compose up -d mobile_gateway
```

Verify it's working:

```bash
curl http://<server-ip>:8080/health
# → {"status":"ok","service":"jarvis-mobile-gateway"}
```

---

## Step 2 — Find your server's LAN IP

On the machine running JARVIS:

```bash
# Linux / macOS
hostname -I | awk '{print $1}'
```

Note this IP address (e.g. `192.168.1.42`).

For convenience, assign a static IP to the server in your router's DHCP
settings so the address never changes.

---

## Step 3 — Create the Siri Shortcut

Open the **Shortcuts** app on your iPhone and tap **+** to create a new
shortcut. Add the following actions **in order**:

### Action 1 — Record audio (your voice command)

1. Search for and add **"Record Audio"** (or **"Record Voice Memo"**).
2. Set **Quality** to *Medium* or *High*.
3. Set **Start Recording** to *Immediately* (optional — makes it feel snappier).

### Action 2 — Ask JARVIS (HTTP request)

1. Search for and add **"Get Contents of URL"**.
2. Fill in the fields:

   | Field | Value |
   |-------|-------|
   | **URL** | `http://192.168.1.42:8080/ask/audio` *(use your IP)* |
   | **Method** | `POST` |
   | **Request Body** | `Form` |

3. Under **Request Body → Form** add one key:

   | Key | Value |
   |-----|-------|
   | `audio` | Tap **Variable** → choose the **Recorded Audio** from Action 1 |

4. Tap **Add new field** → **Header** and add:

   | Key | Value |
   |-----|-------|
   | `X-API-Key` | *(paste your MOBILE_API_KEY value)* |

### Action 3 — Play JARVIS's reply

1. Search for and add **"Play Sound"**.
2. Tap the variable picker and choose **Contents of URL** (the WAV audio
   returned by Action 2).

### Save the shortcut

- Name it something like **"Ask JARVIS"**.
- Tap the settings icon → **Add to Home Screen** to put it on your home screen.
- Or add it to the **Siri Suggestions** widget.

---

## Step 4 — Activate with your voice (optional but recommended)

You can trigger the shortcut hands-free:

1. Open **Settings → Siri & Search**.
2. Make sure **"Listen for Hey Siri"** is on.
3. Open the **Shortcuts** app → long-press **Ask JARVIS** → **Details**.
4. Tap **Add to Siri** and record a phrase, e.g. *"Ask JARVIS"*.

Now you can say **"Hey Siri, Ask JARVIS"** and your iPhone will record your
voice, send it to JARVIS, and play back the spoken reply — just like Siri,
but powered by JARVIS.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `{"detail":"Invalid or missing X-API-Key"}` | Check that the key in your Shortcut header matches `MOBILE_API_KEY` in `.env` exactly |
| `{"detail":"No speech detected in audio"}` | Speak closer to the mic; make sure the recording is not empty |
| `{"detail":"JARVIS did not respond in time."}` | Verify the `llm_agent` container is running: `docker compose logs llm_agent` |
| Shortcut shows a network error | Confirm your phone and server are on the same Wi-Fi; check that port 8080 is open |
| No audio plays | Check that the **Play Sound** action receives the **Contents of URL** variable (not the text representation) |

---

## Testing without an iPhone (curl)

**Voice:**
```bash
curl -X POST http://192.168.1.42:8080/ask/audio \
     -H "X-API-Key: your-key" \
     -F "audio=@/path/to/command.wav" \
     --output reply.wav && afplay reply.wav
```

**Text:**
```bash
curl -X POST http://192.168.1.42:8080/ask/text \
     -H "X-API-Key: your-key" \
     -H "Content-Type: application/json" \
     -d '{"text": "What is the weather like today?"}' \
     --output reply.wav && afplay reply.wav
```

---

## Security notes

- The API key is a shared secret. Keep it out of version control.
- For remote access (outside your home Wi-Fi), put the gateway behind a
  reverse proxy (nginx / Caddy) with TLS and consider IP allowlisting.
- Do **not** expose the MQTT or Redis ports to the internet.
