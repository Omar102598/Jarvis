# Next-Direction Build Tracker

Living log of the 2026-07-17 build-out (from the "fresh angle" analysis session).
Each workstream lists what was built, where it lives, and its test status.
Update this file whenever a workstream advances.

Status key: ✅ built+verified · 🔨 built, needs live test · ⏸ prepped, blocked on
external step · 📋 designed only

---

## 1. Approval Inbox  — 🔨

One queue for every action an agent wants a human yes/no on. Approving becomes
one tap, so agents can safely propose more.

**Design (bus-centric, single executor):**
- Pending approvals live in Redis hash `jarvis:approvals:pending` (id → json
  `{id, source, title, text, action, media_url, created, expires}`).
- ANY surface requests a resolution by publishing MQTT
  `jarvis/approvals/resolve` `{id, decision, by}`. The **agent_runner** is the
  single executor: it applies the decision, runs the approved `action`
  (`{"type":"mqtt"|"redis_lpush"|"redis_set", ...}`), sets
  `jarvis:approvals:status:{id}` (24 h TTL), appends to `jarvis:approvals:log`,
  and publishes `jarvis/approvals/resolved` (synapse persists it — audit trail
  for free).
- Requesting agents call `approvals.request_approval(...)` (fires an urgent
  notification via the notify router) and optionally
  `await approvals.wait_for_approval(id)`.

**Files:**
- `services/agent_runner/approvals.py` — request/wait/resolve + action executor
- `services/agent_runner/main.py` — subscribes `jarvis/approvals/resolve`
- `services/mobile_gateway/main.py` — `GET /approvals`, `POST /approvals/{id}/resolve`
- `services/llm_agent/tools/approvals.py` — `manage_approvals` brain tool
  ("approve that", "what's pending?")
- `services/dashboard/main.py` + `templates/index.html` — Approvals card with
  Approve/Deny buttons
- iOS app — pending (next session): ApprovalsView + client calls

**Test status:** backend flow verified via Redis/MQTT smoke test (see §12).
First real producer: Echo's suggestions (§2).

## 2. Habit miner — "Echo"  — 🔨

`services/agent_runner/habit_agent.py`, registered as `habits` in
`config/agents.yml` (nightly). Aggregates the last 14 days of the synapse event
stream (`jarvis:events`) into compact rhythm histograms + agent-report digests,
asks the LLM for at most 2 NEW suggestions (routine | nudge), dedupes against
`habits:suggested` (90-day memory), and files each as an **approval request**.
Approved routine suggestions land in `habits:routines:approved` (wiring a
routine into ambient triggers stays a follow-up / Forge task).

## 3. Chief-of-staff weekly review  — 🔨

Upgraded `ChronicleAgent._weekly_review` (same Sunday schedule): now also
grounds on open GTD tasks, Apollo's `workout:plan`, Echo's accepted/pending
suggestions, and subscriptions — and delivers a briefing with up to three
concrete recommendations instead of one.

## 4. Memory reflection (episodic → semantic)  — 🔨

Nightly Chronicle pass now extracts 0–5 **durable facts** per journaled day and
queues them on `memory:reflect:queue`. Brain side: `services/llm_agent/main.py`
runs a drain thread → `tools/memory.store_fact()` (refactored out of
`remember`) with a semantic near-duplicate check (Chroma distance < 0.15
skipped). Facts tagged `source: reflection`. Heavy deps stay in llm_agent, per
Chronicle's design note.

## 5. Cost telemetry: brain-side metering + budget alert  — 🔨

The dashboard cost widget only saw background-agent spend; the BRAIN (biggest
spender) was invisible. Added `services/llm_agent/usage_meter.py` (same Redis
schema as agent_runner `usage.py`, attributed per tier as `brain:haiku` etc.),
recorded in `_call_model`. Daily budget alert: when `usage:daily` total crosses
`DAILY_LLM_BUDGET_USD` (env, default 15), one urgent notification per day via
the notify router (both recorders check).

## 6. QA regression agent — "Vega"  — 🔨

`services/agent_runner/qa_agent.py` (`qa` in agents.yml, nightly 04:00 CDT).
Sends a small suite of golden conversations (config `config/qa_tasks.yml`,
builtin fallback) through the live brain via MQTT `jarvis/llm/request`, checks
expect/forbid substrings, diffs against the previous run (`qa:last`), and
alerts (urgent) ONLY on pass→fail regressions. Complements
`scripts/golden_tasks.py` (static) and `scripts/eval_conversations.py` (manual).

## 7. Security: sensitive-tool audit trail  — 🔨

`_push_tool_event` in the brain now mirrors calls to SENSITIVE tools
(comms/money/shell/code-exec/purchases) onto MQTT `jarvis/audit/tool` →
synapse persists them in the durable event stream (domain `audit`). Gives
"what did Jarvis send/spend/execute, when" replay. Per-agent tool allowlists
(MCP firewall) deferred to the dedicated architecture session.

## 8. APNs push (server side)  — ⏸ blocked on Apple Developer Program

`services/mobile_gateway/apns.py` — HTTP/2 token-auth sender (httpx + PyJWT),
`POST /apns/register` for device tokens (Redis set `apns:tokens`), hooked into
`_on_surface_push` so every proactive push also goes out via APNs when
configured. Entirely gated on `APNS_KEY_PATH`/`APNS_KEY_ID`/`APNS_TEAM_ID`
env — silent no-op today. **When Omar joins the dev program:** create an APNs
auth key (.p8) → drop in `data/apns/` → set env in compose/.env → add
`registerForRemoteNotifications` + token POST in the iOS app → test.

## 9. VPS split (Hetzner)  — ⏸ prepped, blocked on box provisioning

- `docker-compose.core.yml` — cloud half (redis, mosquitto, chroma, synapse,
  llm_agent, agent_runner, mobile_gateway, dashboard, ring_mqtt, vision)
- `docker-compose.edge.yml` — Mac half (wake_word, stt, speaker_verify, tts,
  glasses_bridge, homeassistant) — native tts_mac/mac_bridge unchanged
- `docs/HETZNER_SETUP.md` — provision → Tailscale → env deltas → migration
  order → rollback. Known refactor called out: `host.docker.internal:7777`
  mac_bridge references must become `MAC_BRIDGE_URL` env (started in
  agent_runner/main.py; full sweep listed in the runbook).

## 10. iOS app work  — 📋 next session

Approvals view + client methods; APNs registration Swift; Live Activities +
WidgetKit (needs dev program for push-updated activities; static widgets fine).

## 10b. Round 2 (2026-07-18) — 🔨

- **Forge verification gate** (`developer_agent.py`): every CLI-mode run that
  touches the Jarvis repo now ends with an ENFORCED `golden_tasks.py` run +
  `git diff --stat` in the report; a red harness fires an urgent notification.
  Note: Forge already ran on headless Claude Code CLI (Pro-plan billing,
  acceptEdits, tool allowlist) — the "rebuild on Agent SDK" idea was largely
  already true; this closes the verify/report gap.
- **Atlas — unified health** (`atlas_agent.py`, `atlas` in agents.yml, Monday
  8:30 AM + on-demand): deterministic week-over-week trends (sleep, steps,
  HRV, RHR, training days, Sage nutrition adherence, readiness) + LLM
  synthesis with ONE recommendation. Outputs `health:atlas:summary` +
  `widget:health:data`. Brain tool `get_health_overview` (auto re-triggers
  when stale). New dashboard **Health widget** (stat tiles, week-over-week
  deltas; widgets dir is volume-mounted so no dashboard rebuild).
  Bloodwork-PDF ingestion = future follow-up.
- **Memory hierarchy completion**: reflection drain now also runs
  `consolidate_memory` weekly (nx-keyed) so user-added duplicates get merged,
  not just reflection's own near-dups.
- **Travel agent "Miles" v1 — BUILT** (Omar approved assisted booking
  2026-07-18): brain `propose_booking` → Approval-Inbox card with itinerary +
  links; runner `travel_agent.py` `confirm` action sends the booking-links
  card on approval (user pays in own browser), daily `watch` cron re-prices
  proposed trips by querying the brain over the bus (it holds the search
  MCPs), alerting on drops ≥ 5%. Planning itself is conversational ("plan a
  trip to …" — brain uses Kiwi/Jinko/Airbnb MCPs + plan_trip).
- **Firecrawl-first confirmed** (Omar agreed): key is set, Scout already
  tries Firecrawl before the local scraper for URL watches, brain scrape_page
  is Firecrawl. Signed-in flows remain the only mac-browser users — and stay
  on the Mac post-VPS (residential IP; see the browser-layers table in the
  2026-07-18 conversation / HETZNER runbook).
- Still owed to the dedicated session: voice-latency tier (local
  Moonshine/Kokoro, barge-in — needs Omar present for mic/speaker testing)
  and per-agent/MCP tool allowlists.


## 10c. Native web search migration (2026-08-04) — ✅ verified live

Replaced the one-shot Tavily pattern with Anthropic's **server-side web search**
(`web_search_20260209`) for the research agents. The model now searches
iteratively and dynamic filtering keeps irrelevant results out of the context
window, instead of pasting a fixed slice of snippets into the prompt.

- `llm_helper.complete_with_search()` — native search with a per-call
  `max_uses` cap (searches bill separately, ~$10/1,000), automatic tool-version
  selection (dynamic-filtering variant needs Opus 4.6+/Sonnet 4.6+; haiku gets
  the basic one), `pause_turn` continuation handling, and search-count cost
  recording. Returns `""` on failure so every caller can fall back.
- **Ada (research)** — ✅ native, Tavily kept as fallback. Verified live: real
  recent sources with inline `[domain]` citations.
- **Jeeves (task)** — ✅ native. Also retires his separate SEARCH/REASON triage
  call, since the model now decides whether to search in the same turn.
- **Walter (newsletter)** — ✅ built and verified, but **native is OPT-IN**
  (`NEWSLETTER_NATIVE_SEARCH=true`). Measured: one native digest cost **$0.57**
  (148k input tokens across the search loop + 6 searches) vs ~$0.01 on Tavily.
  Daily × 30 = ~$17/mo against a ~$3-4/day baseline, for a news summary that
  doesn't need iterative search. Default stays Tavily.
- Env: `SEARCH_LLM_MODEL` (default `claude-sonnet-5`), `SEARCH_MAX_USES` (5),
  `RESEARCH_MAX_SEARCHES` (8), `NEWSLETTER_NATIVE_SEARCH` (false).
- Firecrawl and Tavily both stay: `web_fetch` only retrieves URLs already in
  the conversation, so it is not a general scraper, and ClassPass studio
  lookups still use Tavily.

### 🐛 Bug found and fixed during this work

`services/agent_runner/usage.py` was missing `import os` — introduced with the
daily-budget change on 2026-07-17 and swallowed by `record()`'s bare `except`.
**Every agent-side cost record has been silently failing since then** (the
dashboard cost widget only showed brain spend, which is why brain looked like
~100% of usage). Fixed; agent attribution verified working again.

## 11. Explicitly deferred

- Glasses build (Bet 1) — until glasses purchased (SDK note: Meta Wearables
  Device Access Toolkit, May 2026).
- Concierge caller — voice demo first, then build.
- Paperwork agent — fold into Hermes once Gmail MCP creds land.
- Per-agent tool allowlists, voice-latency work — dedicated session.

## 12. Verification log (2026-07-17 deploy)

- `scripts/golden_tasks.py` — ALL GREEN (97 modules compile, configs parse,
  critical tools present, gateway health OK) after all edits.
- Rebuilt + recreated: agent_runner, dashboard, llm_agent, mobile_gateway
  (dashboard templates are volume-mounted but its main.py is baked — rebuild
  was required). `docker image prune -f` reclaimed 8.3 GB post-build.
- Brain graph: **211 tools (119 core — `manage_approvals` registered)**, 3 tiers.
- agent_runner: subscribed approvals topic; **habits (04:45 UTC) + qa
  (09:00 UTC) scheduled**.
- **Approval Inbox E2E VERIFIED live**: injected approval → dashboard
  `GET /api/approvals` listed it → `POST resolve` → agent_runner executor ran
  the `redis_set` action, wrote `status:approved`, cleared pending, appended
  the log entry (`resolved_by: dashboard`, outcome recorded) → synapse
  persisted the resolve/resolved events in `jarvis:events`. Gateway
  `/approvals` correctly 401s without the API key.
- **Reflection drain VERIFIED live**: pushed a fact onto
  `memory:reflect:queue` → llm_agent logged "reflection fact stored" (Chroma).
- Cost widget: `widget:cost:data` live (agent spend); brain attribution
  appears after the first real brain query post-deploy.
- Core/edge compose files: `docker compose -f … config -q` both valid.
- **Vega QA first live run: 6/6 PASSED** (time, math, identity, weather tool,
  memory recall, lights state — through the full MQTT brain pipeline).
- **Reflection near-dup check VERIFIED**: re-pushing the same fact logged
  "skipped (near-dup)".
- **Brain cost metering VERIFIED**: after Vega's queries the cost widget shows
  `brain $0.84` vs agents' $0.01 — the brain was ~98% of spend and invisible
  until today.
- Echo: first real run happens tonight 04:45 UTC (needs nothing manual).
