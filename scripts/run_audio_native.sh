#!/usr/bin/env bash
# =============================================================================
# JARVIS — Native macOS Audio Services Launcher
# =============================================================================
# Runs wake_word, stt, speaker_verify, tts, and audio_player natively so
# your Mac mic and speakers are used directly (Docker can't access /dev/snd).
#
# Prerequisites installed automatically if missing:
#   brew install portaudio ffmpeg espeak-ng
#
# Usage:
#   ./scripts/run_audio_native.sh          # start all services
#   ./scripts/run_audio_native.sh stop     # kill them
#   ./scripts/run_audio_native.sh status   # check if running
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/.venv-audio"
PIDS_FILE="$REPO_DIR/.audio_pids"
LOG_DIR="$REPO_DIR/logs/audio"
mkdir -p "$LOG_DIR"

# --- env from .env file ---
if [ -f "$REPO_DIR/.env" ]; then
  set -o allexport
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +o allexport
fi

export MQTT_HOST="${MQTT_HOST:-localhost}"
export MQTT_PORT="${MQTT_PORT:-1883}"
export REDIS_HOST="${REDIS_HOST:-localhost}"
export REDIS_PORT="${REDIS_PORT:-6380}"
export ROOM_NAME="${ROOM_NAME:-office}"
export WAKE_THRESHOLD="${WAKE_THRESHOLD:-0.5}"
export VERIFY_MODE="${VERIFY_MODE:-log}"
export VERIFY_THRESHOLD="${VERIFY_THRESHOLD:-0.25}"
export STT_DEVICE="${STT_DEVICE:-cpu}"
export STT_MODEL="${STT_MODEL:-small}"
export PIPER_MODEL="${PIPER_MODEL:-en_GB-alan-medium}"
export AUDIO_DIR="$REPO_DIR/data/audio_cache"
export MODEL_DIR="$REPO_DIR/models/speaker_model"

# =============================================================================
# stop
# =============================================================================
cmd_stop() {
  if [ ! -f "$PIDS_FILE" ]; then
    echo "[audio] Not running (no PID file)."
    return
  fi
  echo "[audio] Stopping audio services..."
  while IFS=: read -r name pid; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" && echo "  stopped $name (pid $pid)"
    fi
  done < "$PIDS_FILE"
  rm -f "$PIDS_FILE"
  echo "[audio] Done."
}

# =============================================================================
# status
# =============================================================================
cmd_status() {
  if [ ! -f "$PIDS_FILE" ]; then
    echo "[audio] Not running."
    return
  fi
  echo "[audio] Running processes:"
  while IFS=: read -r name pid; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "  ✅  $name  (pid $pid)"
    else
      echo "  ❌  $name  (pid $pid — dead)"
    fi
  done < "$PIDS_FILE"
}

# =============================================================================
# start
# =============================================================================
cmd_start() {
  if [ -f "$PIDS_FILE" ]; then
    echo "[audio] Already running. Use './scripts/run_audio_native.sh stop' first."
    exit 1
  fi

  # ---------- brew deps ----------
  echo "[audio] Checking brew dependencies..."
  if ! command -v brew &>/dev/null; then
    echo "ERROR: Homebrew not found. Install it from https://brew.sh"
    exit 1
  fi
  for pkg in portaudio ffmpeg espeak-ng; do
    if ! brew list "$pkg" &>/dev/null; then
      echo "[audio] Installing $pkg..."
      brew install "$pkg" || true
    fi
  done

  # ---------- virtualenv (Python 3.11 — 3.9 is too old for most ML packages) ----------
  PYTHON311="$(brew --prefix)/bin/python3.11"
  if [ ! -d "$VENV" ]; then
    echo "[audio] Creating virtualenv with Python 3.11..."
    "$PYTHON311" -m venv "$VENV"
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"

  echo "[audio] Installing Python dependencies (this may take a few minutes on first run)..."
  pip install --quiet --upgrade pip
  # TTS uses macOS built-in `say`, so no piper/phonemize needed
  pip install --quiet \
    "paho-mqtt>=1.6,<2.0" \
    "numpy>=1.24" \
    "pyaudio>=0.2.14" \
    "openwakeword>=0.6.0" \
    "faster-whisper>=1.0.0" \
    "speechbrain>=1.0.0" \
    "torch>=2.0.0" \
    "torchaudio>=2.0.0" \
    "soundfile>=0.12.0" \
    "redis>=5.0" \
    "edge-tts>=6.1.9"

  mkdir -p "$AUDIO_DIR" "$MODEL_DIR"

  # ---------- download wake word model ----------
  OWW_MODEL_DIR="$VENV/lib/python3.11/site-packages/openwakeword/resources/models"
  if [ ! -f "$OWW_MODEL_DIR/hey_jarvis_v0.1.onnx" ]; then
    echo "[audio] Downloading 'hey_jarvis' wake word model..."
    "$VENV/bin/python" -c "
from openwakeword.utils import download_models
download_models(['hey_jarvis_v0.1'])
print('hey_jarvis model ready.')
"
  fi

  # ---------- launch services ----------
  echo "[audio] Starting audio services..."
  > "$PIDS_FILE"

  launch() {
    local name=$1; shift
    local script=$1; shift
    "$VENV/bin/python" -u "$script" "$@" \
      > "$LOG_DIR/${name}.log" 2>&1 &
    local pid=$!
    echo "${name}:${pid}" >> "$PIDS_FILE"
    echo "  started $name (pid $pid) → $LOG_DIR/${name}.log"
  }

  launch wake_word      "$REPO_DIR/services/wake_word/main.py"
  launch stt            "$REPO_DIR/services/stt/main.py"
  launch speaker_verify "$REPO_DIR/services/speaker_verify/main.py"
  # TTS: use macOS built-in `say` — no piper required
  launch tts_mac        "$REPO_DIR/services/tts_mac/main.py"

  echo ""
  echo "[audio] All services started. Logs in $LOG_DIR/"
  echo "[audio] Say 'Hey Jarvis' to activate."
  echo ""
  echo "  Stop:   ./scripts/run_audio_native.sh stop"
  echo "  Status: ./scripts/run_audio_native.sh status"
  echo "  Logs:   tail -f $LOG_DIR/wake_word.log"
}

# =============================================================================
# main
# =============================================================================
case "${1:-start}" in
  stop)   cmd_stop ;;
  status) cmd_status ;;
  start)  cmd_start ;;
  *)      echo "Usage: $0 [start|stop|status]"; exit 1 ;;
esac
