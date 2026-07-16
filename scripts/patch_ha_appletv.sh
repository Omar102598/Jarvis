#!/usr/bin/env bash
# =============================================================================
# Re-apply the Apple TV Docker-networking patches to the HA container.
#
# WHY: HA's apple_tv integration scans via HA's shared AsyncZeroconf
# (aiozc=aiozc). Through the Docker bridge that scan finds NOTHING, while
# pyatv's own sockets find devices instantly. This broke BOTH pairing (config
# flow: "no_devices_found") and the runtime connection ("not connected to
# None", entities stuck off / select_source unsupported).
#
# These edits live INSIDE the container image and are LOST when the
# homeassistant image updates / the container is recreated. Re-run this script
# (then it restarts HA) whenever Apple TVs go "unavailable" after an HA update.
# Idempotent — safe to run repeatedly.
# =============================================================================
set -euo pipefail

C=jarvis-homeassistant
BASE=/usr/src/homeassistant/homeassistant/components/apple_tv

# 1. Config flow (pairing): drop aiozc, bump timeout a touch
docker exec "$C" sed -i \
  's/scan_result = await scan(loop, timeout=3, hosts=_host_filter(), aiozc=aiozc)/scan_result = await scan(loop, timeout=5, hosts=_host_filter())/' \
  "$BASE/config_flow.py"

# 2. Runtime connection (__init__.py): drop the aiozc kwarg line
docker exec "$C" sed -i '/^            aiozc=aiozc,$/d' "$BASE/__init__.py"

echo "Patched. Current scan calls:"
docker exec "$C" grep -n "scan_result = await scan" "$BASE/config_flow.py"
docker exec "$C" grep -A5 "atvs = await scan(" "$BASE/__init__.py" | head -7

echo "Restarting Home Assistant…"
docker restart "$C" >/dev/null
echo "Done — give HA ~60s to reconnect the Apple TVs."
