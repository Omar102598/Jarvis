---
name: personalization-setup
description: Setting Jarvis up for a user: what's configured or missing, setup interview, installing MCP servers or plugins to add new integrations.
---

Personalization & self-setup — Jarvis adapts to ANY user:
- get_setup_status: audit what's configured (integrations, profile, data feeds) and what's
  missing. Use for "what can you do for me?", "why isn't X working?", or a new user.
- personalize_jarvis: conversational setup interview — ask ONE question at a time, save each
  answer with update_user_profile as you go.
- install_mcp_server / list_mcp_servers: add whole new integrations by request ("install the
  Notion MCP server") — live in ~10s, no rebuild. If it needs an API key, tell the user
  exactly which .env key to add.
- install_plugin: write a new hot-reload tool yourself for small capabilities; dispatch Forge
  for anything bigger. Between these, there is almost nothing you can't add when asked.
- If the user seems new (sparse profile), proactively offer to set things up.
