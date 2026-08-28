# Jarvis Room Satellite — per-room ears

A ~$45/room mic satellite (Raspberry Pi Zero 2 W + ReSpeaker 2-Mics Pi HAT)
that gives Jarvis ears in any room. It only does wake-word + record + play —
Whisper, the brain, and the voice all run on the Jarvis server via the mobile
gateway, so the Pi stays cheap and cool.

**Flow:** "Hey Jarvis" → beep → speak → reply plays through the HAT/speaker.
Replies route as that room's voice surface (`satellite-<room>`), so the Mac
won't double-speak them, and a follow-up window lets you keep talking without
re-waking.

## Hardware (per room)

| Item | ~Price |
|------|--------|
| Raspberry Pi Zero 2 W | $15 |
| ReSpeaker 2-Mics Pi HAT (Seeed) | $13 |
| microSD 16–32 GB | $8 |
| USB-C power supply | $8 |
| Small speaker → HAT's JST 2.0 speaker jack or 3.5mm | $0–15 |

Any USB mic + USB/3.5mm speaker also works (skip the HAT driver step).

## Setup

1. **Flash Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager — set the
   hostname (`jarvis-livingroom`), enable SSH, and add your Wi-Fi in the imager.

2. **ReSpeaker HAT driver** (skip for USB audio):
   ```bash
   sudo apt update && sudo apt install -y git
   git clone https://github.com/HinTak/seeed-voicecard   # maintained fork
   cd seeed-voicecard && sudo ./install.sh && sudo reboot
   # verify after reboot: arecord -l  (shows seeed-2mic-voicecard)
   ```

3. **Satellite client:**
   ```bash
   sudo apt install -y python3-pip python3-venv libportaudio2
   python3 -m venv ~/jarvis && ~/jarvis/bin/pip install \
       openwakeword sounddevice soundfile numpy requests
   # copy jarvis_satellite.py to the Pi (scp from the repo)
   scp satellite/jarvis_satellite.py pi@jarvis-livingroom.local:~/
   ```

4. **Configure** — `/etc/jarvis-satellite.env`:
   ```ini
   GATEWAY_URL=http://<mac-lan-ip>:8080     # or the Tailscale URL
   MOBILE_API_KEY=<same key as the iOS app>
   JARVIS_ROOM=livingroom
   ```

5. **Run at boot** — `/etc/systemd/system/jarvis-satellite.service`:
   ```ini
   [Unit]
   Description=Jarvis room satellite
   After=network-online.target sound.target

   [Service]
   EnvironmentFile=/etc/jarvis-satellite.env
   ExecStart=/home/pi/jarvis/bin/python /home/pi/jarvis_satellite.py
   Restart=always
   RestartSec=5
   User=pi

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   sudo systemctl enable --now jarvis-satellite
   journalctl -u jarvis-satellite -f     # watch it
   ```

6. **Test:** "Hey Jarvis, what time is it?" — beep, answer plays on the Pi.

## Tuning

| Env | Default | Meaning |
|-----|---------|---------|
| `WAKE_THRESHOLD` | 0.5 | raise if false wakes, lower if it misses you |
| `SILENCE_RMS` | 300 | mic quiet threshold (raise in noisy rooms) |
| `SILENCE_SECS` | 1.2 | pause length that ends your utterance |
| `FOLLOWUP_WINDOW` | 6.0 | seconds to keep listening after a reply (0 = off) |

## Notes

- The gateway must be reachable from the Pi (same LAN or Tailscale on the Pi).
- Server-side requirements (already in the main stack): the gateway's
  `/ask/audio` accepts a `room` form field, and `satellite-*` rooms are
  excluded from the Mac speaker + treated as remote voice surfaces.
- Speaker verification note: satellite audio goes through the gateway's Whisper
  path (same as the iOS app), not the Mac's speaker_verify chain.
