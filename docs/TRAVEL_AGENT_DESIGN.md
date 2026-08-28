# Travel Agent — "Miles" — Design (APPROVED + BUILT v1, 2026-07-18)

Omar reviewed and chose **assisted booking (mode A)**. v1 built same day:
- Brain: `propose_booking` tool (files itinerary + links in the Approval
  Inbox) alongside existing `plan_trip`/`get_trips`; planning runs in
  conversation with the Kiwi/Jinko/Airbnb MCPs.
- Runner: `travel_agent.py` — `confirm` (approval accepted → booking-links
  card; user pays in their own browser) + daily `watch` cron (re-prices
  proposed trips by asking the brain over the bus; alerts on drops ≥ 5%,
  `TRAVEL_WATCH_DROP_PCT`).
- Still open from the decision list: profile facts (home airport, loyalty
  programs — Miles asks conversationally for now), ground transport scope,
  calendar blocks (waiting on Google Calendar MCP OAuth).

Original proposal below for reference.

## What already exists (build on, don't rebuild)

- MCPs live in the brain: **kiwi_flights**, **jinko_hotels**, **airbnb**,
  **google_maps** (expedia registered, off). Flight/hotel/rental *search*
  needs zero new integrations.
- `tools/travel.py` — `plan_trip` / `get_trips` skeleton (trip storage in
  Redis `travel:trips`).
- Price watching — `price_monitor` agent + `manage_watches` (Scout) patterns.
- **Approval Inbox** (new) — the missing piece that makes *booking* safe:
  Miles never books; he files an approval with the exact price/itinerary.
- Playwright MCP (fresh sessions) + `mac_chrome_*` (your signed-in browser)
  for anything without an API.

## Proposed shape

**Persona agent `travel` ("Miles")** in agent_runner, on-demand via
`spawn_task(agent="travel")` + a light daily cron for watched trips.

Three modes:

1. **Plan** ("Miles, plan a long weekend in Denver in September"):
   constraints from conversation + profile (home airport, budget style,
   loyalty programs) → kiwi/jinko/airbnb searches → 2-3 coherent options
   (flight+stay pairs with total math, commute times via Maps) → report card
   to app/dashboard; chosen option saved to `travel:trips:{id}`.
2. **Watch** (auto after a plan is saved): daily price re-check on the chosen
   flight/stay; drop ≥ threshold → notification; "book-by" date nudges.
3. **Book** — ⚖️ **decision point**. Options:
   - **A (recommended v1): assisted booking** — Miles files an Approval-Inbox
     card with the final itinerary + deep links; you tap through and pay in
     your own browser in 2 minutes. Zero payment risk, works everywhere.
   - **B: automated via your signed-in browser** (mac_chrome path, like
     Remy's carts) — approval-gated, but fragile against airline/OTA bot
     defenses and real money on the line.
   - Airbnb/OpenTable-style link-outs stay manual regardless.

## ⚖️ Decisions for Omar

1. Booking mode: A (assisted, recommended) or B (automated, approval-gated)?
2. Profile facts to store now: home airport (AUS?), seat/class preference,
   hotel vs Airbnb bias, loyalty numbers (which?), typical budget band.
3. Does Miles own ground transport too (rental car, Uber estimates) in v1?
4. Calendar integration: should a planned trip auto-propose calendar blocks
   (via the Google Calendar MCP once its OAuth lands)?

## Build estimate

v1 (plan + watch + assisted booking): one Forge-sized session — the agent
file, trip storage/watch cron, report cards, approval wiring. Mode B adds a
second session of browser-flow hardening per booking site.
