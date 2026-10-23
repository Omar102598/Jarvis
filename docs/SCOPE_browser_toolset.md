# Scope: adopt Anthropic's browser toolset (`browser_toolset_20260801`)

**Status:** not started · **Suggested owner:** a working session, not Forge
**Written:** 2026-10-22

## Why not Forge

This touches the brain's agent loop, adds a 31-tool surface, and points a model
at a browser holding live Amazon/HEB sessions that can spend money. The failure
modes are security and silent-wrong-action, not "the layout looks off." It wants
a session where the work can be driven and checked step by step.

The dashboard split is the better Forge task: self-contained, visually
verifiable, low blast radius.

## What this actually is

**A client toolset, not a hosted browser.** Nothing runs on Anthropic's
infrastructure. Claude emits member-tool calls; *our* executor dispatches them
to *our* browser and returns results. We already own the browser.

This is the opposite of `web_fetch`, which shipped in one line because it is a
server tool. Do not expect similar effort here.

| | `web_fetch` (done) | `browser_toolset_20260801` |
|---|---|---|
| Who runs it | Anthropic | **Us** |
| Integration | `bind_tools` only | Executor + state tracking + validation |
| In `ToolNode`? | No | **Yes** — we dispatch every member |

## Why it is worth doing

It reads the **accessibility tree** with element references, rather than pixels
like computer use. No `$0.01`-per-screenshot loop, no coordinate guessing.

The concrete win is the failure mode we keep hitting: scripted selectors break
silently. HEB's add-to-cart fired analytics and never issued a cart request
while reporting success. Amazon still clicks `#freshAddToCartButton` by DOM.
This toolset is the right *fallback tier* when a scripted path fails — not a
replacement for it.

**Keep the existing ladder.** Captured API (HEB, Target) → scripted Playwright
→ browser toolset. Routing a known one-call API through an LLM loop would be
slower, costlier, and less deterministic for zero gain.

## What we already have

`mac_bridge` (port **7777**, headful Chromium with persisted `storage_state`)
exposes nearly the needed surface: `/browser/navigate`, `/browser/click`,
`/browser/fill`, `/browser/press`, `/browser/js`, `/browser/read`,
`/browser/screenshot`, `/browser/network`, `/browser/network/clear`,
`/browser/url`, `/browser/save-state`.

Mapping is close to one-to-one. The gaps are tab management
(`new_tab`/`list_tabs`/`switch_tab`/`close_tab`), `find` (natural-language
element lookup), `read_page` (accessibility tree with ref ids), and `zoom`.

Note `_get_page()` is the **visible, logged-in** browser; the headless scraper
is a separate instance. Know which one each member tool targets.

## Work breakdown

1. **Executor** — dispatch each member call to mac_bridge, return results in
   the documented block shapes. The bulk of the work.
2. **`browser_state` tracking** — report tab state back each turn.
3. **Accessibility tree** — `read_page` with stable element refs; this is the
   toolset's whole advantage and the least covered by current endpoints.
4. **Validation** — coordinates against the viewport, URL schemes, domain
   allowlist.
5. **Wiring** — add to `bind_tools` *and* `ToolNode`, unlike the server tools.
6. **Optional members** — `read_network`, `javascript_exec`, `file_upload` are
   off by default. `read_network` is how we captured HEB and Target and is the
   obvious candidate for enabling first.

## Security — the part that needs a decision

Anthropic's docs are emphatic: a dedicated low-privilege profile, human
confirmation for account-changing actions, and an isolated container.

**Our browser is the opposite of that.** It holds live Amazon, HEB, and Target
sessions with saved payment methods. An agent loop with free rein there can
spend real money, and page content is untrusted input that could carry
injection.

Decide before building:

- Separate profile for agent-driven browsing vs. the logged-in one?
- A confirmation gate on anything transactional (checkout, submit, purchase)?
- Domain allowlist — grocery and research hosts only?

A reasonable default: the toolset gets the **scraper** profile by default and
only reaches the logged-in browser behind an explicit, per-task opt-in.

## Acceptance criteria

1. A multi-step task completes end to end against a benign site.
2. `read_page` returns usable element refs, and clicking by ref works.
3. The brain still answers normally with the toolset enabled (no latency or
   token regression on ordinary turns).
4. Every transactional action is gated or blocked by policy.
5. One feature flag disables the whole thing.

## Open question worth settling first

Does this subsume the scripted grocery paths, or sit beside them as a fallback?
The recommendation here is **beside** — but measure a real Amazon cart-add
through the toolset (cost, latency, success rate) before deciding, rather than
assuming either way.
