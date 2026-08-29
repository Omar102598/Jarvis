# Setting Up Your Own Jarvis

For someone standing up a **fresh, independent Jarvis** — your own machine,
your own API keys, your own data. Nothing is shared with anyone else's
instance.

## What you need

| Required | Why |
|---|---|
| Docker + Docker Compose | Runs the whole stack |
| An **Anthropic API key** | Pays for the LLM — billed to *your* account |
| ~30 min | First build pulls a few GB |

| Optional (add later, one at a time) | Unlocks |
|---|---|
| Home Assistant + long-lived token | Lights, scenes, Apple TV, pet feeders |
| Tavily key | Web search / the research agent |
| Firecrawl key | Reliable page scraping for watches & research |
| Google Maps key | Directions, commute times |
| Gmail app password | Inbox triage (Hermes) |
| Ring account | Cameras + the Sentry watch agent |
| Apple Developer account ($99/yr) | iOS app on your phone + push notifications |

**Everything optional degrades gracefully** — a tool with no key just reports
it isn't set up. Start minimal.

## Cost expectations

You pay Anthropic directly for tokens. Typical solo usage runs **$3–15/day**
depending on how chatty you are and which agents you enable — the brain is
~95% of it, background agents are pennies. Guardrails ship on by default:

- `DAILY_LLM_BUDGET_USD` (default 15) sends one alert when crossed.
- The dashboard's **cost widget** breaks spend down by brain vs each agent.
- Agents needing paid integrations start **disabled** in the example config.

Set a **spend limit in the Anthropic console** too — if credits run out, the
brain returns a generic error on every request (it looks like a broken Jarvis,
not a billing problem).

---

## 1. Clone and configure

```bash
git clone https://github.com/Omar102598/Jarvis.git && cd Jarvis
python3 scripts/setup_wizard.py
```

The wizard asks only for what Jarvis actually needs, **checks each key against
the real service before saving it**, and leaves everything else alone. A wrong
Anthropic key is caught in the wizard rather than surfacing later as a stack of
failing agents.

It is safe to re-run — it keeps what you already have and only fills gaps, so
it doubles as the way to add an integration later. It also generates
`MOBILE_API_KEY` (the phone app ↔ gateway shared secret) so you do not have to
invent one.

```bash
git clone <repo-url> Jarvis && cd Jarvis
cp .env.example .env
cp config/agents.example.yml config/agents.yml
```

Edit `.env` — the four in the "MINIMUM TO BOOT" block are all you need to start:

```
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-4-5
MOBILE_API_KEY=<openssl rand -hex 32>
USER_TZ=America/Chicago
```

Also set `GATEWAY_PUBLIC_URL` to how your phone will reach this machine
(Tailscale hostname or LAN IP, e.g. `http://192.168.1.50:8080`) — push cards
with camera images use it.

`config/agents.yml` is *yours* — it's where you turn agents on/off and set what
they watch. The example ships with the cheap, universally-useful agents on and
anything needing credentials off.

## 2. Start the stack

Pass the explicit service list — a bare `docker compose up -d` also starts the
containerized voice services, which pull a ~1.1 GB CUDA image you probably
don't want yet.

**Recommended — pull prebuilt images (~2 min):**

```bash
SVCS="mosquitto redis chroma llm_agent mobile_gateway agent_runner dashboard synapse"
docker compose pull $SVCS
docker compose up -d --no-build $SVCS
```

Images are published to GHCR by CI for both `linux/amd64` (Intel/AMD, incl. the
UGREEN NAS's N100) and, when built with the multi-arch option, `linux/arm64`
(Apple Silicon, Hetzner CAX, Raspberry Pi). Docker picks the right one.

**Alternative — build from source (~20-30 min, or much longer on a low-power
box).** Only needed if you've modified service code or want an architecture CI
doesn't publish:

```bash
docker compose up -d --build $SVCS
```

Either way, watch until the brain reports its tool count:

```bash
docker compose logs -f llm_agent      # wait for "Graph rebuilt — N tools"
```

MCP servers that lack credentials will log `Could not load server 'x'` and skip
— that's expected and harmless.

Verify:

```bash
curl localhost:8080/health            # gateway
open http://localhost:8888            # dashboard
python3 scripts/golden_tasks.py       # full static health check
```

## 3. Talk to it

Quickest check, straight over the message bus:

```bash
docker exec jarvis-mqtt mosquitto_pub -t jarvis/llm/request \
  -m '{"text":"who are you","room":"test","verified":true,"source":"cli"}'
docker exec jarvis-mqtt mosquitto_sub -t 'jarvis/tts/test/speak' -C 1
```

Or use the HTTP API:

```bash
curl -X POST localhost:8080/ask/query \
  -H "Content-Type: application/json" -H "X-API-Key: $MOBILE_API_KEY" \
  -d '{"text":"what can you do?"}'
```

## 4. Personalize it (important)

Jarvis keys most behavior off a **user profile** in Redis, not off hardcoded
values. Until you set yours, agents fall back to generic placeholders — the
grocery agent in particular will compute calorie targets from placeholder body
stats. Just talk to it:

> "personalize jarvis"

That runs a conversational onboarding interview. Or set fields individually:
*"set my weather location to Denver"*, *"my pets are two cats"*, *"set my
workout split to push/pull/legs"*. Ask **"what's my setup status?"** anytime for
an audit of which integrations are configured and what's missing.

## 5. Add integrations as you want them

Each is a key in `.env` + a `docker compose up -d <service>` to pick it up
(env changes need a **recreate**, not just a restart):

1. **Home Assistant** — add the `homeassistant` service, onboard at
   `:8123`, create a long-lived token → `HA_TOKEN`. Unlocks lights, scenes,
   Apple TV, PetKit, and the HA MCP server.
2. **Search** — `TAVILY_API_KEY` + `FIRECRAWL_API_KEY`.
3. **Email triage** — `IMAP_USER` + a Gmail **app password** (not your login).
4. **Cameras** — add `ring_mqtt`, do the one-time login at `:55123`, then
   enable the `sentry` agent.
5. **MCP servers** — toggle in `config/mcp_servers.yml`; OAuth ones use
   `scripts/authorize_google_mcps.sh` / `authorize_food_mcps.sh`.

Turn on paid-integration agents in `config/agents.yml` only after their keys
are in place.

## 6. Voice (optional, more involved)

The always-listening pipeline (wake word → STT → brain → TTS) needs real audio
hardware. On a Mac the native services are the good path (better latency, uses
the system mic/speaker); the containerized `wake_word`/`stt`/`tts` services are
the Linux path. See `docs/architecture.md`. **Do this last** — everything else
works over the app, dashboard, and API without it.

## 7. Phone app (optional, needs Apple Developer)

`JarvisApp/` is an Xcode project. To run it on your own phone you must:

- Open it in Xcode and set **your own bundle identifier** and signing team.
- Update the App Group in `JarvisApp/Sources/Network/JarvisClient.swift`
  (currently `group.com.omarsalazar.jarvis`) to your own
  `group.<your-reverse-domain>.jarvis`, and match it in the target's
  capabilities.
- Set the app's Server URL to your `GATEWAY_PUBLIC_URL`.
- For real push notifications, create an APNs key (.p8) and fill the `APNS_*`
  block in `.env`.

Without an Apple Developer account you can still use the **dashboard**, the
**HTTP API**, and voice — the app is a convenience surface, not a requirement.

---

## Things that are one-person-per-instance (by design, today)

One Jarvis stack serves **one person**, and this is structural — not just a
matter of some keys being unnamespaced.

`JARVIS_USER_ID` is read from the environment **once at container startup**
(`USER_ID = os.environ.get("JARVIS_USER_ID", "default")`), so it is a constant
for the whole deployment. Requests don't carry a user identity. That means two
people talking to one instance share **everything** — memory, conversation
history, tasks, profile, health, grocery, workout plans, and the Approval
Inbox — regardless of what `JARVIS_USER_ID` is set to.

In practice that would be a bad experience, not just a privacy issue: the
morning brief would blend both calendars, Apollo would program one training
plan from two people's recovery data, Remy would merge two diets into one
grocery list, and each person would see the other's approval cards.

**Two people = two deployments.** They can share hardware (see below) but not
a stack.

*Groundwork exists for the future:* `speaker_verify` already tags each voice
request with a verified speaker name (stored as `jarvis:current_speaker`), so
Jarvis can address people individually — but it does not switch data
namespaces. Full multi-tenancy is a planned direction
(`docs/MULTI_USER_BILLING_PLAN.md`), not something to work around today.

### Sharing one machine between two people

Two isolated stacks on one host works today with no code changes — separate
Compose project names, ports, volumes, and `.env` files:

```bash
# Person A
docker compose -p jarvis_a --env-file .env.a up -d
# Person B (different MOBILE_GATEWAY_PORT, DASHBOARD_PORT, ANTHROPIC_API_KEY)
docker compose -p jarvis_b --env-file .env.b up -d
```

Each project gets its own Redis, Chroma, and MQTT, so the data is genuinely
separate and each person's tokens bill to their own key. Budget **~5–6 GB RAM
per stack** — so this needs a 16 GB+ host, not a free-tier box.

## Where to look when something breaks

- `python3 scripts/golden_tasks.py` — static health (compiles, configs, tools).
- Dashboard **Logs** page, or `docker compose logs -f <service>`.
- `docker stats` — a container at ~100% CPU with climbing network I/O is
  working (MCP cold start), not hung.
- Brain says "I encountered an error" on *everything* → check your Anthropic
  credit balance first.
