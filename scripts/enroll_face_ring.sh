#!/usr/bin/env bash
# =============================================================================
# Enroll a face using a Ring camera — "Jarvis, learn what I look like"
# =============================================================================
# Stand 3-6 ft in front of the camera, facing it, decent light. The script
# pulls N FRESH frames from the camera's RTSP stream (each takes ~5-20s —
# Ring wakes the camera on connect), and for each one tells the vision
# service to extract a face embedding. The samples are then averaged into
# the face:{name} identity Sentry's face recognition compares against.
# Move slightly between frames (turn head a little) for a robust average.
#
# Usage:
#   ./scripts/enroll_face_ring.sh                     # omar @ Living Room
#   ./scripts/enroll_face_ring.sh omar cc3bfb8c0cc3 8 # name, device, frames
#
# Verify afterwards:  docker logs jarvis-vision --since 5m
# Re-run any time to REPLACE the enrollment (new haircut, glasses, etc).
# =============================================================================
set -euo pipefail

NAME="${1:-omar}"
DEVICE="${2:-cc3bfb8c0cc3}"   # Living Room
FRAMES="${3:-6}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="$(grep '^MOBILE_API_KEY=' "$REPO_ROOT/.env" | cut -d= -f2-)"
GATEWAY="http://localhost:8080"
KEEP_DIR="$REPO_ROOT/data/face_enrollment/$NAME"
mkdir -p "$KEEP_DIR"

# Start from a clean sample buffer so a re-run fully replaces the identity
docker exec jarvis-redis redis-cli del "face_samples:$NAME" > /dev/null

echo "Enrolling '$NAME' from camera $DEVICE — stand in front of it now."
captured=0
for i in $(seq 1 "$FRAMES"); do
  echo "[$i/$FRAMES] grabbing a fresh frame (5-20s)..."
  if ! curl -sf -X POST "$GATEWAY/ring/snapshot/$DEVICE/refresh?k=$KEY" > /dev/null; then
    echo "  frame grab failed — camera busy/offline, skipping"
    continue
  fi
  # Keep a copy for the record (and future re-enrollment from disk)
  curl -sf "$GATEWAY/ring/snapshot/$DEVICE.jpg?k=$KEY" -o "$KEEP_DIR/ring_$(date +%s).jpg" || true
  # Ask the vision service to embed the face in this frame
  docker exec jarvis-mqtt mosquitto_pub -t jarvis/vision/enroll \
    -m "{\"name\":\"$NAME\",\"device\":\"$DEVICE\"}"
  captured=$((captured+1))
  sleep 3
done

if [ "$captured" -eq 0 ]; then
  echo "No frames captured — is the camera online? (docker logs jarvis-ring-mqtt)"
  exit 1
fi

echo "Finalizing enrollment..."
sleep 2
docker exec jarvis-mqtt mosquitto_pub -t jarvis/vision/enroll_finalize \
  -m "{\"name\":\"$NAME\"}"
sleep 2

echo ""
echo "Vision service log (look for \"enrolled '$NAME'\" — samples with no"
echo "detectable face are skipped, that's normal for a couple of frames):"
docker logs jarvis-vision --since 3m 2>&1 | grep -i "enroll\|face" | tail -8 || true
echo ""
echo "Done. Sentry now gets 'Face recognition: $NAME (enrolled household"
echo "member)' whenever this face appears on any Ring camera."
