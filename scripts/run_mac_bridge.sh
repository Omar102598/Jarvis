#!/usr/bin/env bash
# =============================================================================
# JARVIS macOS Bridge — native HTTP server for laptop control
# =============================================================================
# Runs on the Mac host at port 7777. The llm_agent container reaches it via
# host.docker.internal:7777.
#
# Usage:
#   ./scripts/run_mac_bridge.sh          # start
#   ./scripts/run_mac_bridge.sh stop     # stop
#   ./scripts/run_mac_bridge.sh status   # check
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/.venv-mac-bridge"
PID_FILE="$REPO_DIR/.mac_bridge.pid"
LOG_FILE="$REPO_DIR/logs/mac_bridge.log"
mkdir -p "$(dirname "$LOG_FILE")"

if [ -f "$REPO_DIR/.env" ]; then
  set -o allexport
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +o allexport
fi

export MAC_BRIDGE_PORT="${MAC_BRIDGE_PORT:-7777}"
export MAC_SHELL_ALLOWLIST="${MAC_SHELL_ALLOWLIST:-}"

cmd_stop() {
  if [ ! -f "$PID_FILE" ]; then echo "[bridge] Not running."; return; fi
  pid=$(cat "$PID_FILE")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    # TERM alone is not enough. The FastAPI lifespan handler closes the
    # Playwright context on shutdown, and when the browser is wedged that
    # await never returns — the process sits there holding the port while the
    # PID file gets removed, so the next start would bring up a second bridge
    # against a port that is still occupied. Wait, then escalate.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "[bridge] Still alive after 10s (browser likely wedged) — sending KILL"
      kill -9 "$pid" 2>/dev/null
      sleep 1
    fi
    echo "[bridge] Stopped (pid $pid)"
  else
    echo "[bridge] Process already gone."
  fi
  rm -f "$PID_FILE"
}

cmd_status() {
  if [ ! -f "$PID_FILE" ]; then echo "[bridge] Not running."; return; fi
  pid=$(cat "$PID_FILE")
  if kill -0 "$pid" 2>/dev/null; then
    echo "[bridge] Running on port $MAC_BRIDGE_PORT (pid $pid)"
  else
    echo "[bridge] Dead (stale PID file)."
    rm -f "$PID_FILE"
  fi
}

cmd_start() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "[bridge] Already running (pid $(cat "$PID_FILE"))."
    exit 0
  fi

  if [ ! -d "$VENV" ]; then
    echo "[bridge] Creating virtualenv..."
    python3 -m venv "$VENV"
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"

  echo "[bridge] Installing dependencies..."
  pip install --quiet --upgrade pip
  pip install --quiet -r "$REPO_DIR/services/mac_bridge/requirements.txt"

  # Install Playwright's Chromium browser (only downloads if not already present)
  echo "[bridge] Checking Playwright Chromium..."
  "$VENV/bin/playwright" install chromium 2>&1 | grep -v "^$" || true

  echo "[bridge] Starting on port $MAC_BRIDGE_PORT..."
  nohup "$VENV/bin/python" "$REPO_DIR/services/mac_bridge/main.py" \
    > "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 1

  if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "[bridge] Running on http://0.0.0.0:$MAC_BRIDGE_PORT (pid $(cat "$PID_FILE"))"
    echo "[bridge] Reachable from Docker at host.docker.internal:$MAC_BRIDGE_PORT"
    echo "[bridge] Log: $LOG_FILE"
  else
    echo "[bridge] Failed to start — check $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
  fi
}

case "${1:-start}" in
  stop)   cmd_stop ;;
  status) cmd_status ;;
  start)  cmd_start ;;
  *)      echo "Usage: $0 [start|stop|status]"; exit 1 ;;
esac
