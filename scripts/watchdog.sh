#!/usr/bin/env bash
# =============================================================================
# JARVIS watchdog — self-healing health checks.
#
# Docker containers already have `restart: always`, so this focuses on the gaps
# that leaves:
#   • native audio procs (wake_word/stt/speaker_verify/tts_mac) dying silently
#     — the ".audio_pids drift" gotcha,
#   • Redis / MQTT unreachable,
#   • any expected container not running.
#
# Idempotent and safe to run on a schedule (cron/launchd), e.g. every 5 min:
#   */5 * * * * /Users/omar/Documents/GitHub/Jarvis/scripts/watchdog.sh >/dev/null 2>&1
#
# Restarts native audio via run_audio_native.sh when a tracked PID is gone, and
# brings any down container back up. Alerts (iMessage via mac_bridge) at most
# once/hour when something needed healing.
# =============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$REPO_DIR/logs/watchdog.log"
PIDS_FILE="$REPO_DIR/.audio_pids"
ALERT_STAMP="/tmp/jarvis_watchdog_alerted"
mkdir -p "$REPO_DIR/logs"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
healed=()

# --- Role: is the core stack local, or on the VM? ----------------------------
# After the move to GCP this script kept "healing" jarvis-llm, redis, mqtt and
# friends ON THE MAC, because it assumed the core stack was always local. Every
# five minutes it started a duplicate of services that now live on the VM — a
# full shadow stack ran here for three days, executing the same scheduled
# agents twice and spending against the same API key.
#
# Rather than hardcode a role, infer it: if REDIS_HOST points somewhere other
# than this machine, the core lives elsewhere and must not be started here.
CORE_HOST="$(grep -E '^REDIS_HOST=' "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d ' \r')"
case "${CORE_HOST:-localhost}" in
  ""|localhost|127.0.0.1|redis|host.docker.internal) ROLE="core" ;;
  *)                                                 ROLE="edge" ;;
esac
COMPOSE_CORE="$REPO_DIR/docker-compose.core.yml"
[[ -f "$COMPOSE_CORE" ]] || COMPOSE_CORE="$REPO_DIR/docker-compose.yml"

# --- 1. Native audio processes ------------------------------------------------
audio_dead=0
if [[ -f "$PIDS_FILE" ]]; then
  while IFS=: read -r name pid; do
    [[ -z "${pid:-}" ]] && continue
    if ! kill -0 "$pid" 2>/dev/null; then
      log "native audio '$name' (pid $pid) is DEAD"
      audio_dead=1
    fi
  done < "$PIDS_FILE"
else
  log "no .audio_pids file — native audio may not be running"
  audio_dead=1
fi
if [[ "$audio_dead" == "1" ]]; then
  log "restarting native audio stack…"
  "$REPO_DIR/scripts/run_audio_native.sh" stop  >/dev/null 2>&1
  "$REPO_DIR/scripts/run_audio_native.sh"       >/dev/null 2>&1 &
  healed+=("native audio")
fi

# --- 1.7 Mac bridge liveness (macOS only) ------------------------------------
# Nothing supervised the bridge until now, and it failed in the one way a port
# check cannot see: the process sat up for 61 days answering /health in 9ms
# while every /browser/* call hung forever behind a wedged Playwright context.
# Remy and Scout had been failing since July because of it. So liveness here
# means the BROWSER answers, not that the port is open.
if [[ "$(uname)" == "Darwin" ]]; then
BRIDGE_PORT="$(grep -E '^MAC_BRIDGE_PORT=' "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d ' \r')"
BRIDGE_PORT="${BRIDGE_PORT:-7777}"
BRIDGE_FAIL_STAMP="/tmp/jarvis_watchdog_bridge_fail"
bridge_bad=""

if ! curl -s -o /dev/null -m 5 "http://localhost:${BRIDGE_PORT}/health" 2>/dev/null; then
  bridge_bad="not answering on :${BRIDGE_PORT}"
else
  # Only probe the browser when one is already running. /browser/url LAUNCHES
  # a headful Chromium if none exists, so probing unconditionally would pop a
  # window onto the desktop every watchdog run. No browser means nothing to be
  # wedged, which is the case we are looking for.
  bridge_pid="$(cat "$REPO_DIR/.mac_bridge.pid" 2>/dev/null || true)"
  if [[ -n "$bridge_pid" ]] && pgrep -P "$bridge_pid" >/dev/null 2>&1; then
    # 25s: a cold Chromium start takes ~10s, a wedged one never returns.
    b_code=$(curl -s -o /dev/null -w '%{http_code}' -m 25 \
             "http://localhost:${BRIDGE_PORT}/browser/url" 2>/dev/null || echo 000)
    [[ "$b_code" == "200" ]] || bridge_bad="browser wedged (/browser/url -> $b_code)"
  fi
fi

if [[ -n "$bridge_bad" ]]; then
  if [[ -f "$BRIDGE_FAIL_STAMP" ]]; then
    log "mac bridge $bridge_bad on two consecutive runs — restarting"
    "$REPO_DIR/scripts/run_mac_bridge.sh" stop  >/dev/null 2>&1
    "$REPO_DIR/scripts/run_mac_bridge.sh" start >/dev/null 2>&1 &
    rm -f "$BRIDGE_FAIL_STAMP"
    healed+=("mac bridge ($bridge_bad)")
  else
    # One strike first: a long navigation or a slow cold start is not a fault.
    log "mac bridge $bridge_bad — first strike, restart if still bad next run"
    touch "$BRIDGE_FAIL_STAMP"
  fi
else
  rm -f "$BRIDGE_FAIL_STAMP"
fi
fi

# --- 1.5 Docker disk pressure (core host only) -------------------------------
if [[ "$ROLE" == "core" ]]; then
# Today's outage: the Docker VM disk filled from repeated builds → Redis AOF
# writes failed → agent_runner crash-looped. Prune BEFORE that happens.
reclaim_gb=$(docker system df --format '{{.Reclaimable}}' 2>/dev/null \
             | grep -oE '[0-9.]+GB' | grep -oE '[0-9.]+' | awk '{s+=$1} END{printf "%d", s}')
if [[ -n "$reclaim_gb" && "$reclaim_gb" -ge "${DISK_PRUNE_THRESHOLD_GB:-20}" ]]; then
  log "docker reclaimable ${reclaim_gb}GB ≥ ${DISK_PRUNE_THRESHOLD_GB:-20}GB — pruning images + build cache"
  docker image prune -af >/dev/null 2>&1
  docker builder prune -af >/dev/null 2>&1
  healed+=("docker disk (~${reclaim_gb}GB)")
fi
# Emergency: if Redis can't even write (AOF full), prune hard right now.
if ! docker exec jarvis-redis redis-cli set __wd_disk ok >/dev/null 2>&1; then
  log "Redis write failed (possible disk-full) — emergency prune"
  docker image prune -af >/dev/null 2>&1
  docker builder prune -af >/dev/null 2>&1
  healed+=("emergency disk prune")
fi
docker exec jarvis-redis redis-cli del __wd_disk >/dev/null 2>&1

fi

# --- 2. Redis / MQTT reachability (core host only) ---------------------------
if [[ "$ROLE" == "core" ]]; then
if ! docker exec jarvis-redis redis-cli ping 2>/dev/null | grep -q PONG; then
  log "Redis not responding — restarting container"
  docker compose -f "$COMPOSE_CORE" -p jarvis up -d redis >/dev/null 2>&1
  healed+=("redis")
fi
if ! docker exec jarvis-mqtt mosquitto_sub -t '$SYS/#' -C 1 -W 3 >/dev/null 2>&1; then
  log "MQTT not responding — restarting container"
  docker compose -f "$COMPOSE_CORE" -p jarvis up -d mosquitto >/dev/null 2>&1
  healed+=("mqtt")
fi

# --- 3. Expected containers up ------------------------------------------------
# Core services only exist on the core host; on the edge these live on the VM.
EXPECTED=(jarvis-llm jarvis-agent-runner jarvis-mobile-gateway jarvis-synapse jarvis-redis jarvis-mqtt)
for c in "${EXPECTED[@]}"; do
  status="$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null || echo missing)"
  if [[ "$status" != "true" ]]; then
    svc="${c#jarvis-}"; svc="${svc//-/_}"
    log "container $c not running ($status) — bringing up $svc"
    docker compose -f "$COMPOSE_CORE" -p jarvis up -d "$svc" >/dev/null 2>&1
    healed+=("$c")
  fi
done
fi   # end core-only block

# --- 3.5 Mobile gateway HTTP liveness (core host only) ------------------------
if [[ "$ROLE" == "core" ]]; then
# The container can be "Up" while hung on the startup Whisper download (model
# loads before uvicorn binds 8080), so .State.Running alone misses it. Two
# strikes (~10 min apart) before restarting, so a legitimate slow model load
# isn't killed mid-startup.
GW_FAIL_STAMP="/tmp/jarvis_watchdog_gw_fail"
if docker inspect -f '{{.State.Running}}' jarvis-mobile-gateway 2>/dev/null | grep -q true; then
  gw_code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://localhost:${MOBILE_GATEWAY_PORT:-8080}/health" 2>/dev/null || echo 000)
  if [[ "$gw_code" != "200" ]]; then
    if [[ -f "$GW_FAIL_STAMP" ]]; then
      log "gateway container Up but /health returned $gw_code twice — restarting (likely hung model load)"
      docker restart jarvis-mobile-gateway >/dev/null 2>&1
      rm -f "$GW_FAIL_STAMP"
      healed+=("mobile-gateway (hung)")
    else
      log "gateway /health returned $gw_code — first strike, restart if still failing next run"
      touch "$GW_FAIL_STAMP"
    fi
  else
    rm -f "$GW_FAIL_STAMP"
  fi
fi

fi

# --- 4. Alert (at most once/hour) --------------------------------------------
if [[ ${#healed[@]} -gt 0 ]]; then
  log "HEALED: ${healed[*]}"
  now=$(date +%s); last=0
  [[ -f "$ALERT_STAMP" ]] && last=$(cat "$ALERT_STAMP" 2>/dev/null || echo 0)
  if (( now - last > 3600 )); then
    echo "$now" > "$ALERT_STAMP"
    phone=$(docker exec jarvis-redis redis-cli get user:profile 2>/dev/null \
            | python3 -c "import sys,json;print(json.load(sys.stdin).get('imessage_to','')) if sys.stdin else print('')" 2>/dev/null || echo "")
    if [[ -n "$phone" ]]; then
      msg="🔧 Jarvis watchdog healed: ${healed[*]}"
      script="tell application \"Messages\" to send \"$msg\" to buddy \"$phone\" of (service 1 whose service type is iMessage)"
      curl -s -m 15 -X POST http://localhost:7777/applescript \
        -H 'Content-Type: application/json' \
        -d "$(python3 -c "import json,sys;print(json.dumps({'script':sys.argv[1],'timeout':20}))" "$script")" \
        >/dev/null 2>&1
    fi
  fi
else
  log "all healthy"
fi
