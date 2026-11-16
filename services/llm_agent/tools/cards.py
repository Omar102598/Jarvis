"""Rich chat cards — structured UI a tool attaches to its result.

A tool that has something worth SHOWING (a macro tracker, a weekly plan, a
trend) builds a card here and appends the returned marker to its result text.
The brain's stream loop finds ``[[card:<id>]]`` in tool results, moves the
card onto the turn, and the gateway hands the turn's cards to the iOS app,
which renders them natively above the reply.

Cards are built by code from real data -- never JSON the model writes -- so a
progress bar can't show a number the tool didn't compute.

Card types (``type`` field; the iOS app renders each):
    macros  {title, calories:{value,target}, bars:[{label,value,target,unit,color}], meals?, footer}
    stats   {title, subtitle?, items:[{label,value,sub?}]}
    table   {title, columns:[...], rows:[[...]], footer?}
    chart   {title, kind:"bar"|"line", unit, points:[{x,y}], target?, footer?}
    action  {title, subtitle, icon, approval_id?, status:"pending"|"done", footer?}
    products {title, subtitle?, products:[{name, price?, image?, url?, detail?, badge?}], footer?}
            One product is drawn large; several scroll sideways. Tapping a
            product opens its url (a store page, an app link).

Links the brain writes into its reply get a preview in the app by themselves
(Apple's LinkPresentation), so a card is only needed when the tool has better
data than the page's preview: the real price, the item's own photo.

Redis: jarvis:card:{id}  json, 1 day (only needs to live until the turn ends)
"""

from __future__ import annotations

import json
import os
import re
import uuid

import redis

_r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)

CARD_MARKER = re.compile(r"\[\[card:([0-9a-f]{10})\]\]")
_NOTE = " (A card with these numbers is shown to the user — don't repeat them all.)"


def attach(card: dict) -> str:
    """Store a card and return the marker to append to a tool's result."""
    cid = uuid.uuid4().hex[:10]
    try:
        _r.set(f"jarvis:card:{cid}", json.dumps(card), ex=86400)
    except Exception:
        return ""   # a card is decoration; never fail the tool over it
    return f"\n[[card:{cid}]]{_NOTE}"


def products(title: str, items: list[dict], subtitle: str = "", footer: str = "") -> str:
    """Attach a products card. Each item: name, and any of price, image, url,
    detail, badge. Items without a name are dropped; returns "" if none are left."""
    keep = ("name", "price", "image", "url", "detail", "badge")
    rows = [{k: str(it[k]) for k in keep if it.get(k)} for it in items if it.get("name")]
    if not rows:
        return ""
    return attach({"type": "products", "title": title, "subtitle": subtitle,
                   "products": rows[:12], "footer": footer})


def take(text: str) -> list[dict]:
    """Cards referenced by markers in `text` (used by the brain's loop)."""
    out = []
    for cid in CARD_MARKER.findall(text or ""):
        try:
            raw = _r.get(f"jarvis:card:{cid}")
            if raw:
                out.append(json.loads(raw))
        except Exception:
            continue
    return out


def strip(text: str) -> str:
    """Remove markers (and the note) if the model echoes them into its reply."""
    return CARD_MARKER.sub("", (text or "").replace(_NOTE, "")).strip()
