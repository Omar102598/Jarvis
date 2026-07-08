# Jarvis for Other Users — Distribution + Unified Billing Plan

*Researched 2026-07-07. This is a handoff document: any model/engineer should be able to execute from it without re-deriving the analysis.*

## Goal

Let other people set up Jarvis easily, pay **one bill**, and still enable/disable any agent, tool, or MCP server — without losing the customizability the single-user install has today.

---

## 1. Current-state findings

### Architecture (relevant facts)

- Docker Compose microservices: mosquitto, redis, chroma, wake_word, stt, speaker_verify, llm_agent, tts, vision, mobile_gateway, agent_runner, dashboard, glasses_bridge, home-assistant, ring-mqtt. Plus host-side pieces (mac_bridge, tts_mac) and native apps (JarvisApp iOS, JarvisDesktop).
- **Per-agent toggles already exist**: `config/agents.yml` has `enabled: true/false` per background agent with params. `config/mcp_servers.yml` has the same per MCP server. This is the foundation for "add or disable agents/tools" — nothing new needed conceptually, only an entitlement layer on top.
- **User namespacing already exists**: `JARVIS_USER_ID` namespaces memory and conversation history. Single-user today but the seam is there.
- **LLM access is already centralized** in `services/llm_agent/llm_factory.py` (`build_llm()` / `resolve_model()`), used by main agent, sub-agents, and agent_runner via `llm_helper.py`. One choke point = easy to redirect to a proxy.
- All secrets flow through one `.env` + compose passthrough.

### Complete key/credential inventory (from `.env`, `.env.example`, and code grep)

Classify every credential into one of three buckets — this classification drives the whole design:

| Bucket | Credential | Service | Cost model |
|---|---|---|---|
| **A. Metered, centralizable** (Jarvis can hold ONE master key and resell usage) | `ANTHROPIC_API_KEY` | Claude (main brain, agents, Forge) | per-token |
| | `OPENAI_API_KEY` | GPT fallback | per-token |
| | `TAVILY_API_KEY` | web search | per-request |
| | `ELEVENLABS_API_KEY` | voice TTS | per-character |
| | `GOOGLE_MAPS_API_KEY` / `GOOGLE_API_KEY` | Maps MCP, geocoding | per-request |
| | `TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM` | SMS | per-message |
| | `BANKSYNC_API_KEY` + `BANKSYNC_MCP_URL` | bank/finance data | per-connection/month (Plaid-like) |
| **B. Personal identity credentials** (must be the USER's own account; never centralizable; mostly free) | `SPOTIFY_CLIENT_ID/SECRET/REFRESH_TOKEN` | Spotify OAuth | free |
| | `RING_REFRESH_TOKEN`, `RING_RTSP_BASE`, `RING_LIVE_USER` | Ring cameras | free (user's sub) |
| | `IMAP_USER/PASSWORD`, `SMTP_*` | email triage (Hermes) | free |
| | `GITHUB_TOKEN` | GitHub MCP/tool | free |
| | `SLACK_BOT_TOKEN/TEAM_ID` | Slack MCP | free |
| | `HA_URL/HA_TOKEN` | Home Assistant | free (local) |
| | `GOVEE_API_KEY` | Govee lights | free |
| **C. Self-generated / infra** (wizard generates; no billing) | `MOBILE_API_KEY`, `ROOM_NAME`, `JARVIS_USER_ID`, `CAMERA_URLS`, MQTT/Redis hosts, STT/TTS/wake settings | — | free/local |

**Key insight:** the "one bill" problem only applies to bucket A (7 services). Bucket B is inherently per-user (you can't centralize someone's Gmail or Spotify login) and is free, so it belongs in the *setup wizard*, not the billing system. Bucket C is generated automatically.

### Free-by-default design already in place

STT (Whisper local), wake word, Piper TTS, Chroma memory, vision, speaker verify all run locally at zero marginal cost. A new user with **zero** bucket-A keys still gets a working voice assistant — only degraded (no web search, macOS `say`-quality voice, GPT/Claude unavailable → needs at least one LLM path). This matters for the pricing tiers below.

---

## 2. Recommended architecture: "Jarvis Cloud Gateway" (key broker + meter)

One hosted service you (Omar) run. Users get a single `JARVIS_TOKEN`. All bucket-A traffic flows through it; it injects the master keys, records usage, and Stripe turns usage into one monthly invoice.

```
User's Jarvis install                    Jarvis Cloud Gateway (you host)
┌──────────────────────┐                ┌──────────────────────────────────┐
│ llm_factory.py ──────┼──LLM calls────►│ LiteLLM proxy                    │──► Anthropic
│ (base_url override)  │                │  • virtual keys per user         │──► OpenAI
│                      │                │  • per-key budgets/rate limits   │
│ web_search.py ───────┼──/relay/tavily►│ Relay service (FastAPI)          │──► Tavily
│ tts (elevenlabs) ────┼──/relay/11labs►│  • path-mirrors upstream APIs    │──► ElevenLabs
│ google_maps MCP ─────┼──/relay/maps──►│  • injects master key            │──► Google Maps
│ comms.py (twilio) ───┼──/relay/twilio►│  • emits usage events            │──► Twilio
│ finance (banksync) ──┼──/relay/bank──►│  • checks entitlements           │──► BankSync
└──────────────────────┘                │                                  │
  auth: JARVIS_TOKEN                    │ Metering (OpenMeter/Stripe       │
  (one env var replaces                 │  Meters) ──► Stripe usage-based  │
   7 API keys)                          │  billing ──► ONE invoice         │
                                        └──────────────────────────────────┘
```

### Why this shape

- **LiteLLM proxy** (open source, self-hosted) already does exactly the LLM half: virtual API keys per user, spend tracking per key/model, budgets, rate limits, Anthropic+OpenAI passthrough with prompt caching. Don't build this yourself. It exposes an OpenAI/Anthropic-compatible endpoint, so the only Jarvis change is `base_url` + key in `llm_factory.py`.
  - Interim zero-infra option: **OpenRouter** gives one key/one bill for all LLM providers today with no hosting — but it can't cover Tavily/ElevenLabs/Maps/Twilio, and you can't add margin. Fine as a stopgap or a "BYOK-lite" recommendation in docs.
- **Relay service** for the non-LLM APIs is small: for each provider, mirror the upstream REST surface under `/relay/<provider>/…`, swap `Authorization`, forward, and emit a usage event `{user, provider, units, ts}`. ~50 lines per provider in FastAPI/httpx. Every one of these SDKs/clients supports a base-URL override (Tavily: `api_base`; ElevenLabs SDK: `base_url`; google Maps MCP: needs the relay to mimic Google's host — simplest is to run that MCP server *on the gateway* instead, see below; Twilio SDK: custom `http_client`/edge config).
- **Entitlements = the existing toggles, mirrored server-side.** The gateway stores per-user flags (`tavily: on, elevenlabs: off, banksync: on`). Disabling a paid tool in the dashboard flips the local `agents.yml`/`mcp_servers.yml` toggle **and** the gateway entitlement, so a disabled tool can't bill even if misconfigured. Enabling a paid tool shows its price before confirming.
- **Billing: Stripe usage-based billing (Meters)**. One meter per provider (llm_tokens, tavily_searches, tts_chars, sms_messages, maps_requests, bank_connections). Price each with a small margin over cost (this is standard practice — you're carrying the credit risk and the infra). BankSync is a flat monthly add-on per connection rather than metered. Result: **one Stripe invoice per user**, itemized by service, and each line item disappears when they disable the tool.
- **Budget safety**: LiteLLM per-key budgets + a gateway-level monthly cap per user (configurable, e.g. default $25 hard stop with notification) prevents surprise bills — the same problem you already hit yourself (ambient agent disabled 2026-07-01 for idle token spend). Expose the spend widget on the dashboard (there's already a finance widget pattern to copy: producers write `widget:{name}:data` to Redis).

### MCP servers with keys

`google_maps` MCP currently spawns locally with the key in env. Two options; prefer (1):
1. **Host key-bearing MCP servers on the gateway** as `streamable_http` remotes — `mcp_servers.yml` already supports `transport: streamable_http` with `headers: Authorization: Bearer ${JARVIS_TOKEN}`. The registry entry just changes from stdio+key to http+token. Metering happens naturally at the gateway.
2. Relay-and-rewrite the upstream host for the local stdio server (fragile).

Keyless MCPs (fetch, playwright, memory, airbnb, sequential_thinking) stay exactly as they are — local, free, user-toggleable.

---

## 3. Setup experience for a new user

### Tiers (let the user choose; don't force the paid path)

1. **Managed (recommended, "one bill")** — sign up at your gateway, get `JARVIS_TOKEN`, done. All bucket-A services work instantly, pay-as-you-go on one invoice.
2. **BYOK (free/self-managed)** — user pastes their own keys per service, exactly like today. The wizard links to each provider's key page. Zero revenue but zero liability, and it keeps the OSS story clean. `llm_factory.py` should prefer direct keys when present so BYOK is just "don't set JARVIS_TOKEN".
3. **Hybrid** — JARVIS_TOKEN for most things, own key for specific services (e.g. they already have an Anthropic account). Resolution order per provider: own key → JARVIS_TOKEN → disabled.

### Onboarding wizard (`jarvis setup` / dashboard first-run page)

Sequence:
1. **Identity & basics**: name, `JARVIS_USER_ID`, timezone, room name(s). Generate `MOBILE_API_KEY` and other bucket-C secrets automatically.
2. **Billing choice**: Managed (opens signup → paste token) / BYOK (per-service key prompts, each skippable) / Hybrid.
3. **Personal accounts (bucket B)**: optional, each skippable — Spotify OAuth flow, Gmail app-password walkthrough, Ring token, Home Assistant URL+token, GitHub PAT. These gate which agents the wizard offers to enable.
4. **Agent & MCP selection**: show the roster from `agents.yml`/`mcp_servers.yml` with descriptions, *estimated monthly cost* per paid agent (e.g. "Newsletter: ~$0.40/mo in LLM tokens + Tavily"), and dependency notes ("Email Triage needs the Gmail step"). Write the `enabled:` flags.
5. **Validation pass**: ping each configured service, report green/red, offer fixes. Then `docker compose up`.

Implementation: the wizard is a page in the existing dashboard service (it already volume-mounts config) plus a `scripts/setup.sh` bootstrap for the CLI path. It writes `.env` and the two YAML files — no new config formats.

### Packaging

- One-command install: `curl … | bash` or `git clone && ./scripts/setup.sh` → checks Docker, copies `.env.example`, launches wizard, `docker compose up -d`.
- Split compose into profiles: `core` (mqtt/redis/chroma/llm/stt/tts/wake/dashboard) vs optional (`vision`, `homeassistant`, `ring-mqtt`, `glasses`) using compose `profiles:` so a minimal install doesn't pull HA + ring images.
- Mac-host components (mac_bridge, tts_mac) and JarvisApp are optional extras with their own short docs; core must work without them.
- Clean the repo for distribution: `.bak` files, personal defaults in `agents.yml` (Omar's job-search keywords, newsletter topics, schedule times) move to a `config/agents.defaults.yml` template that the wizard personalizes.

---

## 4. Execution plan (phased, each phase independently shippable)

### Phase 1 — BYOK setup wizard + packaging (no billing infra; biggest UX win)
- Setup wizard (dashboard page + CLI script), config templating, compose profiles, validation pings, docs. Repo hygiene (strip personal params, remove `.bak` files, secrets audit of git history before making anything public).
- Also: per-provider key resolution order (own key → token → disabled) scaffolded in code now, even before the gateway exists.
- **No dependency on you hosting anything.** Ship this first; it alone makes Jarvis installable by others.

### Phase 2 — LLM unification via LiteLLM proxy
- Deploy LiteLLM (the Month-3 VPS from ROADMAP.md is the natural home). Master Anthropic+OpenAI keys live only there.
- Jarvis change: `llm_factory.py` honors `JARVIS_TOKEN` + `JARVIS_GATEWAY_URL` (LangChain `ChatAnthropic(base_url=…)` / `ChatOpenAI(base_url=…)`). ~30 lines.
- Per-user virtual keys, budgets, spend dashboard. This covers ~80% of a typical user's spend (LLM tokens dominate).

### Phase 3 — Relay for non-LLM metered APIs
- FastAPI relay on the same VPS: Tavily, ElevenLabs, Twilio, BankSync. Move google_maps MCP to gateway-hosted streamable_http.
- Client-side: base-URL overrides in `web_search.py`, the ElevenLabs TTS path, `comms.py`, finance agent. Usage events to OpenMeter (or plain Postgres table first — volume is tiny).

### Phase 4 — Billing + entitlements
- Stripe: customer per user, usage-based prices per meter, BankSync as flat add-on. Monthly cap with hard stop + notification.
- Entitlement API on gateway; dashboard agent-toggle UI calls it alongside flipping the YAML. Show live month-to-date spend per service on the dashboard.

### Phase 5 — Hosted/multi-tenant (optional, later)
- `JARVIS_USER_ID` already namespaces memory/history; a hosted variant (your VPS runs llm_agent+agent_runner per user, thin local audio clients) becomes feasible. Not needed for the "one bill" goal — defer.

### Effort guess
Phase 1: ~2–4 focused days. Phase 2: ~1 day (LiteLLM is mostly config). Phase 3: ~2–3 days. Phase 4: ~2–3 days plus Stripe account setup. Forge (`spawn_task agent="developer"`) can execute most of the code changes per project convention.

---

## 5. Open questions for Omar (decide before executing)

1. **Legal/ToS check** on reselling access: Anthropic/OpenAI generally allow this via their commercial terms when you're providing an application (you're billing for *Jarvis usage*, not raw API resale) — but ElevenLabs/Tavily/Twilio terms should be skimmed before Phase 3. Framing the product as "Jarvis credits" rather than "API passthrough" is both cleaner legally and simpler to price.
2. **Pricing model**: pure pay-as-you-go with margin, or subscription with included allowance (e.g. $15/mo includes N tokens/searches)? Subscription+allowance is simpler for users, PAYG is simpler to build. Recommendation: PAYG with a monthly cap first (it falls out of the metering for free), add plans later.
3. **Where the gateway lives**: the ROADMAP Month-3 VPS, or a managed platform (Fly.io/Railway)? LiteLLM + FastAPI + Postgres fits on a $10–20/mo box.
4. **How much of the repo goes public** vs. stays private (Forge, personal agents)?
