#!/usr/bin/env bash
# =============================================================================
# One-time OAuth sign-in for the food-delivery MCP servers (Uber Eats, DoorDash)
# =============================================================================
# The official hosted MCP servers use interactive OAuth: a browser window opens
# so you can sign in to your account. That can't happen inside the headless
# llm_agent container, so this script runs the flow HERE on the Mac and stores
# the resulting tokens in ./data/mcp_auth — which the container sees at
# /data/mcp_auth (the ./data volume mount). mcp-remote inside the container
# then reuses/refreshes those tokens headlessly.
#
# Usage:
#   ./scripts/authorize_food_mcps.sh            # authorize both
#   ./scripts/authorize_food_mcps.sh ubereats   # just Uber Eats
#   ./scripts/authorize_food_mcps.sh doordash   # just DoorDash
#
# Afterwards restart the agent:  docker compose restart llm_agent
#
# Re-run this script if a server starts getting skipped at startup again
# (i.e. the refresh token itself expired or you revoked access).
#
# NOTE: neither auth server advertises dynamic client registration, so if the
# flow fails at the registration step, mcp-remote supports supplying a
# pre-registered client instead — add to BOTH this script's npx line and the
# matching args in config/mcp_servers.yml:
#   --static-oauth-client-info '{"client_id":"<id>"}'
# (Get a client id from the provider's developer portal if needed.)
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export MCP_REMOTE_CONFIG_DIR="$REPO_ROOT/data/mcp_auth"
mkdir -p "$MCP_REMOTE_CONFIG_DIR"

UBEREATS_URL="https://mcp.ubereats.com/eats-claude/mcp"
DOORDASH_URL="https://openapi.doordash.com/mcp/consumer"

authorize() {
  local name="$1" url="$2"
  echo ""
  echo "=== $name: starting OAuth flow (a browser window will open) ==="
  # mcp-remote-client connects once as a test client, which triggers the OAuth
  # browser flow and persists tokens into MCP_REMOTE_CONFIG_DIR. Ctrl-C after
  # it reports a successful connection if it keeps running.
  npx -y -p mcp-remote@latest mcp-remote-client "$url" || {
    echo "!!! $name authorization failed — see output above." >&2
    return 1
  }
  echo "=== $name: tokens saved to $MCP_REMOTE_CONFIG_DIR ==="
}

target="${1:-all}"
case "$target" in
  ubereats)  authorize "Uber Eats" "$UBEREATS_URL" ;;
  doordash)  authorize "DoorDash"  "$DOORDASH_URL" ;;
  all)
    authorize "Uber Eats" "$UBEREATS_URL"
    authorize "DoorDash"  "$DOORDASH_URL"
    ;;
  *) echo "Usage: $0 [ubereats|doordash|all]" >&2; exit 1 ;;
esac

echo ""
echo "Done. Restart the agent so it picks up the new servers:"
echo "  docker compose restart llm_agent"
