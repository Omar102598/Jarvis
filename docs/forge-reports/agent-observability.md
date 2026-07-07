# Agent Observability — Unified Activity Stream

**Date:** 2026-07-07
**Goal:** One chronological stream of every agent's tool calls / thinking steps / findings
(like Claude's chat shows tool use), rendered in the dashboard behind a DEV toggle.
Backend + dashboard only; the iOS app toggle is a later pass.

## Architecture

- `BaseAgent.log_event(kind, text)` writes `{agent, kind, text (≤300 chars), ts}` JSON to:
  - `jarvis:agent_events` (global, LTRIM 0 499) — read by the dashboard DEV panel + mobile gateway
  - `agent:{name}:events` (per-agent, LTRIM 0 99)
  - Kinds: `tool` (a tool call/command with args preview), `thinking` (what the agent is
    about to do), `finding` (a result/discovery). Never raises — Redis failures are swallowed.
- The brain's existing `jarvis:tool_events` stream (llm_agent `_push_tool_event`) is
  merged into the same panel server-side, tagged `agent='jarvis'`, `kind='tool'`.

## Files changed

### services/agent_runner/base_agent.py
Added `log_event(kind, text)` as described above. No changes to `store_report` or `run`.

### services/agent_runner/grocery_agent.py (logging only)
- Module-level `_EMIT` hook; `GroceryAgent.run()` binds it to `self.log_event` so the
  module-level pipeline functions (store scrapes, cart adds, LLM resolution) feed the stream.
- `_log(msg, kind="")` now forwards every line to the stream. Heuristic classification:
  lines starting `✓ ✗ ⚠ …` → finding; `[cart]`/`[checkout]`/Scanning/Fetching/Navigating/
  Resolving → tool; else thinking. Explicit `kind="tool"` on the per-item resolution line
  (`[i/N] item`).

### services/agent_runner/classpass_agent.py (logging only)
Same `_EMIT` hook pattern, bound in `ClasspassAgent.run()` (covers scan / search / book /
waitlist paths). Heuristic: `✓ ✗ ⚠ …` → finding; Navigating / trying studio / day advance /
date tab / cached → tool; else thinking.

### services/agent_runner/sentry_agent.py (logging only)
- `thinking` event before the vision assessment ("motion on 'X' — assessing snapshot").
- `finding` event with the verdict (NOTABLE / not notable + summary), and on vision failure.
- `tool` event in `_notify` (push + iMessage text).

### services/agent_runner/web_monitor_agent.py (logging only)
- `tool` event per URL watch check ("Checking watch 'name': url") and per Tavily query.
- Per-watch outcome lines (unchanged / fetch failed / baseline recorded / N NEW units…) now
  go through a local `record()` helper that appends to the report AND emits a `finding`.
  Note: the helper is named `record` (not `note`) because `note` was already a local variable
  in `_run_url_watches`.
- `tool` event in `_notify`; `finding` summarising query-watch results.

### services/agent_runner/developer_agent.py (logging only)
`_progress()` now mirrors every line into `log_event` (existing `agent:developer:progress`
list and print behavior kept exactly as before). Classification: step lines starting `[`
(e.g. `[3/40] tool_name {args}`) → tool; "Done." / "CLI run complete." / "Step limit
reached." → finding; else thinking.

### services/mobile_gateway/main.py
New `GET /agents/events?limit=100` (auth: `X-API-Key`, same as `/agents/feed`). Returns
`{"events": [...]}` newest-first from `jarvis:agent_events`. `limit` clamped to 1–500.

### services/dashboard/main.py
New `GET /api/agents/events?limit=100`: reads `jarvis:agent_events` directly from Redis,
merges the brain's `jarvis:tool_events` normalised to the unified shape
(`agent='jarvis'`, `kind='tool'`, `text=tool + args_preview [+ → result_preview]`),
sorts newest-first, returns `{"events": [...]}`.

### services/dashboard/templates/index.html
- **DEV toggle button** in the navbar (next to LOGS). State persisted in
  `localStorage['jarvis_dev_mode']`; gold glow when on; applied on page load.
- **"Agent Activity" panel** (hidden unless DEV is on) below the Tool Events timeline:
  newest-first rows with time, agent name, kind badge (tool = blue / thinking = grey /
  finding = green), and the event text. Scrollable (max 420px).
- Polls `/api/agents/events?limit=100` every 4s while DEV mode is on; polling stops when
  toggled off.

## How verified

- `python3 -m py_compile` on all 8 changed Python files from the repo root — all pass.
- Smoke test of `BaseAgent.log_event` with a fake Redis: both keys written, 300-char text
  cap enforced, newest-first order, `{agent, kind, text, ts}` shape correct, and a raising
  Redis client does NOT propagate (never-breaks-the-agent guarantee).
- `node --check` on the template's inline `<script>` block — syntax OK.
- Per constraints, **no services were rebuilt or restarted** and docker-compose.yml/.env
  are untouched — changes go live on the next rebuild of `agent_runner`, `mobile_gateway`,
  and `dashboard`. (The dashboard template + main.py both changed; if templates are
  volume-mounted the panel may appear before a rebuild, but the new `/api/agents/events`
  route needs the dashboard container restarted.)

## Left for the user

1. **Rebuild/restart** `agent_runner`, `mobile_gateway`, and `dashboard` to activate
   (deliberately not done from inside this run).
2. **iOS app**: skipped per instructions — a later pass adds the app-side Developer toggle
   consuming `GET /agents/events`.
3. Not instrumented (out of scope, single-shot or low-step agents): ambient, newsletter,
   email, job_monitor, research, task, price_monitor, finance, workout. They inherit
   `log_event` from BaseAgent, so instrumenting them later is a one-liner per call site.
4. The brain merge is server-side in the dashboard only; the gateway's `/agents/events`
   returns background-agent events only (no `jarvis:tool_events` merge there) — flag if the
   app should merge them too.
