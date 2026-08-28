#!/usr/bin/env bash
# =============================================================================
# One-time OAuth sign-in for Google's official hosted MCPs (Gmail, Calendar)
# =============================================================================
# Google's MCP servers (gmailmcp/calendarmcp.googleapis.com) authenticate
# against accounts.google.com, which does NOT support dynamic client
# registration — you must bring your own OAuth client. One-time GCP setup:
#
#   1. console.cloud.google.com → create (or reuse) a project
#   2. Enable APIs: Gmail API, Google Calendar API
#      + MCP services:  gcloud services enable gmailmcp.googleapis.com \
#                            calendarmcp.googleapis.com
#   3. OAuth consent screen: External, add yourself as a test user,
#      scopes: .../auth/gmail.modify and .../auth/calendar
#   4. Credentials → Create OAuth client ID → type **Desktop app**
#      (Desktop clients get refresh tokens by default and allow the
#      loopback redirect mcp-remote uses — do NOT pick "Web application")
#
# Then run this script with the client id + secret. It writes the client file
# to ./data/mcp_auth (which the llm_agent container sees at /data/mcp_auth)
# and runs the browser sign-in for each service. Afterwards:
#   docker compose restart llm_agent
#
# Usage:
#   ./scripts/authorize_google_mcps.sh <client_id> <client_secret>
#   ./scripts/authorize_google_mcps.sh              # reuse saved client file
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export MCP_REMOTE_CONFIG_DIR="$REPO_ROOT/data/mcp_auth"
mkdir -p "$MCP_REMOTE_CONFIG_DIR"
CLIENT_FILE="$MCP_REMOTE_CONFIG_DIR/google_oauth_client.json"

if [[ $# -ge 2 ]]; then
  printf '{"client_id":"%s","client_secret":"%s"}\n' "$1" "$2" > "$CLIENT_FILE"
  chmod 600 "$CLIENT_FILE"
  echo "Wrote $CLIENT_FILE"
elif [[ ! -f "$CLIENT_FILE" ]]; then
  echo "No saved client file. Usage: $0 <client_id> <client_secret>" >&2
  echo "(Create a Desktop-app OAuth client in Google Cloud Console first — see header.)" >&2
  exit 1
fi

authorize() {
  local name="$1" url="$2" scope="$3"
  echo ""
  echo "=== $name: starting OAuth flow (a browser window will open) ==="
  npx -y -p mcp-remote@latest mcp-remote-client "$url" \
    --static-oauth-client-info "@$CLIENT_FILE" \
    --static-oauth-client-metadata "{\"scope\":\"$scope\"}" || {
    echo "!!! $name authorization failed — see output above." >&2
    return 1
  }
  echo "=== $name: tokens saved to $MCP_REMOTE_CONFIG_DIR ==="
}

authorize "Gmail" "https://gmailmcp.googleapis.com/mcp/v1" \
  "https://www.googleapis.com/auth/gmail.modify"
authorize "Google Calendar" "https://calendarmcp.googleapis.com/mcp/v1" \
  "https://www.googleapis.com/auth/calendar"

echo ""
echo "Done. Restart the agent so it picks up the new servers:"
echo "  docker compose restart llm_agent"
