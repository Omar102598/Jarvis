# Hetzner VPS Migration Runbook (Month 3 core/edge split)

Prepped 2026-07-17 alongside `docker-compose.core.yml` (VPS) and
`docker-compose.edge.yml` (Mac). **Untested until the box exists** — follow in
order, verify each checkpoint before the next step. Rollback at the bottom is
always one command away.

## Why

One Mac runs everything today: disk-fill crash loops, sleep = Jarvis offline,
remote access depends on a laptop. The split puts the always-on brain
(llm_agent, agent_runner, redis, mosquitto, chroma, synapse, gateway,
dashboard, ring_mqtt, vision) on a ~$18/mo VPS; the Mac keeps microphone/
speaker/HA/mac_bridge.

## 0. Buy the box

- **Hetzner CPX31** (4 vCPU / 8 GB / 160 GB, ~$18/mo) — Ashburn (US East) is
  the closest region to Austin. CPX21 (4 GB) is too tight for Chroma + Whisper.
- Alternative $0 experiment: Oracle Cloud Always-Free Ampere (4 OCPU / 24 GB,
  ARM — the stack is ARM-clean since it already runs on Apple Silicon).
- OS: Ubuntu 24.04 LTS.

## 1. Base setup (VPS)

```bash
adduser jarvis && usermod -aG sudo jarvis        # no root operation
apt update && apt install -y docker.io docker-compose-v2 git ufw
usermod -aG docker jarvis
# Tailscale — the ONLY way in; no public ports.
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up          # log in with the same tailnet as the Mac/iPhone
```

Firewall — everything closed except SSH + tailnet:

```bash
ufw default deny incoming
ufw allow in on tailscale0
ufw allow OpenSSH        # optionally restrict to tailscale0 too once verified
ufw enable
```

Checkpoint: `tailscale status` on the Mac shows the VPS; `ssh jarvis@<vps-tailnet>` works.

## 2. Repo + data over

```bash
# On the VPS
git clone <repo> ~/Jarvis && cd ~/Jarvis
# From the Mac — copy env + stateful data the core services need:
scp .env jarvis@<vps>:~/Jarvis/.env
rsync -av data/ring-mqtt/ jarvis@<vps>:~/Jarvis/data/ring-mqtt/     # Ring token
rsync -av data/mcp_auth/  jarvis@<vps>:~/Jarvis/data/mcp_auth/      # food-MCP OAuth
rsync -av models/hf/      jarvis@<vps>:~/Jarvis/models/hf/          # Whisper (skip = re-downloads)
rsync -av models/insightface/ jarvis@<vps>:~/Jarvis/models/insightface/
```

Redis + Chroma data: fresh start is acceptable (memory rebuilds), but to keep
memories/journal do: `docker exec jarvis-redis redis-cli BGSAVE`, copy the
volume dump + `chroma_data` volume across (`docker run --rm -v ...` tar dance),
BEFORE first `up` on the VPS.

## 3. VPS .env deltas

Append to `~/Jarvis/.env` on the VPS:

```
MAC_HOST=omars-macbook-pro.tail7e74a7.ts.net
HA_URL=http://omars-macbook-pro.tail7e74a7.ts.net:8123
GATEWAY_PUBLIC_URL=http://<vps-tailnet-name>:8080
```

## 4. Start core on the VPS

```bash
docker compose -f docker-compose.core.yml up -d --build
docker compose -f docker-compose.core.yml logs -f llm_agent   # watch tool count
```

Checkpoint: from the Mac, `curl http://<vps-tailnet>:8080/health` returns OK;
dashboard loads at `http://<vps-tailnet>:8888`.

## 5. Switch the Mac to edge mode

```bash
# On the Mac, in the repo:
echo 'JARVIS_CORE_HOST=<vps-tailnet-name>' >> .env
docker compose down                                   # stop the full stack
docker compose -f docker-compose.edge.yml up -d --build
```

Native services (launchd/scripts outside Docker) need their MQTT/Redis hosts
pointed at the VPS: **tts_mac**, native wake/STT helpers, **audio_player** —
`MQTT_HOST=<vps-tailnet-name>` (and `REDIS_HOST` where used; redis is on 6379
at the VPS, note the Mac's old host port was 6380). `mac_bridge` itself changes
NOTHING — it just starts receiving calls from the VPS instead of localhost.

Checkpoint sweep (in order):
1. Wake word → answer spoken in the room (voice loop across the tailnet).
2. iPhone app with Server URL `http://<vps-tailnet>:8080` — chat + history.
3. "Check the living room camera" (ring_mqtt on VPS → snapshot → vision faceID).
4. "Turn on the living room lights" (VPS → HA on the Mac over tailnet).
5. "Forge, list the repo files" (VPS agent_runner → mac_bridge over tailnet).
6. Dashboard cost widget updating; QA agent green next morning.

## 6. Known refactor debt (check before cutover)

`grep -rn "host.docker.internal" services/ --include="*.py"` — every hit must
either honor `MAC_BRIDGE_HOST`/`MAC_BRIDGE_PORT` env (most do) or be fixed.
Status at prep time: `agent_runner/main.py` fixed (2026-07-17);
`llm_agent/tools/mac.py`, `developer.py`, `self_modify.py`, `plugins.py`,
`spotify_desktop.py`, `dashboard/main.py` (logs proxy), and several
agent_runner agents still default to `host.docker.internal` — verify each
reads the env var, else a one-line fix per file. Good Forge sweep task.

Voice latency note: the wake→STT→brain→TTS loop gains ~1-2 RTTs to US-East.
If it feels sluggish, STT stays local (it does, in edge) and the answer is the
planned local fast-tier — do not move STT to the VPS.

## 7. Rollback

The old all-on-Mac file is untouched:

```bash
# On the Mac
docker compose -f docker-compose.edge.yml down
docker compose up -d          # full stack back on the Mac
# On the VPS
docker compose -f docker-compose.core.yml down
```

Remove `JARVIS_CORE_HOST` from the Mac .env and revert native services'
MQTT_HOST. Nothing in the repo needs reverting.

## 8. Later hardening (post-migration)

- Caddy on the VPS for HTTPS on gateway+dashboard (roadmap 3a) — only needed
  if anything ever leaves the tailnet; inside Tailscale, WireGuard already
  encrypts.
- Nightly `redis-cli BGSAVE` + rsync of `data/` back to the Mac (offsite-ish
  backup both directions).
- Watchdog: adapt `scripts/watchdog.sh` to run on the VPS (systemd timer).
