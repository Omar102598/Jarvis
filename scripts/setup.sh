#!/usr/bin/env bash
# ==============================================================================
# JARVIS — Initial Setup Script
# ==============================================================================
# Run this once after cloning the repository to set up the environment.
# Usage: bash scripts/setup.sh
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== JARVIS Setup ==="
echo ""

# --- 1. Create .env from template ---
# The wizard creates .env, prompts for the keys that matter, and validates each
# one against the real service. Hand-editing the template is still possible but
# is how people end up debugging a typo'"'"'d key as if it were a broken install.
if command -v python3 &> /dev/null; then
    python3 "$SCRIPT_DIR/setup_wizard.py" || true
elif [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "✓ Created .env from .env.example"
    echo "  → python3 not found; edit .env by hand with your API keys"
fi

# --- 2. Create required directories ---
echo ""
echo "Creating directories..."

dirs=(
    "$PROJECT_DIR/models"
    "$PROJECT_DIR/models/speaker_model"
    "$PROJECT_DIR/data/speaker_enrollment"
    "$PROJECT_DIR/data/face_enrollment"
    "$PROJECT_DIR/data/snapshots"
    "$PROJECT_DIR/data/audio_cache"
)

for dir in "${dirs[@]}"; do
    mkdir -p "$dir"
    echo "  ✓ $dir"
done

# --- 3. Check Docker ---
echo ""
if command -v docker &> /dev/null; then
    echo "✓ Docker found: $(docker --version)"
else
    echo "✗ Docker not found. Install Docker: https://docs.docker.com/get-docker/"
fi

if command -v docker compose &> /dev/null; then
    echo "✓ Docker Compose found: $(docker compose version)"
else
    echo "✗ Docker Compose not found."
fi

# --- 4. Check NVIDIA GPU (optional) ---
echo ""
if command -v nvidia-smi &> /dev/null; then
    echo "✓ NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true
else
    echo "• No NVIDIA GPU detected (STT, TTS, and Vision will need GPU)"
fi

# --- 5. Download models ---
echo ""
echo "To download ML models, run:"
echo "  python scripts/download_models.py"
echo ""

# --- 6. Start infrastructure ---
echo "To start JARVIS infrastructure:"
echo "  docker compose up -d mosquitto redis"
echo ""
echo "To start the full voice pipeline:"
echo "  docker compose up -d"
echo ""
echo "To include vision services:"
echo "  docker compose --profile vision up -d"
echo ""
echo "To include wearable bridge:"
echo "  docker compose --profile wearable up -d"
echo ""

echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Re-run 'python3 scripts/setup_wizard.py' any time to add integrations"
echo "  2. Run: python scripts/download_models.py"
echo "  3. Run: docker compose up -d"
echo "  4. Run: python scripts/test_pipeline.py"
