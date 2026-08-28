# Jarvis — Phases 0–4 Feature Guide

Everything built in the 2026-07-14 capability expansion, and how to use it.

Most features are **conversational** — talk to Jarvis naturally; the example
phrases below are illustrative, not magic strings. A few are **automatic**
(they just run) and a few need a one-time **setup** value.

> **Activating changes:** code changes go live after a rebuild, e.g.
> `docker compose build <service> && docker compose up -d <service>`.
> Profile settings take effect immediately (no rebuild) — set them by voice
> ("set my cash buffer to 500") or `update_user_profile(field, value)`.

---

## Quick reference

| Feature | Trigger | Type |
|---|---|---|
| Daily digest (batched alerts) | automatic | auto |
| LLM spend widget | dashboard (`:8888`) | auto |
| Cross-agent insights (Synapse) | automatic | auto |
| Daily journal | "what did I do last Tuesday?" | mixed |
| Spending overview | "how's my spending / any subscriptions?" | ask |
| Task list (GTD) | "add X to my list", "what's on my plate?" | ask |
| Readiness score | "how recovered am I?" | ask |
| Lighting scenes | "save this as movie night" / "set movie night" | ask |
| Departure automation | automatic (on leaving) | auto |
| Email reply drafts | "show my email drafts" | ask |
| Focus mode | "focus for 90 minutes" | ask |
| Nutrition (Sage) | show a meal photo → "log this" | ask |
| Visual memory | "remember my keys are on the shelf" / "where are my keys?" | ask |
| Quick capture | "note that…", "remind me to…" | ask |
| OCR / translate | show a photo → "what does this say?" | ask |
| Packages | "any deliveries come?" | ask |
| Webhooks | `POST /webhook/{source}` | integrate |
| Supplement reminders | set profile, then automatic | setup+auto |
| Watchdog | automatic (every 5 min) | auto |
| Local model tier | set `OLLAMA_BASE_URL` | setup |
| Weekly review | automatic (Sun 6pm) | auto |
| Protein-gap nudge | automatic (Sage-driven) | auto |
| Undo / audit | "undo that" / "what did you just do?" | ask |
| Web scrape+extract | "scrape/read this page: …" (scrape_page) | ask |
| People / relationships | "remember Sarah is my sister, birthday 03-14" | ask |
| Routines | "what are my routines?" | ask |
| Trip planning (Miles) | "plan a trip to Lisbon in October" | ask |
| Memory cleanup | "consolidate your memory" | ask |
| Outbound call | "call +1… and say …" (needs Twilio) | ask |
| Dynamic web watch | "watch this URL for concert tickets" (manage_watches) | ask |

---

## Phase 0 — Foundation (mostly invisible plumbing)

### Durable event log
Every meaningful bus event is mirrored into a replayable Redis Stream
(`jarvis:events`). Nothing to do — it's the substrate Chronicle and "what did I
miss" read from. Inspect: `docker exec jarvis-redis redis-cli XREVRANGE jarvis:events + - COUNT 10`.

### Notification digest (no more push-spam)
Agents now route alerts through Synapse. **Urgent** items (a person at the door)
arrive immediately; **normal/low** items batch into one **hourly digest card**
titled "🗒️ Jarvis digest — N updates."
- Tune: `DIGEST_INTERVAL_S` (default 3600), `DIGEST_MAX` (flush when queue hits 8),
  `NOTIFY_DEDUP_TTL_S` (default 1800).
- Force a flush now: publish to `jarvis/notify/flush`, or it releases automatically
  when Focus mode ends.

### LLM cost tracking
Every background-agent LLM call is costed and attributed per agent automatically.
See it on the **dashboard "LLM Spend" widget** (`http://localhost:8888`). Raw data:
`usage:daily:{YYYY-MM-DD}` and `widget:cost:data` in Redis. Prices are editable in
`services/agent_runner/usage.py`.

---

## Phase 1 — Connective tissue

### Synapse — cross-agent insights (automatic)
Joins signals across agents and surfaces an insight (into your digest) when a
cross-domain rule fires. Built-in rules:
- **Recovery vs training** — HRV/sleep/RHR down + a workout planned → suggests an easy day.
- **Short sleep before an early start** — low sleep + an early calendar event.
- **Dining spend vs groceries** — high takeout spend + no fresh grocery plan → offers meal prep.
- **Stale next-actions** — a GTD next-action untouched 4+ days.
- **Supplement reminder** — see Phase 4.

Tune cadence: `SYNAPSE_RULE_INTERVAL_S` (300), cooldown `SYNAPSE_RULE_COOLDOWN_S` (43200).
Add rules in `services/synapse/rules.py` with the `@rule` decorator.

### Chronicle — daily memory
Writes a nightly journal (11:15 PM CDT) from the day's signals, and **catches up
days missed while the Mac slept**. Ask:
- "What did I do last Tuesday?" / "yesterday" / a specific date
- "When did the plumber come?" / "how has my week been?"

Powered by the `recall_journal` tool (Chroma-indexed). Journal entries persist in
Redis `chronicle:day:{date}` and `chronicle:entries`.
- **On-device (free) summarization:** set `CHRONICLE_LLM_MODEL` + `OPENAI_BASE_URL`
  (a local Ollama) — otherwise it uses the API model.
- Catch-up cap: `CHRONICLE_CATCHUP_MAX` (default 3 days).

---

## Phase 2 — High-value daily tools

### Spend Guardian (Brad's watch)
Runs daily; flags **new subscriptions** (with monthly total + next-charge date),
**unusual one-off charges**, and **low cash** vs your buffer — all into your digest.
Ask anytime: "how's my spending?", "what subscriptions do I have?", "am I over
budget?", "anything unusual on my accounts?" (`get_spending_insights`).
- Setup: `cash_buffer_usd` (profile, default 500), `dining_alert_usd` (default 250).
- Needs `BANKSYNC_API_KEY`.

### Task loop (GTD)
A persistent inbox / next-actions list across every surface (`manage_tasks`):
- "Add *call the dentist* to my list" · "add *file taxes* under the finance project"
- "What's on my plate?" / "what are my next actions?"
- "Mark *the dentist thing* done" · "make *file taxes* a next-action" · "clear finished tasks"

### Readiness score
One recovery number (0–100) computed from HealthKit (HRV, resting HR, sleep, prior
load) vs **your own baselines**, whenever a health snapshot syncs. Ask: "how
recovered am I?", "should I train hard today?" (`get_readiness`). Stored at
`user:readiness:today`.

### Lighting scenes (natural-language)
Capture the current lights as a named scene and replay it (`manage_scenes`):
- "Save this as **movie night**" → captures on/off, brightness, color, effect of every light
- "Set **movie night**" · "what scenes do I have?" · "delete movie night"

### Departure automation (automatic)
When you leave the geofence: managed lamps turn **off** and **away-mode** engages
(Sentry escalates while you're out). Cleared automatically when you're back.
- Setup: `departure_lights_off` (profile, default true). Uses the same lamps as the
  arrival scene (`arrival_lights`).
- Arrival greeting always fires; arrival **lights** only during `arrival_scene_hours`
  (default `18-07`) so a daytime arrival doesn't turn lights on.

### Email reply drafts (Hermes)
During inbox triage, Hermes drafts replies for action-needed mail. Review them:
"show my email drafts", "any emails I need to reply to?" (`get_email_drafts`).
To send: confirm and Jarvis uses the existing `send_email`.

### Focus mode
"Focus for 90 minutes" / "start a deep-work session" (`focus_mode`). While active,
Jarvis **holds all non-urgent notifications** (urgent still gets through) and releases
the batch when Focus ends. "How much focus time is left?" · "end focus."

---

## Phase 3 — Glasses / multimodal

Jarvis already *sees* photos you send from the app/glasses — these let it **act** on
what it sees.

### Sage — nutrition
Show a meal (photo) or describe it → "log this meal" / "log a grilled chicken bowl."
Jarvis estimates macros and tallies them (`log_meal`). Check in: "what have I eaten
today?", "how's my protein?" (`get_nutrition_today`). Targets come from your profile
(`weight_lbs`, `protein_goal_g_per_lb`).

### Visual memory
"Remember my **keys are on the counter**" / show a photo → "remember this"
(`log_sighting`). Later: "where did I leave my keys?", "have you seen my passport?",
"what did that wine look like?" (`recall_visual`, Chroma-searchable).

### Quick capture (hands-free)
"Note that…", "remind me to…", "add milk to the list", "idea: …" → auto-filed to the
right place (task inbox / shopping list / notes) with one step (`quick_capture`).

### OCR / translation (native)
Show a photo of a sign, menu, or document → "what does this say?", "translate this to
English." No special command — the multimodal brain reads and translates directly.

### Doorbell concierge
Sentry logs every delivery it sees and flags ones that arrive **while you're away**.
Ask: "did any packages come?", "anything delivered while I was out?" (`get_packages`).

---

## Phase 4 — Infrastructure & reliability

### Webhook ingress
Let external services push onto Jarvis's bus (routed into your digest):
```
POST http://<gateway>:8080/webhook/<source>?secret=<WEBHOOK_SECRET>
Content-Type: application/json
{"title":"Deploy finished","text":"prod build #482 is live","urgency":"normal"}
```
- Enable by setting `WEBHOOK_SECRET` in `.env` (empty = disabled).
- Use for IFTTT, GitHub, Stripe, home automations, CI, etc. `urgency:"urgent"` bypasses
  the digest.

### Supplement / medication reminders
Set once, then automatic daily nudge:
- "Set my supplements to *creatine, vitamin D, omega-3*" → profile `supplements`
- "Remind me at 8am" → profile `supplement_hour` (0–23, default 8)

### Self-healing watchdog (automatic)
`scripts/watchdog.sh` runs every 5 min via launchd (`com.jarvis.watchdog`). Heals dead
native-audio processes, unreachable Redis/MQTT, and down containers; alerts once/hour by
iMessage if it had to fix something.
- Status: `launchctl list | grep jarvis` · Logs: `logs/watchdog.log`
- Reinstall: `cp scripts/com.jarvis.watchdog.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.jarvis.watchdog.plist`

### Golden-task regression gate (for Forge)
`python3 scripts/golden_tasks.py` — compiles every module, parses configs, asserts the
critical tools stay registered. Exit 0 = safe, 1 = regression. Forge runs this after
self-edits so it can't silently break the system.

### Local model tier (Month 3)
Route cheap, latency-tolerant work to a **local Ollama** ($0/call, on-device):
- Set `OLLAMA_BASE_URL` (e.g. `http://host.docker.internal:11434/v1`) and
  `LOCAL_LLM_MODEL` (e.g. `llama3.1`).
- The brain's `"local"` tier and Chronicle's `CHRONICLE_LLM_MODEL` then run locally;
  falls back to the cheap cloud tier if unset.

### Household multi-user (foundation)
The verified speaker (from `speaker_verify`) is recorded to `jarvis:current_speaker` so
Jarvis can key behavior off who's talking. Full per-user profile isolation needs each
voice enrolled — this is the non-breaking hook it builds on.

---

## Round 2 — Closing the loops & beyond

The Phase 0–4 features produced signals; this round makes them **feed each other**,
adds reliability, and expands capability.

### Closed loops (mostly automatic)
- **Readiness drives the day** — the morning brief calls `get_readiness` and lets
  the score set its tone; Apollo scales training volume to it.
- **Protein-gap nudge** — Synapse flags an afternoon/evening protein shortfall from
  what Sage logged, vs your profile target.
- **Weekly review** — Sunday 6pm, Chronicle delivers a "week in review" card:
  what happened, trends (sleep/training/spend/subscriptions), one suggestion.

### Reliability
- **Disk-space guardian** — the watchdog prunes Docker images/cache when reclaimable
  ≥ `DISK_PRUNE_THRESHOLD_GB` (20), and emergency-prunes if Redis can't write.
- **Nightly backup** — `scripts/backup.sh` snapshots Redis + Chroma to `./backups/`
  (rotates to `BACKUP_KEEP`); restore steps in the script header.
- **Undo + audit** — "what did you just do?" (`get_recent_actions`) and "undo that"
  (`undo_last_action`) — reverses reversible actions (a task add/complete, a setting),
  reports that sends/orders can't be taken back.

### New capabilities
- **Web scrape+extract** (`scrape_page`) — renders JS/anti-bot pages to clean text
  and can pull out a specific fact/table. Use over `fetch_url` for hard pages.
- **People / relationships** (`manage_people`) — remember people (relationship,
  birthday, notes, last contact); Synapse surfaces birthdays + "reach out" nudges.
- **Routines** (`detect_routines`) — mines the event stream for recurring habits
  ("usually leaves ~5pm Tue/Thu").
- **Trip planning — Miles** (`plan_trip` / `get_trips`) — records a trip and runs the
  planning workflow; **flights via Kiwi MCP** + **rentals via Airbnb MCP** + hotels via
  `scrape_page`. (Jinko/Expedia hotel MCPs are flaky upstream — see notes.)
- **Memory consolidation** (`consolidate_memory`) — dedupes remembered facts.
- **Outbound calls** (`make_call`) — speaks a message via Twilio (creds-gated;
  confirm before dialing).
- **Notification feedback** — Synapse suppresses rules you keep dismissing (backend
  ready; iOS posts `/notify/feedback {key, action}` on tap/dismiss).

### Dynamic web watches (how Scout works)
Scout (web_monitor) watches specific pages for **new items matching a goal**
(apartments, restocks, ticket drops, price/job changes). Each run it fetches the
page (**Firecrawl** when `FIRECRAWL_API_KEY` is set, else the local scraper),
**hashes the text, and only calls the LLM to extract + diff when the page actually
changed** — so idle cost is ~zero.

Two ways to define watches:
- **Static** — `params.watch_urls` in `config/agents.yml` (needs an `agent_runner`
  restart to reload). Each entry can set `url`, `goal` (what to extract), `noun`,
  `alert_title`, and `priority_keywords`.
- **Dynamic** — `manage_watches` (voice): "watch `<url>` for `<goal>`", "what am I
  watching?", "stop watching …". Stored in Redis `scout:watches` and **merged with
  the config watches each run** — no restart, no YAML edit. New matches → iOS push +
  iMessage, priority items first.

---

## Configuration reference

### `.env` (compose passthroughs — rebuild service to apply)
| Var | Default | What |
|---|---|---|
| `DIGEST_INTERVAL_S` / `DIGEST_MAX` | 3600 / 8 | digest cadence / size |
| `NOTIFY_DEDUP_TTL_S` | 1800 | suppress duplicate alerts |
| `SYNAPSE_RULE_INTERVAL_S` / `_COOLDOWN_S` | 300 / 43200 | rule tick / per-rule cooldown |
| `CHRONICLE_LLM_MODEL` + `OPENAI_BASE_URL` | — | on-device journaling |
| `CHRONICLE_CATCHUP_MAX` | 3 | max missed days summarized |
| `OLLAMA_BASE_URL` / `LOCAL_LLM_MODEL` | — / llama3.1 | local model tier |
| `WEBHOOK_SECRET` | — | enable `/webhook/{source}` |
| `DEPARTURE_GRACE_S` | 120 | GPS-flap hysteresis on leaving |
| `FIRECRAWL_API_KEY` | — | Scout + `scrape_page` managed scraping |
| `TWILIO_ACCOUNT_SID` / `AUTH_TOKEN` / `FROM_NUMBER` | — | enable `make_call` |
| `DISK_PRUNE_THRESHOLD_GB` | 20 | watchdog auto-prune trigger |
| `BACKUP_KEEP` | 7 | nightly snapshots to retain |

### `user:profile` (set by voice — no rebuild)
| Field | Purpose |
|---|---|
| `cash_buffer_usd` | Spend Guardian low-cash threshold |
| `dining_alert_usd` | takeout-spend nudge threshold |
| `supplements`, `supplement_hour` | daily supplement reminder |
| `arrival_scene_hours` | window arrival lights fire (default 18-07) |
| `departure_lights_off` | turn lamps off on leaving (default true) |
| `weight_lbs`, `protein_goal_g_per_lb` | nutrition/readiness targets |

---

## Where things live

- **New service:** `services/synapse/` (event store + notify router + correlation)
- **New agents:** `services/agent_runner/{chronicle,spend_guardian}_agent.py`
- **New brain tools:** `services/llm_agent/tools/{chronicle,finance_insights,tasks,health,scenes,email_drafts,focus,nutrition,visual_memory,capture,packages}.py`
- **Scripts:** `scripts/watchdog.sh`, `scripts/golden_tasks.py`, `scripts/com.jarvis.watchdog.plist`
- **Dashboard widget:** `services/dashboard/widgets/cost/`
