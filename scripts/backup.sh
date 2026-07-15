#!/usr/bin/env bash
# =============================================================================
# JARVIS backup — snapshots Redis (state/memory/tasks/journal) + Chroma (vector
# memory) so a disk/AOF failure can't lose everything.
#
# Nightly via launchd (scripts/com.jarvis.backup.plist), or run by hand.
# Keeps the last BACKUP_KEEP (default 7) snapshots in ./backups/.
#
# Restore (manual):
#   Redis:  docker compose stop redis && \
#           docker run --rm -v jarvis_redis_data:/data -v "$PWD/backups":/b \
#             alpine sh -c 'cp /b/redis-<ts>.rdb /data/dump.rdb' && \
#           docker compose up -d redis
#   Chroma: docker compose stop chroma && \
#           docker run --rm -v jarvis_chroma_data:/c -v "$PWD/backups":/b \
#             alpine sh -c 'rm -rf /c/* && tar xzf /b/chroma-<ts>.tgz -C /c' && \
#           docker compose up -d chroma
# =============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_DIR/backups"
LOG="$REPO_DIR/logs/backup.log"
KEEP="${BACKUP_KEEP:-7}"
TS="$(date '+%Y%m%d-%H%M%S')"
mkdir -p "$OUT" "$REPO_DIR/logs"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# --- Redis: trigger a save, then copy the RDB out of the container ------------
if docker exec jarvis-redis redis-cli SAVE >/dev/null 2>&1; then
  if docker cp jarvis-redis:/data/dump.rdb "$OUT/redis-$TS.rdb" >/dev/null 2>&1; then
    log "redis snapshot → redis-$TS.rdb ($(du -h "$OUT/redis-$TS.rdb" | cut -f1))"
  else
    log "redis: SAVE ok but copy failed"
  fi
else
  log "redis: SAVE failed (skipped)"
fi

# --- Chroma: tar the persisted vector store ----------------------------------
if docker exec jarvis-chroma sh -c 'tar czf - -C /chroma chroma 2>/dev/null' \
     > "$OUT/chroma-$TS.tgz" 2>/dev/null && [[ -s "$OUT/chroma-$TS.tgz" ]]; then
  log "chroma snapshot → chroma-$TS.tgz ($(du -h "$OUT/chroma-$TS.tgz" | cut -f1))"
else
  rm -f "$OUT/chroma-$TS.tgz"
  log "chroma: snapshot failed (skipped)"
fi

# --- Rotate: keep the newest $KEEP of each ------------------------------------
for prefix in redis chroma; do
  ls -1t "$OUT/$prefix-"* 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$old" && log "rotated out $(basename "$old")"
  done
done
log "backup complete"
