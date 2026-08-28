# JARVIS on GCP — live deployment (2026-08-28)

The core/edge split is **live**. This is what actually runs, superseding the
provider-specific parts of `docs/HETZNER_SETUP.md` (kept for its migration
order, checkpoint list, and rollback, which are provider-agnostic).

Hetzner was the original plan; the account got locked during signup screening,
Oracle's free tier never had ARM capacity (~1,870 failed attempts over 3 days),
so this runs on GCP against the $300 free-trial credit.

## Topology

```
GCP  jarvis-core.tail7e74a7.ts.net  (us-south1-a, e2-standard-2, 2 vCPU / 8 GB)
  mosquitto · redis · chroma · llm_agent · agent_runner
  mobile_gateway · dashboard · synapse · vision · ring_mqtt

Mac  omars-macbook-pro.tail7e74a7.ts.net
  homeassistant (drives LAN devices — must stay local)
  native: wake_word · stt · speaker_verify · tts_mac · mac_bridge

iPhone  Server URL → http://jarvis-core.tail7e74a7.ts.net:8080
```

Everything crosses the tailnet. Nothing is publicly exposed: GCP firewall
denies inbound `tcp:22` from `0.0.0.0/0` and allows it only from `100.64.0.0/10`.

## Cost

- VM ≈ $49/mo + 100 GB pd-balanced ≈ $10/mo → **~$59/mo**, covered by the
  $300 credit for roughly 5 months.
- Budget alert: **$50/mo**, thresholds 50 / 90 / 100 %. Alerts only — GCP
  budgets notify, they do not cap spend. Wire a Pub/Sub shutdown if you want
  a hard stop.

## Key facts that bit us

- **Redis port differs by side.** The Mac published `6380:6379`; the VM
  publishes `6379:6379`. `REDIS_PORT` in the Mac's `.env` must be `6379` now,
  or the native services connect to nothing.
- **MCP URLs must be env-expanded.** `home_assistant` hardcoded
  `http://homeassistant:8123`, which only resolves inside the Mac's compose
  network — on the VM it silently loaded 0 tools (213 → 193). Fixed by
  expanding `${HA_URL}` in `url`/`command`/`args` (`mcp_loader.py`).
- **Stale `.audio_pids`.** `run_audio_native.sh` refuses to start if the PID
  file is stale — always `./scripts/run_audio_native.sh stop` first.
- **GHCR package visibility is separate from repo visibility.** Making the
  repo public does not make the images pullable; flip each `jarvis-*` package
  to public.

## Routine operations

```bash
# ssh (tailnet only)
ssh -i ~/.ssh/jarvis_cloud omar@jarvis-core.tail7e74a7.ts.net

# deploy a merged change
cd ~/Jarvis && git pull origin develop
docker compose -f docker-compose.core.yml pull
docker compose -f docker-compose.core.yml up -d --no-build

# native audio on the Mac
./scripts/run_audio_native.sh stop && ./scripts/run_audio_native.sh
```

Config-only changes (`config/`) need just `git pull` — that directory is
volume-mounted. Service code needs a CI image build, then `pull`.

## Rollback

The Mac's all-in-one compose is untouched:

```bash
# Mac: remove the edge-mode block from .env, then
docker compose up -d
./scripts/run_audio_native.sh stop && ./scripts/run_audio_native.sh
# GCP: docker compose -f docker-compose.core.yml down
```

## Verified at cutover

Brain round-trip · lights via HA-on-Mac over tailnet · 213 tools (full parity)
· mac_bridge reachable from the VM (Forge/browser) · 2 Ring cameras · 3
dashboard widgets · 16 agents scheduled · 307 Redis keys, 24 memories, 16
journal entries migrated intact.

**Not yet verified: the spoken voice loop** — needs someone physically present.
