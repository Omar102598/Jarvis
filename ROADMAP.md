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

## Month 1 — Foundation  ✅ Done (completed 2026-07-04: Chroma restored, MCP live + validated)

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
- `mcp-server-fetch` (Python, via uvx) — clean web fetching
- GitHub hosted remote (`https://api.githubcopilot.com/mcp/`) — replace custom `github_tool.py`
- Playwright MCP — replace `mac_browser_*` tools

**Registry corrected 2026-07-02:** fetch entry now uses `uvx mcp-server-fetch`
(the npm package never existed); github entry now points at GitHub's hosted
remote server (npm one is archived) via new `headers:` support in
`mcp_loader.py`; llm_agent Dockerfile installs `uv` for uvx-based servers.

**MCP ACTIVATED 2026-07-03 — first servers live.** Two latent bugs had kept
this path dead since Month 1: (1) `mcp_loader` used the pre-0.1 adapters API
(`async with MultiServerMCPClient`) which the installed 0.3.0 removed;
(2) `./config` was never volume-mounted into llm_agent, so the registry file
was invisible. Loader rewritten for adapters 0.3 with a new `persistent: true`
mode (dedicated event-loop thread holds one session open — REQUIRED for
stateful servers like Playwright where the browser must keep page state across
tool calls; per-call sessions would launch a fresh browser every step).
- `[x]` **Playwright MCP live** (23 tools, persistent, headless Chromium in
        the container via `--browser chromium`; Dockerfile installs browsers
        with @playwright/mcp's own playwright-core so revisions match).
        `mac_browser_*` tools unbound from core — signed-in browsing stays on
        `mac_chrome_*`/mac_bridge; agents' direct `/browser` HTTP path untouched.
- `[x]` **Memory MCP live** (9 knowledge-graph tools, persists to
        `/data/mcp_memory.json`) — entity/relation memory complementing Chroma.

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
- `[x]` **Validated live 2026-07-04**: Forge (developer agent) edited the MCP
  registry (google_maps entry) and hot-reloaded the tool graph via the Redis
  reload queue — no rebuild, per-server failure isolation confirmed (missing
  API key skips that server; playwright/memory unaffected)

### 2c. Dedicated Developer Agent — "Forge"  ✅ Done (2026-07-02)
All self-modification and delegated coding projects now route through one
dedicated agent instead of the main brain editing code inline mid-conversation.
- `[x]` `services/agent_runner/developer_agent.py` — full Anthropic tool-use
        loop (read/write/find/grep/shell/rebuild via mac_bridge), Opus-tier
        (`DEV_LLM_MODEL`), on-demand only (zero idle cost), progress streamed
        to Redis `agent:developer:progress`, write-guard mirrors self_modify
        (no .env / compose / credential content)
- `[x]` Registered: `AGENT_CLASSES` + `DISPATCHABLE` + `AGENT_PERSONAS`
        ("Forge"), `config/agents.yml` entry, compose env for mac_bridge
- `[x]` `spawn_task` accepts `agent="developer"`; system prompt routes ALL
        code changes to Forge
- `[x]` Main agent demoted to read-only code tools (`jarvis_read/find/grep/
        list`, `dev_read/list/search`) — `jarvis_write_code`, `jarvis_rebuild`,
        `dev_write_file`, `dev_shell` unbound from core (functions kept for
        Forge-era rollback)
- `[x]` Agent completion announcements use persona names ("Sir, Forge has
        finished…")
- `[x]` First real task verified 2026-07-03: Forge pinned the chromadb client
        (`>=0.6.0,<0.7`) to match the 0.6.3 server, restoring Chroma semantic
        memory (was silently in Redis-only fallback since the 1.x client slipped in)

### 2d. Grocery usual-order learning  ✅ Done (2026-07-03)
- `[x]` `action=learn_cart`: Remy scans the user's own Amazon Fresh cart
        (visible browser), LLM-cleans titles, merges into Redis
        `grocery:usual_order` (frequency-counted across scans)
- `[x]` Weekly list generation biases toward the learned staples
        (strongest signal in the meal-planner prompt)
- `[x]` Tools: `learn_fresh_cart` ("learn my Fresh cart"), `get_usual_order`
- `[x]` Verified live against a real 28-item Fresh cart
- `[x]` `pin_favorite_product` tool (2026-07-03): pins the exact resolved
        product from the last order into `grocery:favorites` (always-wins
        cache); pin/unpin/list via conversation

## Month 2.5 — Platform Polish  ✅ Done (2026-07-03)

### Apps (iOS + macOS)
- `[x]` Auto-reconnecting push WebSocket (`JarvisSocket`, backoff 2s→60s +
        30s ping) in both apps — pushes previously died silently until relaunch
- `[x]` Chat history restore: new gateway `GET /history` (reads the same
        Redis conversation the brain keeps); both apps load it on launch
- `[x]` iOS tool-event polling gated: 2.5s while processing, 20s idle (battery)
- `[x]` Hands-free: `AskJarvisIntent` now `authenticationPolicy = .alwaysAllowed`
        (works from AirPods with phone locked) + new gateway
        `POST /ask/query/siri` fast path that skips server-side WAV synthesis
        (Siri speaks the dialog itself) — cuts seconds off, prevents timeouts.
        Old "Get contents of URL" voice-recording shortcut is obsolete — use
        "Hey Siri, Ask Jarvis" (App Intent).
- `[x]` Desktop global hotkey ⌥Space (Carbon RegisterEventHotKey, no
        Accessibility permission needed) summons the chat window
- Both apps verified: `xcodebuild` BUILD SUCCEEDED (2026-07-03)

### Siri ecosystem bridge (macOS)
- `[x]` `mac_run_shortcut` / `mac_list_shortcuts` tools + mac_bridge
        `/shortcut/run|list` — Jarvis runs any macOS Shortcut (HomeKit scenes,
        Reminders, Focus modes). This is the sanctioned way for Jarvis to
        leverage Siri's toolset; iOS offers no API to invoke Siri directly.
- `[x]` `read_imessages` tool + mac_bridge `/imessage/recent` (chat.db,
        read-only; best-effort attributedBody decode). ⚠ Requires Full Disk
        Access for `.venv-mac-bridge/bin/python3`, then restart mac_bridge.

### Dev observability
- `[x]` Dashboard **Logs page** (`/logs`, linked from header): live tail of
        every docker + native service, proxied via mac_bridge `/logs/tail`
        (docker logs / native log files), service pills, follow mode

### Workout Coach — "Apollo"  ✅ Done (2026-07-03)
- `[x]` `services/agent_runner/workout_agent.py`: programs the week around
        (1) the user's lift split (profile `workout_split`; default back/bi →
        chest/tri → legs/shoulders ×2 + weekend class), (2) Kai's ClassPass
        data (`classpass:booked` shapes the weekend; suggestions recommended
        when nothing booked), (3) HealthKit recovery (volume cuts on poor
        sleep / elevated RHR). Plan → Redis `workout:plan`.
- `[x]` Scheduled Sunday 6 PM CDT (plan ready for Monday) + on-demand
- `[x]` Tools: `get_todays_workout`, `get_workout_plan`, `plan_workout_week`

### Grocery: multi-store cart learning + kitchen-grounded meals (2026-07-03)
- `[x]` `learn_cart` expanded beyond Amazon Fresh: `stores=amazon|whole_foods|
        target|heb|all`. Amazon/WF use the precise DOM scanner; Target/HEB use
        page-text + LLM extraction (their cart DOMs churn; text is resilient).
        Items carry a `store` tag in `grocery:usual_order`.
- `[x]` `suggest_meals` now grounds dinner ideas in BOTH this week's order and
        the learned usual staples ("what can I make for dinner?" works even
        before the first weekly run)

### Everyday-AGI tools (2026-07-03, round 2)
- `[x]` `apple_reminder_add` / `apple_reminders_list` — Apple Reminders via
        AppleScript (syncs to iPhone/Watch); verified against the real list
- `[x]` Brad: `detect_subscriptions` — recurring-charge finder over BankSync
        transactions (cadence + price-increase detection, annualized totals);
        verified live (found 4 real subs, ≈$2.3k/yr)
- `[x]` `AskJarvisIntent` returns its answer as a value (`ReturnsValue<String>`)
        so Shortcuts can chain it — enables the "Hey Jarvis" Vocal Shortcut
        recipe: Dictate Text → Ask Jarvis → Speak Text
### Email Triage — "Hermes"  ✅ Built by Forge (2026-07-04) — awaiting creds
- `[x]` `services/agent_runner/email_agent.py` (stdlib imaplib/email — no new
        deps): triages last 24h into important/action-needed, package &
        tracking updates, newsletters/promos, everything-else count.
        7:30 AM CDT daily + on-demand. Built end-to-end by Forge.
- `[ ]` **Activate: add `IMAP_USER` + `IMAP_PASSWORD` (Gmail app password) to
        `.env`** (`IMAP_HOST` optional; compose passthrough already wired)

### Google Maps MCP  ✅ Registered by Forge (2026-07-04) — awaiting key
- `[x]` `google_maps` entry in `config/mcp_servers.yml` (directions, distance
        matrix, geocoding, places) — added + hot-reloaded live by Forge
- `[ ]` **Activate: add `GOOGLE_MAPS_API_KEY` to `.env`**, then restart
        llm_agent (compose passthrough already wired)

### ClassPass day-navigation fix (2026-07-04)
- `[x]` Studio-page searches for a non-today day ("Barry's on Sunday") landed
        on the wrong day: blind Next-day arrow clicks got swallowed mid-render
        (2nd click never took; Sunday → Saturday). Now `_advance_studio_day`
        tries the date tab first, then arrow-clicks ONE day at a time,
        confirming the selected date after every click before scraping.

Remaining candidates (not built): WhatsApp MCP, Google Calendar MCP,
package-tracking (rides on Hermes once email creds land)

### Self-service customization — Jarvis for ANY user  ✅ Done (2026-07-04)
- `[x]` `install_mcp_server` rewritten: APPENDS to the registry (old version
        yaml.dump'd the whole file, destroying every comment), validates before
        writing, supports `persistent`/`env_keys`; + new `list_mcp_servers`
- `[x]` **Conversational install VERIFIED end-to-end**: "install the sequential
        thinking MCP server" → registry appended (comments intact) → hot
        reload → tool live in seconds, no rebuild (121 tools)
- `[x]` `get_setup_status`: audits integrations (env presence only), profile
        completeness, and data feeds, with get-credentials hints — verified live
- `[x]` `personalize_jarvis`: conversational onboarding interview (one question
        at a time → update_user_profile)
- `[x]` fetch MCP enabled (uvx, no creds); `sequential_thinking` MCP installed
        (structured reasoning for complex planning)
- `[x]` System prompt: personalization section — Jarvis proactively offers
        setup to new users and knows its own extension paths
        (MCP install < hot-reload plugin < Forge)

### Surface & liveness fixes (2026-07-04)
- `[x]` Mac no longer speaks iPhone/Siri responses aloud — tts_mac subscribed
        `jarvis/tts/+/speak` (every room incl. `mobile-*`/`glasses-*`); now
        serves physical rooms only
- `[x]` Dashboard live updates fixed — SSE change detection compared list
        LENGTHS of windowed reads, which stop changing once the window fills
        (30 msgs / 20 tool events): the stream froze exactly when the dashboard
        got busy. Now content-signature based.
- `[x]` iPhone app: history re-syncs on every foregrounding (other surfaces'
        turns appear); local notifications for pushes arriving while
        backgrounded (permission requested at launch)
- `[ ]` **Real push notifications (APNs)** — local notifs only cover the brief
        window before iOS suspends the socket. Needs an APNs auth key (.p8)
        from the developer account; mobile_gateway gains an HTTP/2 sender.
        Good Forge project once the .p8 exists.
- `[x]` **Remote access via Tailscale** — Mac joined the tailnet 2026-07-04
        (`omars-macbook-pro.tail7e74a7.ts.net` / 100.110.206.125); gateway
        (8080) + dashboard (8888) verified reachable over it. iPhone: install
        Tailscale, sign in with the same account, set the Jarvis app Server URL
        to `http://omars-macbook-pro.tail7e74a7.ts.net:8080`.
        Month 3's VPS is the eventual replacement.

### Multi-surface behavior policy  ✅ Agreed (2026-07-04)
1. **Replies answer ONLY on the surface that asked** (phone → phone; room
   wake-word → that room's speaker). Enforced by the tts_mac room filter.
2. **Proactive alerts route by presence**: home → speak in the last-active
   room + silent phone notification; away → phone notification only.
3. **Room speakers (when purchased)**: announcements go to the nearest-active
   room (last wake-word/motion), never broadcast unless explicitly asked to
   announce house-wide.
- `[ ]` Presence-aware announcement routing in ambient/fanout — build when
   speakers arrive (signals already exist: jarvis:active_surfaces, heartbeats,
   per-room voice state). Queued as a Forge project after APNs.

### Vision overhaul — Claude everywhere + video  ✅ Done (2026-07-05)
- `[x]` **Fixed: glasses/app photos were invisible to the model** — the
        `[GLASSES_CAMERA_IMAGE: data:...]` marker was never converted to an
        image block on the live turn, so Claude received base64 AS TEXT. The
        brain now converts markers (up to 8) into real multimodal content.
- `[x]` `get_camera_snapshot` (HA cameras) switched from hardcoded OpenAI
        gpt-4o to Claude via llm_factory (OPENAI_API_KEY no longer needed)
- `[x]` **NEW `/ask/video`**: short clip → ffmpeg samples 6 evenly spaced
        downscaled frames → one multimodal message ("frames in order from an
        N-second video"). VERIFIED live: Claude correctly described a test
        video. This is the backend for fridge/pantry scans on the glasses and
        an app record button (app-side capture UI = future Forge project).
- `[x]` **Video-native tier (2026-07-05)**: `/ask/video` prefers Gemini when
        `GOOGLE_API_KEY` (AI Studio — free tier) is set — true video
        understanding (motion, temporal order, audio track), description then
        routed through the normal Jarvis pipeline; frame-sampling+Claude
        remains the always-works fallback (re-verified). NOTE: this is a
        DIFFERENT key from GOOGLE_MAPS_API_KEY (AI Studio vs Maps Platform).
- `[x]` **Google Maps MCP ACTIVATED 2026-07-05** — key added, 7 real tools
        loaded, verified live through the brain (real driving directions,
        Domain → downtown Austin: 12.3mi/19min via MoPac)
- `[ ]` Gemini key: `API_KEY_SERVICE_BLOCKED` (403) fixed by adding the API to
        the key's restriction allowlist — but that project has Gemini API
        billing enabled and is on the "prepay" model with a $0 balance, so
        calls now fail 429 "prepayment credits depleted". OPEN — pick one:
        (a) create a fresh key via AI Studio "new project" (free tier, no
        prepay needed — recommended), or (b) add prepay credit at
        ai.studio/projects for this project. Reminder set (Apple Reminders).
        Not urgent — frame-sampling+Claude fallback fully verified working.
- Fixes that fell out of the same investigation (2026-07-05): TAVILY_API_KEY
  had a typo in .env (`ttvly-`→`tvly-`) — Ada/web_search were dead; fixed +
  Ada verified live. CODE_EXEC_ENABLED=true needed a container RECREATE (env
  changes don't apply on restart); run_python verified live.

### Ring cameras + Sentry  ✅ Built (2026-07-05) — awaiting one-time Ring login
- `[x]` `ring_mqtt` bridge service in docker-compose (community tsightler/
        ring-mqtt — the de-facto standard; no official Ring API exists),
        publishing to Jarvis's existing mosquitto
- `[x]` agent_runner subscribes ring/# — device registry, camera names (from
        HA discovery configs), snapshot caching, motion/ding → Sentry dispatch
        with per-camera cooldown (SENTRY_COOLDOWN_S, default 10 min)
- `[x]` **Sentry agent**: Claude vision (haiku) assesses each event snapshot;
        alerts ONLY when notable (person/package/vehicle/doorbell/Finley up to
        something) via iOS push + iMessage (iMessage retires once APNs lands)
- `[x]` Tools: `check_ring_camera` ("check on Finley"), `list_ring_cameras`
- `[x]` **Ring login done via web UI (:55123) 2026-07-05** — 2 cameras
        discovered with correct names (Front Door, Living Room); topic wiring
        verified against live traffic; snapshots caching.
        **E2E verified: "check the living room camera" → "Finley is chilling
        on the couch" — real camera, real dog, Claude vision.**
- `[x]` **Arrival greetings**: person arriving/doorbell → Sentry's vision
        verdict includes a spoken JARVIS-style announce line → played indoors
        (profile `sentry_greetings`, default on; room via
        `sentry_greeting_room`)
- `[x]` **Snapshot pushes**: Sentry alerts attach the camera frame via new
        gateway `GET /ring/snapshot/{device}.jpg` + `media_url` passthrough in
        surface pushes → renders as an image card in the app (and on the
        glasses HUD when they arrive)
- `[x]` **Privacy mode**: "give me some privacy" → `ring_privacy` tool →
        `sentry:privacy` TTL key; event handler drops ALL Ring data (no
        snapshots, no logging, no Sentry) until expiry; auto-resumes.
        Live-verified full on/status/off cycle.
- Watch item: a community **Ring MCP server** exists (lobehub.com/mcp/
  jpcors-ring-mcp, ~May 2026) — thin vs ring-mqtt today; revisit if it matures.
- `[x]` **Live view (2026-07-05)**: gateway relays ring-mqtt's RTSP to HLS via
        ffmpeg on demand (`POST /ring/live/{device}/start`, 5-min auto-stop);
        `ring_live_view` tool starts it + pushes a video card (m3u8 media_url
        → type "video" → app/glasses HUD play natively via AVPlayer).
        **VERIFIED: real HLS segments streaming from the Living Room camera.**
        Livestream creds set in bridge config; data/ring-mqtt/ gitignored
        (ring-state.json holds the refresh token — never commit).
- `[x]` **Package watch (2026-07-05)**: Sentry verdict includes
        package_visible → stateful per-camera tracking (ring:package:{device},
        24h TTL): arrival alert (📦, with delivery-email cross-ref from
        Hermes's latest triage once IMAP creds land) and package-GONE alert
        if a later frame shows it missing (porch-pirate alarm).
- `[x]` **Arrival scenes (2026-07-05)**: evening person-arrival (profile
        `arrival_scene_shortcut`, window `arrival_scene_hours` default 18-07)
        → runs the named HomeKit Shortcut via mac_bridge.
- `[x]` `who_came_by` tool — digest of Sentry-assessed events over N hours.
- `[x]` **Fresh snapshots + send-to-app (2026-07-05)**: `check_ring_camera`
        auto-grabs a live RTSP frame when the cache is >2 min old and pushes
        the picture to the app/glasses as an image card by default.
        VERIFIED: Living Room fresh frame in ~4s. KNOWN LIMIT: the battery
        doorbell refuses to livestream (Ring-side: "stream unexpectedly
        inactive" — check battery level / Live View toggle in the Ring app);
        it still snapshots on motion/ding, which is when the front door
        matters.
- Awaiting real-world events to observe: greeting/package/scene paths fire on
  the next genuine person/package event (all code-deployed; vision verdict
  drives them).
- `[x]` **Sentry presence-aware + push feed (2026-07-13):** fixed the three
  reported failures. (1) App cards: pushes were WS-only (lost unless app
  foregrounded) — gateway now persists every push (surface:pushes, 50-deep)
  + `GET /pushes`; app merges the feed on launch/foreground (pushID dedupe);
  Sentry pins each assessed frame to `ring:snapshot:event:{id}` (48h) served
  by `GET /ring/snapshot/event/{id}.jpg` so old cards show the RIGHT moment
  (per-camera cache gets overwritten). (2) False "someone in living room"/
  "welcome home": Sentry now reads the geofence presence key — prompt gets
  HOME/AWAY + indoor/outdoor camera + pets context (resident home + indoor
  person = expected/not notable; away + indoor person = intruder alert;
  cats/dog never notable alone); indoor cameras NEVER announce arrivals;
  resident greeting only when presence≠home, sharing the geofence's
  30-min debounce key so the two paths can't double-greet; geofence 'left'
  no longer clears the debounce (GPS flap re-greeted). (3) Lamps on midday
  arrival: `_run_arrival_scene` now honors arrival_scene_hours (18-07
  default) and the greeting only claims lights when the scene ran.
  VERIFIED live: simulated Living Room motion while home → "Person seated
  on couch; resident at home" → not notable, no alert, no announce.
  Profile tunables: pets_description, indoor_cameras, arrival_scene_hours,
  arrival_lights, resident_description.
- `[~]` **Face recognition (2026-07-13):** dormant `services/vision`
  (InsightFace buffalo_l, built Month 1, never started) revived as the
  Ring face-ID sidecar: subscribes ring snapshot MQTT → identifies against
  enrolled embeddings (Redis face:{name}) → `ring:camera:{device}:face_id`;
  Sentry polls that key and injects a deterministic "Face recognition:
  omar (enrolled household member)" line into the vision prompt — TRUSTed
  over visual guessing. Honors privacy mode. Fixes applied to the old
  service: onnxruntime-gpu → CPU (no arm64 wheel — build was impossible),
  YOLO/ultralytics removed (legacy CAMERAS mode only, ~2GB torch), compose
  env names matched to code, profile gate removed, model cache persisted.
  Enrollment over MQTT (jarvis/vision/enroll + enroll_finalize).
  **Activate: run `scripts/enroll_face_ring.sh` while standing 3-6ft in
  front of the Living Room camera** (grabs fresh RTSP frames, averages
  embeddings; re-run any time to replace). Degrades gracefully — Sentry
  says "face recognition unavailable" if the service is down/unenrolled.
- `[x]` **Selfie enrollment (2026-07-13, same day):** the PRIMARY enrollment
  path — reusable for users with no home cameras, and phone selfies give
  better embeddings than security-cam frames anyway. iOS Settings → FACE
  RECOGNITION → name + PhotosPicker (3-5 selfies, resized to 1280px) →
  gateway `POST /face/enroll` {name, images[], finalize} → MQTT
  `jarvis/vision/enroll_image` → vision embeds each → finalize averages →
  face:{name}. Re-enroll replaces (gateway clears the sample buffer).
  E2E VERIFIED with a real portrait: 1/1 usable → enrolled:true.
  Ring scan remains the bonus path (can top up the same identity with
  camera-distribution frames). KNOWN LIMIT: enroll messages are QoS-0 —
  a vision MQTT reconnect can drop them; app shows FAILED, retry works.
- `[x]` **Arrival greeting timing (2026-07-13):** the 120m geofence fires
  while still walking up — lights stay on geofence (apartment ready on
  entry) but the SPOKEN greeting is now HELD (jarvis:arrival:pending,
  10-min TTL) until the first camera motion confirms entry, with a
  timer fallback (ARRIVAL_GREET_FALLBACK_S, default 150s) so camera-less
  arrivals still get greeted. Atomic delete = motion and timer can't
  both speak. 'left' clears the pending greeting.

### PetKit + Home Assistant  🔜 Next up (decided 2026-07-05)
- `[x]` `tools/petkit.py` built (Forge): feed_pet, feeder/fountain status,
        schedules — drives HA entities via HA_URL/HA_TOKEN
- `[ ]` Add `homeassistant` service to docker-compose (./data/homeassistant)
- `[ ]` User: HA onboarding + long-lived token → .env; install HACS + PetKit
        integration (PetKit account creds live in HA, not Jarvis)
- `[ ]` **BUY: PetKit cat feeder, PetKit dog feeder, PetKit water fountain**
        (chosen over Petlibro's fragile reverse-engineered API — Ada's
        briefing in agent:research:reports)
- Bonus: HA also activates the dormant smart_home tools + camera snapshot tool

### Models & cost (2026-07-05)
- `[x]` Sonnet tier → **claude-sonnet-5** (verified live)
- `[x]` Credit-exhaustion alert: any agent failure mentioning credits/billing
        → iMessage warning (deduped 1h)
- `[ ]` Proactive balance monitor (warn BEFORE failure) — needs Anthropic
        Admin API usage/cost endpoints (separate admin key from console)

### Travel Agent  🔜 Planned (requested 2026-07-07)
- Flights / hotels / Airbnb / VRBO search + booking assistant. Likely shape:
  new persona agent using Playwright MCP (fresh sessions) or mac_chrome_*
  (signed-in bookings), Google Maps for logistics, price tracking via the
  existing price_monitor pattern, approval-gated bookings like grocery.
  Design session with Omar before building.

### Food delivery MCPs  ✅ Built (2026-07-12) — awaiting one-time OAuth
- `[x]` `uber_eats` + `doordash` in mcp_servers.yml — OFFICIAL hosted servers
        (mcp.ubereats.com / openapi.doordash.com), bridged via `npx mcp-remote`
        with token cache at /data/mcp_auth (headless OAuth reuse)
- `[ ]` **Activate: run `scripts/authorize_food_mcps.sh` on the Mac** (browser
        sign-in to both accounts), then `docker compose restart llm_agent`
- Ordering is approval-gated (tool descriptions require user confirmation)

### Integration candidates — researched 2026-07-12
Cross-checked against everything above; ordered by value ÷ friction.
The mcp-remote OAuth bridge (food MCPs) is the reusable pattern for any
hosted OAuth server; mcp_loader's `headers:` support covers token servers.

**Tier 1 — ✅ ALL IMPLEMENTED 2026-07-12** (plus GitHub MCP enabled):
- `[x]` **Home Assistant official MCP Server** — `mcp_server` integration
        enabled in HA via its config-flow API (Assist API exposed); registry
        entry `home_assistant` (sse + HA_TOKEN header). Live same-session.
- `[~]` **Google Workspace official hosted MCPs** — `gmail` (scope
        gmail.modify; sending stays on SMTP) + `google_calendar` (full write)
        registered via the mcp-remote bridge; Drive/People noted, off.
        **Activate: create a Desktop-app OAuth client in GCP (enable Gmail/
        Calendar APIs + gmailmcp/calendarmcp MCP services), then run
        `scripts/authorize_google_mcps.sh <client_id> <client_secret>`**
- `[~]` **Package tracking** — `package_tracking` entry (mcp-server-17track,
        2900+ carriers). **Activate: API_TOKEN_17TRACK in .env** (17track.net
        → Settings → API key; free 100/mo). Compose passthrough wired.
- `[~]` **Restaurant reservations** — `resy` entry (resy-mcp npm: search +
        real booking, email/password auth, approval-gated).
        **Activate: RESY_EMAIL + RESY_PASSWORD in .env.** `opentable` entry
        registered but OFF (needs the fetchproxy browser extension on the
        Mac; booking is link-only on OpenTable anyway). NOTE: both automate
        your own account against anti-bot ToS — personal use only, same
        category as the ClassPass/Fresh automation.
- `[~]` **GitHub hosted MCP** — flipped `enabled: true`.
        **Activate: GITHUB_TOKEN (PAT) in .env** — not present today. Retire
        custom github_tool.py once proven.

**Tier 2 — valuable, needs a decision or has friction:**
- `[ ]` **Instacart official MCP** (docs.instacart.com developer platform) —
        recipe pages + shopping-list fulfillment. Candidate SECOND grocery
        backend for Remy (Amazon Fresh separate-cart bug still open) —
        decide whether Remy should dual-source.
- `[ ]` **WhatsApp MCP** (long-standing candidate) — no official server;
        self-hosted bridge required (OpenBSP has first-class MCP; or
        lharries/whatsapp-mcp via whatsmeow). Worth it only if WhatsApp is
        a daily channel — otherwise iMessage coverage is enough.
- `[x]` **GitHub hosted MCP** — enabled 2026-07-12 (see Tier 1 above);
        awaiting GITHUB_TOKEN in .env.
- `[ ]` **Events/tickets** — no credible Ticketmaster/StubHub MCP yet;
        Playwright/browser path in the meantime. WATCH.
- `[ ]` **Browser MCPs (assessed 2026-07-12)** — reality check: MCP tools
        load into the BRAIN only; Scout/Remy/Kai scrape via mac_bridge
        /browser/* (Mac-side Playwright, stealth patches, saved sessions)
        and Ada is Tavily-only, so no browser MCP touches them directly.
        The one real gap was the BRAIN's signed-in browsing (container
        playwright = fresh sessions) — CLOSED 2026-07-12: **mcp-chrome
        wired** (`chrome` registry entry → host.docker.internal:12306/mcp;
        mcp-chrome-bridge installed + native host registered).
        **Activate: load data/mcp-chrome-extension/ via chrome://extensions
        (Developer mode → Load unpacked) → extension icon → Connect.**
        Skips gracefully until then. **ego-lite** (citrolabs) =
        parallel human+agent browser that migrates Chrome logins — would fix
        Remy hogging the visible browser, but young (v1.2, ~500 stars),
        skill/JS interface not MCP, and it would hold ALL logins. WATCH.
        chrome-devtools-mcp (official) = dev/debug tool for Forge, not a
        scraping upgrade (automation-flagged Chrome, sign-ins get blocked).
- `[ ]` **Uber rides MCP** — only thin community wrappers today; official
        Uber Eats MCP suggests a rides server may follow. WATCH — slots
        into the Travel Agent (concierge) persona when real.

**Assessed and skipped (already covered):** Spotify MCP (custom tools),
Plaid (BankSync plugin), Notion/Todoist (Apple Reminders + notes tools —
revisit only if Omar adopts those apps), wearable MCPs Oura/Whoop
(HealthKit pipeline covers; sleep gap is a device-side issue, not data
access), flights/hotels (Kiwi + Jinko + Airbnb + Expedia already in).

### Glasses vision → kitchen inventory  🔒 Planned (needs Meta glasses purchase)
- `[ ]` "Look at my fridge/pantry" — glasses photo(s) → existing `/ask/image`
        path → vision model extracts inventory → merge into a
        `grocery:kitchen_inventory` Redis key
- `[ ]` `suggest_meals` grounds on actual fridge contents; missing ingredients
        for a chosen meal get added to the cart via the existing
        `mac_fresh_add` / resolver path
- Groundwork already in place: image pipeline (`/ask/image`), usual-order
  store, resolver + cart-add machinery. Build when the glasses arrive.

---

## Next Direction build-out (2026-07-17) — see docs/BUILD_TRACKER.md

Decisions from the 2026-07-16 analysis session (agreed with Omar): Bet 2
(Proactivity 2.0) + Bet 3 (unified memory) now; glasses bet deferred until
purchase; dedicated session still owed for voice-latency + tool allowlists.

- `[x]` **Approval Inbox** — one queue for every agent action needing a yes/no.
        Redis `jarvis:approvals:pending`; single executor in agent_runner
        (`jarvis/approvals/resolve` MQTT); surfaces: dashboard card with
        Approve/Deny buttons, gateway `GET /approvals` + `POST resolve` (iOS
        UI queued), brain tool `manage_approvals` ("approve that").
- `[x]` **Echo — habit miner** (`habits`, nightly 04:45 UTC): mines 14 days of
        jarvis:events into rhythm histograms → ≤2 suggestions/night filed as
        approvals; 90-day dedupe; approved routines → habits:routines:approved
        (auto-wiring into ambient triggers = follow-up Forge task).
- `[x]` **Chief-of-staff weekly review** — Chronicle's Sunday review now also
        reads GTD tasks, Apollo's plan, pending approvals, Echo's findings;
        delivers up to 3 grounded recommendations.
- `[x]` **Memory reflection** — Chronicle nightly extracts durable facts
        (high bar, [] is normal) → `memory:reflect:queue` → brain drain thread
        stores via `store_fact()` with Chroma near-dup check (<0.15 skipped).
        Episodic days now compound into semantic memory.
- `[x]` **Brain-side cost metering** (`usage_meter.py` in `_call_model`) — the
        cost widget finally includes the biggest spender; daily budget alert
        at DAILY_LLM_BUDGET_USD (default $15), mirrored in agent_runner.
- `[x]` **Vega — QA regression agent** (`qa`, nightly 09:00 UTC): golden
        conversations from config/qa_tasks.yml through the LIVE brain; alerts
        urgent ONLY on pass→fail vs the previous run.
- `[x]` **Sensitive-tool audit trail** — money/comms/shell/purchase tool calls
        mirrored to `jarvis/audit/tool` → synapse's durable stream (domain
        "audit"): replayable "what did Jarvis send/spend/execute".
- `[~]` **APNs server side** (`mobile_gateway/apns.py`, `POST /apns/register`,
        hooked into every surface push) — inert until the Apple Developer
        Program .p8 lands (Omar joins after the Schwab sign-on bonus). iOS
        registration Swift + approvals UI = next session.
- `[~]` **Month 3 prep** — `docker-compose.core.yml` (VPS) +
        `docker-compose.edge.yml` (Mac) + `docs/HETZNER_SETUP.md` runbook
        written; Omar is buying the Hetzner box. host.docker.internal env
        sweep started (agent_runner/main.py fixed; rest listed in runbook §6).

Round 2 (2026-07-18):
- `[x]` **Forge verification gate** — every CLI-mode Forge run on the Jarvis
        repo ends with an enforced golden_tasks run + diffstat in the report;
        red harness → urgent notification. (Forge already ran on headless
        Claude Code CLI — the gate was the missing piece, not an SDK rebuild.)
- `[x]` **Atlas — unified health** (`atlas`, Monday 8:30 AM + on-demand):
        deterministic week-over-week trends across HealthKit + Apollo + Sage +
        readiness → one synthesis + one recommendation. Brain tool
        `get_health_overview`; dashboard Health widget (stat tiles).
        Follow-up candidate: bloodwork-PDF ingestion.
- `[x]` **Memory hierarchy complete** — reflection drain also runs weekly
        `consolidate_memory` (exact-dup merge across the whole store).
- `[ ]` **Travel agent ("Miles")** — design proposal at
        `docs/TRAVEL_AGENT_DESIGN.md` with 4 decision points for Omar;
        build is one Forge-sized session after he reacts.
- Dedicated-session queue (needs Omar present): voice-latency tier
  (local Moonshine STT / Kokoro TTS, barge-in), per-agent tool allowlists.

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
| Developer agent (Forge) | `services/agent_runner/developer_agent.py` |
| Agent schedule | `config/agents.yml` |
| Mac bridge (host API) | `services/mac_bridge/main.py` |
| iOS app | `JarvisApp/Sources/` |
| Model selection | `services/llm_agent/llm_factory.py` |
| Memory (Chroma+Redis) | `services/llm_agent/tools/memory.py` |
