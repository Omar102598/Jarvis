# Scope: split the dashboard into three pages

**Status:** not started · **Owner:** Forge (`spawn_task agent="developer"`)
**Written:** 2026-10-22

## Why

The dashboard is one 1,713-line page doing three unrelated jobs at once: the
voice orb, the conversation feed, and the widget/agent grid. They compete for
the same screen, which is why the agent rows ended up unreadable and the orb
got squeezed. Splitting them lets each be sized for its own purpose.

## The three pages

| Route | Contains | Design intent |
|---|---|---|
| `/voice` | The orb, nothing else | As large as the viewport allows. Glanceable from across the room. |
| `/chat` | Conversation feed + **text input** + **click-to-speak** | Parity with the iOS app's chat tab. |
| `/` (or `/ops`) | Widgets, nudges, agent cards | The existing grid, with the orb's space reclaimed. |

Keep a persistent nav between the three. Decide once whether `/` is the ops
page or a redirect to a remembered last-page; don't leave it ambiguous.

## The one piece that is genuinely new

Everything except the chat input is a layout change over existing endpoints.
**The dashboard has never accepted user input** — it publishes only
`jarvis/agents/{name}/trigger` (`main.py:405`) and
`jarvis/approvals/resolve` (`main.py:454`). There is no path for a typed
message.

**Do not build a new inbound pipeline.** `mobile_gateway` already owns this and
the iOS app already uses it:

- `POST /ask/query` — text in, JSON display payload out (`mobile_gateway/main.py:742`)
- `POST /ask/query/audio` — audio bytes in, same shape out (`:786`)

The iOS client calls these via `JarvisClient.askText(text, speak:)`
(`ChatViewModel.swift:262`). The dashboard should add a thin server-side proxy
route and call the same endpoints. Two reasons it must be server-side: the
browser should not hold gateway credentials, and the dashboard already holds
the session cookie that authorises the user.

Note `askText(text, speak: false)` in the iOS app — typed messages deliberately
do **not** speak the reply, because your eyes are already on the screen. Match
that. Click-to-speak should speak; typing should not.

## Endpoints that already exist (reuse, don't rebuild)

`/api/agents`, `/api/approvals`, `/api/widgets`, `/widgets/{name}/html`,
`/api/widgets/{name}/data`, `/api/conversation`, `/api/voice-state`,
`/api/tool-events`, `/api/agents/events`, `/api/stream`.

## Gotchas that have already cost time

- **Widget JS must read `resp.data`**, not the raw response.
- **Inline widget `<script>` tags do not execute when injected via
  `innerHTML`** — they must be re-created as nodes. This has bitten before.
- Widgets are volume-mounted; adding one needs no rebuild.
- `/api/voice-state?room=office` is polled frequently — keep that on the voice
  page only, not on all three, or you triple the request rate for nothing.
- The dashboard listens on **8888**, not 8086.

## Acceptance criteria

1. Each page is reachable, navigable, and renders without console errors.
2. The orb on `/voice` scales with the viewport and is legible across a room.
3. Typing a message on `/chat` gets a reply in the feed and **does not speak**.
4. Click-to-speak records, sends, replies, **and speaks**.
5. The ops page shows agent rows that are readable at a glance — the original
   complaint.
6. No page polls an endpoint it does not display.
7. Mobile-width viewports stay usable; this gets opened from a phone.


## Forge can verify this visually (new, 2026-10-22)

Forge has no Chrome tools — those come from the Claude Code Chrome extension,
which only attaches to an interactive session, not to headless `-p`. It gets
eyes a different way, and this was verified working before assigning the task:

1. drive the Mac's browser via `mac_bridge` (`POST :7777/browser/navigate`)
2. `GET :7777/browser/screenshot` → `image_base64`
3. decode to a PNG and use the **`Read`** tool on it — a headless run can see
   images, confirmed by having one describe a screenshot correctly

So "the agent rows are readable" is checkable by Forge, not only by a human.
Judge the layout by looking at it, then iterate.

**URL trap:** the dashboard runs on the GCP VM. From the Mac use
`http://jarvis-core.tail7e74a7.ts.net:8888`; `localhost:8888` screenshots a
Chrome error page.

## Explicitly out of scope

Changing agent/widget behaviour, the widget producer contract, or auth. This is
a presentation split plus one proxy route.
