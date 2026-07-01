# Jarvis Roadmap

High-level architecture plan agreed on 2026-06-29. Reference this file at the start of any
implementation session to stay aligned on priorities and completed work.

---

## Architecture Vision

**Core principle:** Jarvis is the router across all surfaces. Every input (Mac wake-word, iPhone
tap, glasses camera, Siri intent) funnels through the same LangGraph agent via MQTT. The
`ACTIVE_ROOM` context routes responses back to the right surface.

**End-state topology:**
```
[Cloud VPS — always on]          [Edge devices — local I/O only]
  llm_agent                  ←→    mac_bridge  (Mac)
  agent_runner                      tts_mac     (Mac)
  redis                             wake_word   (Mac/Pi)
  mosquitto / NATS                  stt         (Mac/Pi)
  chroma                            glasses_bridge
  dashboard
```

Mac currently runs everything. Lift core services to VPS first, then keep
only I/O-sensitive services (TTS, wake word, STT, bridges) native per-device.

---

## Status Key
- `[ ]` not started
- `[~]` in progress
- `[x]` complete

---

## Month 1 — Foundation  ✅ In Progress

### 1a. Chroma Vector Memory
- `[x]` Add `chroma` service to `docker-compose.yml`
- `[x]` Rewrite `services/llm_agent/tools/memory.py` — Chroma backend for semantic
        recall, Redis kept for exact key-value backup and `forget` resolution
- `[x]` Add `chromadb` to `services/llm_agent/requirements.txt`
- `[x]` Graceful fallback to Redis-only if Chroma is unreachable

### 1b. MCP (Model Context Protocol) Infrastructure
- `[x]` Add `langchain-mcp-adapters` to requirements
- `[x]` Add Node.js to `services/llm_agent/Dockerfile` (for `npx`-based stdio servers)
- `[x]` Create `config/mcp_servers.yml` — declarative MCP server registry
- `[x]` Create `services/llm_agent/mcp_loader.py` — per-call proxy tool factory
- `[x]` Wire MCP tools into `main.py` `_core_tools` at startup
- `[x]` Fix `plugin_registry.py` `_load_mcp_tools` lifecycle bug (tools invalid after context exits)
        — now delegates to `mcp_loader.load_tools_for_server` (per-call proxy tools); single MCP path

**MCP servers to activate (edit `config/mcp_servers.yml`):**
- `@modelcontextprotocol/server-filesystem` — structured file ops
- `@modelcontextprotocol/server-fetch` — clean web fetching
- `@modelcontextprotocol/server-github` — replace custom `github_tool.py`
- Playwright MCP — replace `mac_browser_*` tools

---

## Month 2 — Autonomy

### 2a. Proactive Ambient Agent
- `[x]` Create `services/agent_runner/ambient_agent.py`
  - Calendar: announces events starting in ≤30 min
  - Weather alerts: temperature/rain changes
  - User-defined Redis triggers (key `jarvis:ambient:triggers`)
  - Morning briefing hook (8 AM)
  - Cooldown: won't re-announce same trigger within `cooldown_mins`
- `[x]` Register in `config/agents.yml` (cron `*/5 * * * *`, enabled: true)
- `[x]` Register in `services/agent_runner/main.py` `AGENT_CLASSES`

### 2b. Dynamic MCP Install + Plugin Self-Test
- `[x]` Add `install_mcp_server` to `services/llm_agent/tools/plugins.py`
  - Writes to `config/mcp_servers.yml` via mac_bridge
  - Pushes `__mcp_reload__` to Redis queue → mcp_loader reloads
- `[x]` Add `test_tool` to `services/llm_agent/tools/plugins.py`
  - Runs a plugin's tool in a sandboxed call and returns output/errors
  - Enables Jarvis to self-debug new plugins without a full rebuild

---

## Month 3 — Infrastructure  ⏳ Remaining (does not block Month 4, which is done)

### 3a. Cloud Hosting
- `[ ]` Provision VPS (Hetzner CPX31 recommended, ~$18/mo)
- `[ ]` Move `llm_agent`, `agent_runner`, `redis`, `mosquitto`, `chroma`, `dashboard`
        to VPS Docker stack
- `[ ]` Mac bridge → outbound WebSocket client to VPS (not an inbound server)
- `[ ]` Update `docker-compose.yml` to split into `docker-compose.core.yml` (VPS)
        and `docker-compose.edge.yml` (per-device local services)
- `[ ]` Set up Caddy/Nginx on VPS for HTTPS on mobile_gateway + dashboard

### 3b. Local LLM for Private Queries
- `[ ]` Add Ollama service to docker-compose
- `[ ]` Add `ollama` branch to `llm_factory.py`
- `[ ]` Add "private" routing tier in `_classify_tier()` for health/finance/location queries
- `[ ]` Recommended models: Qwen2.5 72B (tool use), DeepSeek-V3 (reasoning)

> ⚠️ **Reconsider before building.** iOS 27 (see Month 6) ships an on-device
> Foundation Models LLM that can handle the private tier entirely on the iPhone —
> free, offline, no VPS/Ollama. If we wait for iOS 27's public release, the
> client-side on-device path likely replaces this server-side Ollama tier. Keep
> this as the fallback only if we need a server-side private tier before iOS 27
> is out, or for private queries that require the full Python tool ecosystem.

---

## Month 4 — Platform Integrations  ✅ Done (2026-06-29, ahead of Month 3)

> Implemented before Month 3 because none of it depends on cloud hosting or a
> local LLM — it builds only on the existing iOS app, mobile_gateway, grocery
> agent, and the Month 2 ambient agent.

### 4a. HealthKit → Jarvis Pipeline
- `[x]` Add `HealthKitManager.swift` — reads steps, active energy, sleep, HRV,
        resting HR, body mass, workouts; read-only auth
- `[x]` `POST /health/snapshot` in mobile_gateway → Redis `user:health:latest`
        + `user:health:history` (60-deep); auto-syncs profile weight
- `[x]` Grocery agent merges HealthKit into profile; TDEE uses measured active
        energy (BMR + avg active kcal) when available instead of static multiplier
- `[x]` Ambient agent `_check_health_anomalies` — sleep drop (>1.5h below 7-day
        avg) and resting-HR spike (>8bpm above avg)
- `[x]` iOS pushes snapshot on launch + every foreground (`scenePhase`)

### 4b. Surface Presence — Proactive Fanout
- `[x]` `_fanout_to_active_surfaces` refactored with `title` support
- `[x]` llm_agent subscribes `jarvis/agents/+/report` → `on_agent_report` fans
        completed agent reports out to active iPhone/glasses surfaces
        (blocklist for noisy agents; ambient excluded to avoid double-push)
- `[x]` Ambient agent fans its own direct alerts to surfaces + titled HUD push
- `[x]` mobile_gateway `_on_surface_push` honors optional `title`

### 4c. Siri Consolidation + Calendar
- `[x]` `AskJarvisIntent` made explicit background-only (`openAppWhenRun = false`)
        — every query routes through the Jarvis engine, never Siri-native
- `[x]` `CalendarManager.swift` (EventKit) → `POST /calendar/next-event` →
        Redis `jarvis:calendar:next_event` (closes the loop: this is the exact key
        the ambient agent's calendar trigger reads — previously had no data source)
- `[x]` Info.plist: Health, Calendar (+ full access), Contacts usage strings
- `[x]` Entitlements: `com.apple.developer.healthkit`

---

## Month 6 — iOS 27 Apple Intelligence  🔒 Blocked on OS release

> **Status: research only — do NOT build yet.** iOS 27 is announced (WWDC June 2026)
> but not publicly released. APIs are beta and may change; devices in the field are
> still on iOS 26. Revisit once iOS 27 ships in the fall and enough of the user base
> has upgraded. Research captured 2026-06-30.
>
> **What iOS 27 exposes (three distinct layers):**
> - **Foundation Models framework** — high-level Swift API over Apple's sealed
>   on-device LLM (AFM 3 Core 3B, Core Advanced 20B on Pro). Tool calling,
>   structured output (`@Generable`), image input, streaming, Private Cloud
>   Compute fallback. **iOS-only Swift — the Python backend cannot call it.**
> - **Core AI** — bring-your-own-model on-device inference (`.aiasset`, explicit
>   CPU/GPU/Neural Engine control). Path for a future custom persona model.
> - **MLX** — build-pipeline framework to convert open-weight models → `.aiasset`.

### 6a. On-device private tier (replaces Month 3b)
- `[ ]` `FoundationModelsManager.swift` — wraps `LanguageModelSession` with native
        Swift tools (HealthKit, Calendar, Contacts)
- `[ ]` Client-side query classifier in the iOS app: private/fast/offline →
        on-device AFM; complex/full-tool → existing Claude backend
- `[ ]` Graceful fallback when Apple Intelligence unavailable (older device / iOS 26)
- **Why it wins over 3b:** free, offline, nothing leaves the device, no VPS/Ollama.
- **Limit:** 3B model — good for summarization/classification/structured extraction
        over local health+calendar data; NOT a Claude replacement, no Python tools.

### 6b. On-device Vision pre-filter for glasses
- `[ ]` Insert Vision (OCR, barcode, object detection) before `/ask/image`
- `[ ]` "Read this sign/menu/label" and barcode/QR handled locally; only escalate
        reasoning-heavy frames to the cloud llm_agent
- **Why:** cuts latency, API cost, and image exposure on the glasses camera path.

### 6c. Siri AI intent enrichment
- `[ ]` Adopt iOS 27 App Intents onscreen-awareness + personal-context params in
        `AskJarvisIntent` so "ask Jarvis about *this*" passes screen context

### 6d. Cost note
- Small Business Program (<2M downloads — we qualify) gets **free** Private Cloud
  Compute access to Apple's cloud Foundation Models. Zero marginal cost even for
  the on-device model's cloud-escalation tier.

---

## LLM Strategy

**Recommended: Hybrid (API + local), defer fine-tuning**

| Tier | Model | When |
|------|-------|------|
| Fast | `claude-haiku-4-5` | Simple commands, device control |
| Default | `claude-sonnet-4-6` | Conversational, tool use |
| Powerful | `claude-opus-4-8` | Complex reasoning, code |
| Private | Local Ollama (VPS) *or* on-device AFM 3 (iOS 27, preferred once released) | Health, finance, location data |

**Fine-tuning targets (Month 5+, only after 6mo of real query history):**
1. **Persona LoRA** on 7B model for voice response style consistency (~2k examples)
2. **Tier classifier** — replace keyword heuristics in `_classify_tier()` with a
   fine-tuned 1B classifier trained on real query patterns

**Open-weight models to watch:**
- Qwen2.5 72B — best open-source tool use
- DeepSeek-V3/R1 — best open-source reasoning, MIT license
- Llama 3.3 70B — general purpose, well-supported

---

## Native AI Orchestration Strategy

Siri/Meta AI/Google will always have deeper OS access. Strategy: **delegation, not competition**.

| AI | Role |
|----|------|
| Siri | Voice-in on iPhone, HealthKit/Contacts/Calendar access, CarPlay |
| Google Home | Relay queries to Jarvis via Google Home Action (if household uses GH) |
| Meta AI | Glasses: camera + display as Jarvis sensors/output (not the brain) |
| **Jarvis** | Everything else + cross-platform memory + custom tools + autonomy |

---

## Key Files

| Purpose | Path |
|---------|------|
| Agent brain | `services/llm_agent/main.py` |
| Tool library | `services/llm_agent/tools/` |
| Hot-reload plugins | `services/llm_agent/plugins/` |
| MCP server registry | `config/mcp_servers.yml` |
| MCP loader | `services/llm_agent/mcp_loader.py` |
| Background agents | `services/agent_runner/` |
| Agent schedule | `config/agents.yml` |
| Mac bridge (host API) | `services/mac_bridge/main.py` |
| iOS app | `JarvisApp/Sources/` |
| Model selection | `services/llm_agent/llm_factory.py` |
| Memory (Chroma+Redis) | `services/llm_agent/tools/memory.py` |
