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
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "✓ Created .env from .env.example"
    echo "  → Edit .env with your API keys and configuration"
else
    echo "• .env already exists, skipping"
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
echo "  1. Edit .env with your API keys"
echo "  2. Run: python scripts/download_models.py"
echo "  3. Run: docker compose up -d"
echo "  4. Run: python scripts/test_pipeline.py"
