#!/usr/bin/env bash
# =============================================================================
# Dashboard preview — run the dashboard from the WORKING TREE on the Mac.
# =============================================================================
# Forge edits dashboard files on the Mac, but the live dashboard runs on the
# GCP VM. Screenshotting the VM shows the DEPLOYED page, not the edit just made,
# which makes a verify loop pointless: it looks at someone else's work.
#
# This serves the working tree locally on :8899 so a screenshot of
# http://localhost:8899 is the edit itself. Redis/MQTT still point at the VM
# via .env, so widgets and agent data are real.
#
#   ./scripts/dash_preview.sh start|stop|status|restart
#
# Deliberately NOT a replacement for deploying: Forge cannot reach the VM, so
# shipping is still commit -> push -> pull -> rebuild by a human or a session.
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/.venv-dash"
PORT="${DASH_PREVIEW_PORT:-8899}"
PIDFILE="$REPO_DIR/.dash_preview.pid"
LOG="$REPO_DIR/logs/dash_preview.log"

mkdir -p "$(dirname "$LOG")"

cmd_status() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "[dash-preview] running (pid $(cat "$PIDFILE")) on http://localhost:$PORT"
    return 0
  fi
  # Trust the port, not just the pidfile — a stale pidfile and a live process
  # are the same bug that let two speaker_verify processes run at once.
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[dash-preview] something is already listening on :$PORT (not ours)"
    return 0
  fi
  echo "[dash-preview] not running"
  return 1
}

cmd_stop() {
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE")"
    kill "$pid" 2>/dev/null && echo "[dash-preview] stopped pid $pid"
    rm -f "$PIDFILE"
  fi
  # Sweep by port too, for the same reason as above.
  for p in $(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true); do
    kill "$p" 2>/dev/null && echo "[dash-preview] stopped stray pid $p on :$PORT"
  done
  sleep 1
}

cmd_start() {
  if cmd_status >/dev/null 2>&1; then
    echo "[dash-preview] already running — use restart to pick up code changes"
    cmd_status
    return 0
  fi
  [ -x "$VENV/bin/python" ] || {
    echo "[dash-preview] missing $VENV — create it with:"
    echo "  python3 -m venv .venv-dash && .venv-dash/bin/pip install -r services/dashboard/requirements.txt"
    exit 1
  }

  # The dashboard reads REDIS_HOST/MQTT_HOST from the environment; .env already
  # points them at the VM over the tailnet, so the preview shows real data.
  set -o allexport
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +o allexport

  cd "$REPO_DIR/services/dashboard"
  nohup "$VENV/bin/python" -m uvicorn main:app \
      --host 127.0.0.1 --port "$PORT" --reload \
      > "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 4
  if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "[dash-preview] started (pid $(cat "$PIDFILE")) → http://localhost:$PORT"
    echo "[dash-preview] log: $LOG"
  else
    echo "[dash-preview] FAILED to start — last lines of $LOG:"
    tail -15 "$LOG"
    rm -f "$PIDFILE"
    exit 1
  fi
}

case "${1:-start}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  restart) cmd_stop; cmd_start ;;
  *) echo "usage: $0 start|stop|status|restart"; exit 1 ;;
esac
