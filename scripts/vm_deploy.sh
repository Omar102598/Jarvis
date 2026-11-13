#!/usr/bin/env bash
# Deploy listener on the VM host — the last step of Forge's Ship flow.
#
# agent_runner (forge_cloud.py) writes jarvis:deploy:request once a merged PR's
# images are built. This script, run by cron every minute, picks it up, pulls
# develop and restarts exactly the services named. It runs on the HOST because
# that's where docker compose and the checkout live; containers can't restart
# themselves (agent_runner may be one of the targets).
#
# Install (on the VM, once):
#   mkdir -p ~/Jarvis/logs     # cron cannot open the log otherwise, and never runs the script
#   chmod +x ~/Jarvis/scripts/vm_deploy.sh
#   (crontab -l 2>/dev/null; echo '* * * * * $HOME/Jarvis/scripts/vm_deploy.sh >> $HOME/Jarvis/logs/deploy.log 2>&1') | crontab -
# Pause auto-deploys: touch ~/Jarvis/.deploy-paused

set -uo pipefail
cd "$HOME/Jarvis" || exit 1
exec 9>/tmp/jarvis-deploy.lock
flock -n 9 || exit 0
[ -e .deploy-paused ] && exit 0

COMPOSE="docker compose -f docker-compose.core.yml"
redis() { docker exec jarvis-redis redis-cli "$@"; }

REQ=$(redis GET jarvis:deploy:request 2>/dev/null)
[ -z "$REQ" ] && exit 0
redis DEL jarvis:deploy:request >/dev/null      # claim it: one deploy per request

field() { python3 -c 'import json,sys; v=json.loads(sys.argv[1]).get(sys.argv[2]); print(" ".join(v) if isinstance(v,list) else ("" if v is None else v))' "$REQ" "$1"; }
SHA=$(field sha); PR=$(field pr); TITLE=$(field title)
SERVICES=$(field services); RESTART_RUNNER=$(field restart_agent_runner); NATIVE=$(field native)
echo "$(date -Is) deploy PR #$PR ${SHA:0:7}: services=[$SERVICES] restart_runner=$RESTART_RUNNER"

notify() {   # title, text, urgency
  docker exec jarvis-agent-runner python -c '
import sys
from notify import route_notification
route_notification("Forge", sys.argv[2], title=sys.argv[1], urgency=sys.argv[3])' "$1" "$2" "${3:-normal}" \
    || echo "notify failed"
}

fail() {
  echo "$(date -Is) FAILED: $1"
  redis SET jarvis:deploy:last "$(python3 -c 'import json,sys,time; print(json.dumps({"pr":sys.argv[1],"sha":sys.argv[2],"ok":False,"error":sys.argv[3],"ts":time.time()}))' "$PR" "$SHA" "$1")" >/dev/null
  notify "🔴 Deploy failed — PR #$PR" "$TITLE: $1. Nothing else was changed." urgent
  exit 1
}

git pull -q --ff-only origin develop || fail "git pull failed (local changes on the VM?)"
git merge-base --is-ancestor "$SHA" HEAD || fail "merge commit ${SHA:0:7} is not on develop"

DONE=()
for svc in $SERVICES; do
  $COMPOSE pull -q "$svc" || fail "image pull failed for $svc"
  $COMPOSE up -d --no-build "$svc" || fail "restart failed for $svc"
  DONE+=("$svc")
done
if [ "$RESTART_RUNNER" = "True" ] && [[ " $SERVICES " != *" agent_runner "* ]]; then
  $COMPOSE restart agent_runner || fail "agent_runner restart failed"
  DONE+=("agent_runner (restart)")
fi

MSG="Live on the VM (${SHA:0:7})."
[ ${#DONE[@]} -gt 0 ] && MSG="$MSG Restarted: ${DONE[*]}."
[[ " $SERVICES " == *" llm_agent "* ]] && MSG="$MSG The brain needs ~75s to come back."
[ -n "$NATIVE" ] && MSG="$MSG Mac services still need a restart: $NATIVE."
redis SET jarvis:deploy:last "$(python3 -c 'import json,sys,time; print(json.dumps({"pr":sys.argv[1],"sha":sys.argv[2],"ok":True,"msg":sys.argv[3],"ts":time.time()}))' "$PR" "$SHA" "$MSG")" >/dev/null
echo "$(date -Is) OK: $MSG"
sleep 5   # let a restarted agent_runner accept the notify exec
notify "🚀 Shipped — $TITLE" "$MSG"
