"""Uber Eats — search restaurants, read menus, build carts. Never checks out.

Uber's own MCP server only searches, and only for Anthropic's Claude apps
(it answers "access denied" to everyone else). So these tools use the user's
signed-in Uber Eats session in Remy's Mac browser (the mac_bridge Playwright
page) and call the same JSON endpoints the ubereats.com web app calls, from
inside that page, so the session cookies ride along:

    getSearchSuggestionsV1       restaurants by NAME (the search box typeahead)
    getSearchFeedV1              search results for a cuisine/dish (ignores names)
    getStoreV1                   store info + full menu
    getMenuItemV1                one item's option groups
    getDraftOrdersByEaterUuidV1  the user's open carts (one per restaurant)
    createDraftOrderV2           new cart for a restaurant, with items
    addItemsToDraftOrderV2       add items to an existing cart
    removeItemsFromDraftOrderV2  remove cart lines (an emptied cart disappears)
    getCheckoutPresentationV1    a cart's subtotal / fees / total (read only)

Request shapes were captured from the web app on 2026-11-15. If Uber changes
them these fail loudly ("Uber Eats said: ...") rather than guessing.

Placing the order is deliberately NOT here: the user checks out in the Uber
Eats app, where the cart Jarvis built is waiting.

The browser page is shared with Remy; while he runs (Redis browser:busy) these
tools back off instead of navigating his page away.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import re
import uuid
from collections import Counter

import aiohttp
import redis
from langchain_core.tools import tool

from tools import cards

_HOST = os.environ.get("MAC_BRIDGE_HOST", "host.docker.internal")
_PORT = int(os.environ.get("MAC_BRIDGE_PORT", "7777"))
_BASE = f"http://{_HOST}:{_PORT}"
_r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)

_FEED_URL = "https://www.ubereats.com/feed"
_BUSY_KEY = "browser:busy"
_MENU_TTL = 3600            # menus change rarely within an hour
_STORE_NAME_TTL = 7 * 86400
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class UberError(Exception):
    pass


# ---------------------------------------------------------------------------
# Transport: run fetch() inside the signed-in ubereats.com page
# ---------------------------------------------------------------------------

async def _bridge(method: str, path: str, body: dict | None = None, timeout: int = 45) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.request(method, f"{_BASE}{path}", json=body) as resp:
            if resp.status >= 400:
                raise UberError(f"Mac browser error {resp.status}: {(await resp.text())[:200]}")
            return await resp.json()


async def _ensure_page() -> None:
    busy = _r.get(_BUSY_KEY)
    if busy:
        raise UberError(f"{busy.capitalize()} is using the Mac browser right now; "
                        "try Uber Eats again in a few minutes.")
    cur = (await _bridge("GET", "/browser/url", timeout=15)).get("url", "")
    if "ubereats.com" not in cur:
        await _bridge("POST", "/browser/navigate", {"url": _FEED_URL}, timeout=45)


async def _api(endpoint: str, body: dict) -> dict:
    """POST to an Uber Eats web endpoint from inside the page; return data."""
    await _ensure_page()
    script = (
        "(async () => { const r = await fetch('/_p/api/%s', {method: 'POST', "
        "headers: {'content-type': 'application/json', 'x-csrf-token': 'x'}, "
        "body: %s}); return await r.text(); })()" % (endpoint, json.dumps(json.dumps(body)))
    )
    raw = (await _bridge("POST", "/browser/js", {"script": script})).get("result") or ""
    try:
        out = json.loads(raw)
    except Exception:
        if "login" in raw.lower() or "<html" in raw.lower():
            raise UberError("Uber Eats isn't signed in on the Mac browser. Sign in at "
                            "ubereats.com in Remy's browser, then try again.")
        raise UberError(f"Unexpected reply from Uber Eats: {raw[:150]}")
    if out.get("status") != "success":
        msg = (out.get("data") or {}).get("message") or str(out)[:200]
        raise UberError(f"Uber Eats said: {msg}")
    return out.get("data") or {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _words(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split()


def _money(cents) -> str:
    try:
        return f"${float(cents) / 100:.2f}"
    except Exception:
        return "?"


def _best_match(query: str, titles: list[str]) -> int | None:
    """Index of the title that best matches query, or None."""
    q = " ".join(_words(query))
    if not q:
        return None
    norm = [" ".join(_words(t)) for t in titles]
    for i, t in enumerate(norm):
        if t == q:
            return i
    qw = set(q.split())
    hits = [i for i, t in enumerate(norm) if qw <= set(t.split())]
    if hits:
        return min(hits, key=lambda i: len(norm[i]))
    hits = [i for i, t in enumerate(norm) if q in t or (t and t in q)]
    if hits:
        return min(hits, key=lambda i: len(norm[i]))
    close = difflib.get_close_matches(q, norm, n=1, cutoff=0.6)
    return norm.index(close[0]) if close else None


async def _store(store_uuid: str) -> dict:
    data = await _api("getStoreV1", {"storeUuid": store_uuid, "diningMode": "DELIVERY",
                                     "time": {"asap": True}, "cbType": "EATER_ENDORSED"})
    if data.get("title"):
        _r.set(f"ubereats:store_name:{store_uuid}", data["title"], ex=_STORE_NAME_TTL)
    return data


def _store_url(store_uuid: str, slug: str = "") -> str:
    """ubereats.com/store/<slug>/<id>; the id is the UUID's bytes in base64url.
    Opens the Uber Eats app on the phone when it's installed."""
    try:
        sid = base64.urlsafe_b64encode(uuid.UUID(store_uuid).bytes).decode().rstrip("=")
    except ValueError:
        return ""
    return f"https://www.ubereats.com/store/{slug or 'store'}/{sid}"


def _product(item: dict, info: dict) -> dict:
    """One menu item as a products-card entry."""
    badge = "Sold out" if item.get("soldOut") else ""
    return {"name": item["title"], "price": _money(item["price"]),
            "image": item.get("imageURL", ""), "detail": item.get("description", "")[:90],
            "badge": badge, "url": _store_url(info.get("uuid", ""), info.get("slug", ""))}


def _menu_items(store: dict) -> list[dict]:
    """Flatten a getStoreV1 menu into unique items with their section."""
    items, seen = [], set()
    sections = {s.get("uuid"): s.get("title") for s in store.get("sections") or []}
    for sec_uuid, blocks in (store.get("catalogSectionsMap") or {}).items():
        for block in blocks or []:
            payload = (block.get("payload") or {}).get("standardItemsPayload") or {}
            heading = (payload.get("title") or {}).get("text") or sections.get(sec_uuid) or ""
            for it in payload.get("catalogItems") or []:
                if it.get("uuid") in seen:
                    continue
                seen.add(it.get("uuid"))
                items.append({
                    "uuid": it.get("uuid"), "title": it.get("title", ""),
                    "price": it.get("price") or 0,
                    "description": it.get("itemDescription") or "",
                    "section": heading,
                    "sectionUuid": it.get("sectionUuid") or sec_uuid,
                    "subsectionUuid": it.get("subsectionUuid") or "",
                    "imageURL": it.get("imageUrl") or "",
                    "options": bool(it.get("hasCustomizations")),
                    "soldOut": bool(it.get("isSoldOut")),
                })
    return items


async def _menu(store_uuid: str) -> tuple[dict, list[dict]]:
    cached = _r.get(f"ubereats:menu:{store_uuid}")
    if cached:
        c = json.loads(cached)
        return c["store"], c["items"]
    store = await _store(store_uuid)
    info = {"uuid": store_uuid, "title": store.get("title", ""),
            "slug": store.get("slug") or "",
            # isOpen stays true while a store takes SCHEDULED orders; a
            # closedMessage ("Opens at 12:00 PM") is what says it's shut now.
            "isOpen": bool(store.get("isOpen")) and not store.get("closedMessage"),
            "closedMessage": store.get("closedMessage") or "",
            "eta": (store.get("etaRange") or {}).get("text", ""),
            "rating": (store.get("rating") or {}).get("ratingValue")}
    items = _menu_items(store)
    _r.set(f"ubereats:menu:{store_uuid}", json.dumps({"store": info, "items": items}), ex=_MENU_TTL)
    return info, items


async def _suggest(query: str) -> list[dict]:
    """Restaurants whose NAME matches -- what the search box shows as you type.
    getSearchFeedV1 ranks by cuisine/promotions and ignores names entirely."""
    data = await _api("getSearchSuggestionsV1", {"userQuery": query, "date": "",
                                                "startTime": 0, "endTime": 0, "vertical": "ALL"})
    out = []
    for sug in data if isinstance(data, list) else []:
        st = sug.get("store") or {}
        if sug.get("type") != "store" or not st.get("uuid"):
            continue
        cats = [c for c in st.get("categories") or [] if c]
        _r.set(f"ubereats:store_name:{st['uuid']}", st.get("title", ""), ex=_STORE_NAME_TTL)
        out.append({"uuid": st["uuid"], "title": st.get("title", ""),
                    "eta": "closed now" if "Closed" in cats else "",
                    "fee": ", ".join(c for c in cats if c != "Closed"), "rating": ""})
    return out


async def _search(query: str) -> list[dict]:
    data = await _api("getSearchFeedV1", {
        "userQuery": query, "date": "", "startTime": 0, "endTime": 0, "sortAndFilters": [],
        "vertical": "", "searchSource": "", "displayType": "SEARCH_RESULTS", "searchType": "",
        "keyName": "", "cacheKey": "", "recaptchaToken": ""})
    out = []
    for f in data.get("feedItems") or []:
        s = f.get("store") or (f.get("miniStoreWithItems") or {}).get("store") or {}
        if not s.get("storeUuid"):
            continue
        meta = s.get("meta") or []
        eta = next((m.get("accessibilityText") for m in meta if m.get("badgeType") == "ETD"), "")
        fee = next((m.get("text") for m in meta if "Fee" in (m.get("text") or "")), "")
        title = (s.get("title") or {}).get("text", "")
        _r.set(f"ubereats:store_name:{s['storeUuid']}", title, ex=_STORE_NAME_TTL)
        out.append({"uuid": s["storeUuid"], "title": title, "eta": eta, "fee": fee,
                    "rating": (s.get("rating") or {}).get("text", "")})
    return out


async def _resolve_store(store: str) -> tuple[str, str]:
    """(uuid, title) for a store id or name."""
    store = (store or "").strip()
    if _UUID_RE.match(store.lower()):
        return store.lower(), _r.get(f"ubereats:store_name:{store.lower()}") or ""
    results = await _suggest(store)
    if _best_match(store, [r["title"] for r in results]) is None:
        results += await _search(store)
    if not results:
        raise UberError(f"No restaurant on Uber Eats matches '{store}'.")
    idx = _best_match(store, [r["title"] for r in results])
    if idx is None:
        names = ", ".join(r["title"] for r in results[:5])
        raise UberError(f"No restaurant named '{store}' near you. Closest results: {names}.")
    return results[idx]["uuid"], results[idx]["title"]


async def _drafts() -> list[dict]:
    data = await _api("getDraftOrdersByEaterUuidV1", {"currencyCode": "USD"})
    return data.get("draftOrders") or []


async def _store_name(store_uuid: str) -> str:
    name = _r.get(f"ubereats:store_name:{store_uuid}")
    if name:
        return name
    try:
        return (await _store(store_uuid)).get("title") or store_uuid[:8]
    except UberError:
        return store_uuid[:8]


def _line_text(item: dict) -> str:
    picks = [v.get("title", "") for vals in (item.get("customizations") or {}).values()
             for v in vals]
    qty = item.get("quantity") or 1
    txt = f"{qty}× {item.get('title', '')}"
    return txt + (f" ({', '.join(p for p in picks if p)})" if picks else "")


async def _cart_totals(draft_uuid: str) -> tuple[str, str]:
    try:
        data = await _api("getCheckoutPresentationV1", {
            "payloadTypes": ["subtotal", "total"], "draftOrderUUID": draft_uuid})
        p = data.get("checkoutPayloads") or {}
        sub = ((p.get("subtotal") or {}).get("subtotal") or {}).get("formattedValue", "")
        tot = ((p.get("total") or {}).get("total") or {}).get("formattedValue", "")
        return sub, tot
    except UberError:
        return "", ""


def _build_customizations(groups: list[dict], wanted: list[str], prefix: str = ""
                          ) -> tuple[dict, list[str], set[str]]:
    """Uber's cart format: {"<group>+<i>": [option...]}, nested groups keyed
    "<parentKey>,<optionUuid>,<childGroup>+<j>". Picks options the user named,
    else the menu defaults. Returns (customizations, problems, used wanted)."""
    out, problems, used = {}, [], set()
    for gi, g in enumerate(groups or []):
        key = f"{prefix}{g.get('uuid')}+{gi}"
        opts = g.get("options") or []
        picks = []
        for o in opts:
            ow = " ".join(_words(o.get("title", "")))
            for w in wanted:
                ww = " ".join(_words(w))
                if ww and (ww == ow or ww in ow or (ow and ow in ww)):
                    picks.append(o)
                    used.add(w)
                    break
        if not picks:
            picks = [o for o in opts if (o.get("defaultQuantity") or 0) > 0]
        lo, hi = g.get("minPermitted") or 0, g.get("maxPermitted") or 0
        if hi:
            picks = picks[:hi]
        if len(picks) < lo:
            choices = "; ".join(f"{o.get('title')}"
                                + (f" +{_money(o.get('price'))}" if o.get("price") else "")
                                for o in opts[:15])
            problems.append(f"'{g.get('title')}' needs {lo} choice(s): {choices}")
            continue
        if picks:
            out[key] = [{"uuid": o.get("uuid"), "price": o.get("price") or 0,
                         "quantity": max(o.get("defaultQuantity") or 0, 1),
                         "title": o.get("title", ""),
                         "defaultQuantity": o.get("defaultQuantity") or 0,
                         "customizationMeta": {"title": g.get("title", ""), "isPickOne": False}}
                        for o in picks]
            for o in picks:
                child = o.get("childCustomizationList") or []
                if child:
                    sub, p2, u2 = _build_customizations(child, wanted, f"{key},{o.get('uuid')},")
                    out.update(sub)
                    problems += p2
                    used |= u2
    return out, problems, used


async def _item_detail(store_uuid: str, item: dict) -> dict:
    return await _api("getMenuItemV1", {
        "itemRequestType": "ITEM", "storeUuid": store_uuid,
        "sectionUuid": item["sectionUuid"], "subsectionUuid": item["subsectionUuid"],
        "menuItemUuid": item["uuid"], "diningMode": "DELIVERY", "isEditFlow": False,
        "cbType": "EATER_ENDORSED", "includeCheaperAlternatives": False,
        "contextReferences": [{"type": "GROUP_ITEMS", "payload": {
            "type": "groupItemsContextReferencePayload",
            "groupItemsContextReferencePayload": {}}, "pageContext": "STORE"}]})


async def _find_item(store_uuid: str, name: str) -> tuple[dict, dict]:
    info, items = await _menu(store_uuid)
    idx = _best_match(name, [i["title"] for i in items])
    if idx is None:
        raise UberError(f"'{name}' isn't on {info['title']}'s menu. "
                        "Use ubereats_menu to see the exact item names.")
    return info, items[idx]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
async def ubereats_search(query: str) -> str:
    """Search Uber Eats for restaurants that deliver to the user's saved address.

    Use for "what's on Uber Eats", "find sushi near me", or to find a specific
    restaurant before reading its menu. Returns restaurant names, ratings,
    delivery time and fee, and a store id for the other ubereats_* tools.

    Args:
        query: a cuisine, dish or restaurant name, e.g. "sushi", "Blue Sushi".
    """
    try:
        named = await _suggest(query)
        ids = {r["uuid"] for r in named}
        results = named + [r for r in await _search(query) if r["uuid"] not in ids]
    except UberError as e:
        return str(e)
    if not results:
        return f"Uber Eats found nothing for '{query}'."
    lines = []
    for r in results[:12]:
        bits = [b for b in (f"rated {r['rating']}" if r["rating"] else "", r["eta"], r["fee"]) if b]
        lines.append(f"- {r['title']} (store id {r['uuid']})" + (": " + ", ".join(bits) if bits else ""))
    return f"Uber Eats results for '{query}':\n" + "\n".join(lines)


@tool
async def ubereats_menu(store: str, filter: str = "") -> str:
    """Read a restaurant's Uber Eats menu: item names, prices, descriptions,
    whether the item has options, and whether the store is open now.

    Use before adding to a cart, to get exact item names, or to answer "what
    can I order at X" (for nutrition goals, read descriptions and pick dishes).

    Args:
        store: the restaurant's name ("Blue Sushi Sake Grill") or store id
            from ubereats_search.
        filter: optional words to narrow the menu, e.g. "roll", "chicken".
    """
    try:
        uid, _ = await _resolve_store(store)
        info, items = await _menu(uid)
    except UberError as e:
        return str(e)
    fw = set(_words(filter))
    if fw:
        items = [i for i in items
                 if fw & set(_words(i["title"] + " " + i["description"] + " " + i["section"]))]
    head = f"{info['title']} (store id {uid})"
    head += (f" is open, delivery {info['eta']}." if info.get("isOpen")
             else f" is CLOSED now ({info.get('closedMessage') or 'no hours given'}); "
                  "a cart can still be built for later.")
    lines, last = [], None
    for i in items[:80]:
        if i["section"] != last:
            lines.append(f"\n{i['section'] or 'Menu'}:")
            last = i["section"]
        extra = (" [has options]" if i["options"] else "") + (" [SOLD OUT]" if i["soldOut"] else "")
        desc = f": {i['description'][:110]}" if i["description"] else ""
        lines.append(f"- {i['title']} {_money(i['price'])}{extra}{desc}")
    more = f"\n({len(items) - 80} more items; use filter to narrow.)" if len(items) > 80 else ""
    if not lines:
        return head + f"\nNothing matches '{filter}'."
    # A filtered menu is a short list worth seeing: photos and prices as a card.
    card = ""
    if fw:
        info.setdefault("uuid", uid)
        shown = [i for i in items if i.get("imageURL")][:8] or items[:8]
        card = cards.products(f"{info['title']}: {filter}", [_product(i, info) for i in shown],
                              subtitle=info.get("eta") if info.get("isOpen")
                              else (info.get("closedMessage") or "Closed now"))
    return head + "\n" + "\n".join(lines) + more + card


@tool
async def ubereats_item_options(store: str, item: str) -> str:
    """List an Uber Eats item's option groups (size, sauce, protein, add-ons),
    which are required, and their prices.

    Use when ubereats_menu shows [has options], before ubereats_add_to_cart,
    so you can pass the user's choices (or ask them for a required one).

    Args:
        store: restaurant name or store id.
        item: the menu item's name.
    """
    try:
        uid, _ = await _resolve_store(store)
        info, it = await _find_item(uid, item)
        detail = await _item_detail(uid, it)
    except UberError as e:
        return str(e)
    groups = detail.get("customizationsList") or []
    if not groups:
        return f"{it['title']} ({_money(it['price'])}) has no options."

    def fmt(gs, depth=0):
        out = []
        for g in gs:
            lo, hi = g.get("minPermitted") or 0, g.get("maxPermitted") or 0
            rule = f"required, pick {lo}" if lo else f"optional, up to {hi}" if hi else "optional"
            out.append("  " * depth + f"- {g.get('title')} ({rule}):")
            for o in g.get("options") or []:
                price = f" +{_money(o.get('price'))}" if o.get("price") else ""
                default = " (default)" if (o.get("defaultQuantity") or 0) > 0 else ""
                out.append("  " * depth + f"    {o.get('title')}{price}{default}")
                if o.get("childCustomizationList"):
                    out += fmt(o["childCustomizationList"], depth + 2)
        return out
    return f"{it['title']} at {info['title']}, {_money(it['price'])}:\n" + "\n".join(fmt(groups))


@tool
async def ubereats_add_to_cart(store: str, item: str, quantity: int = 1,
                               options: str = "", instructions: str = "") -> str:
    """Add an item to the user's Uber Eats cart for that restaurant. Does NOT
    order: the user checks out in the Uber Eats app.

    Use when the user asks to put something in their Uber Eats cart. For items
    with options, pass the user's choices; required groups with no choice fall
    back to the menu's default, and if there is none this returns the choices
    so you can ask. Confirm with the user before adding a lot of items.

    Args:
        store: restaurant name or store id.
        item: the menu item's name, as on the menu.
        quantity: how many.
        options: comma-separated option names, e.g. "Brown rice, Spicy mayo".
        instructions: special instructions for the restaurant, if any.
    """
    quantity = max(1, min(int(quantity or 1), 20))
    wanted = [w.strip() for w in (options or "").split(",") if w.strip()]
    try:
        uid, _ = await _resolve_store(store)
        info, it = await _find_item(uid, item)
        if it["soldOut"]:
            return f"{it['title']} is sold out at {info['title']} right now."
        custom = {}
        if it["options"] or wanted:
            groups = (await _item_detail(uid, it)).get("customizationsList") or []
            custom, problems, used = _build_customizations(groups, wanted)
            if problems:
                return (f"Can't add {it['title']} yet. Ask the user: " + " | ".join(problems))
            unused = [w for w in wanted if w not in used]
            if unused:
                return (f"{it['title']} has no option called {', '.join(unused)}. "
                        "Use ubereats_item_options to see the real choices.")
        line = {"uuid": it["uuid"], "shoppingCartItemUuid": str(uuid.uuid4()),
                "storeUuid": uid, "sectionUuid": it["sectionUuid"],
                "subsectionUuid": it["subsectionUuid"], "price": it["price"],
                "title": it["title"], "quantity": quantity, "customizations": custom,
                "imageURL": it["imageURL"], "specialInstructions": instructions or ""}
        drafts = await _drafts()
        mine = next((d for d in drafts if d.get("storeUuid") == uid), None)
        if mine:
            await _api("addItemsToDraftOrderV2", {
                "items": [line], "cartUUID": mine["shoppingCart"]["cartUuid"],
                "draftOrderUUID": mine["uuid"], "storeUUID": uid,
                "actionMeta": {"isQuickAdd": False}, "shouldUpdateDraftOrderMetadata": True,
                "isNewCartAbstraction": True, "locationType": "PHYSICAL"})
            draft_uuid = mine["uuid"]
        else:
            kinds = Counter(d.get("interactionType") for d in drafts if d.get("interactionType"))
            made = await _api("createDraftOrderV2", {
                "removeAdapters": True, "isMulticart": True, "useCredits": True,
                "extraPaymentProfiles": [],
                "promotionOptions": {"autoApplyPromotionUUIDs": [],
                                     "selectedPromotionInstanceUUIDs": [],
                                     "skipApplyingPromotion": False},
                "deliveryTime": {"asap": True}, "deliveryType": "ASAP", "currencyCode": "USD",
                "interactionType": (kinds.most_common(1)[0][0] if kinds else "door_to_door"),
                "checkMultipleDraftOrdersCap": True, "actionMeta": {"isQuickAdd": False},
                "analyticsRelevantData": {"profileSource": ""}, "businessDetails": {},
                "shoppingCartItems": [line]})
            draft_uuid = ((made.get("draftOrder") or made).get("uuid")) or ""
        # Verify against the cart Uber now holds, not the request we sent.
        after = next((d for d in await _drafts() if d.get("storeUuid") == uid), None)
        lines = (after or {}).get("shoppingCart", {}).get("items") or []
        landed = next((l for l in lines
                       if l.get("shoppingCartItemUuid") == line["shoppingCartItemUuid"]), None)
        if not landed:
            return f"Uber Eats accepted the request but {it['title']} isn't in the cart. Nothing to undo."
        sub, tot = await _cart_totals(after["uuid"] or draft_uuid)
    except UberError as e:
        return str(e)
    picks = ", ".join(v["title"] for vals in custom.values() for v in vals)
    msg = (f"Added {_line_text(landed)} to the {info['title']} cart on Uber Eats"
           + (f" (with {picks})" if picks and "(" not in _line_text(landed) else "") + ". "
           f"Cart now: {'; '.join(_line_text(l) for l in lines)}."
           + (f" Subtotal {sub}, total {tot} with fees." if sub else ""))
    if not info.get("isOpen"):
        msg += f" Note: {info['title']} is closed now ({info.get('closedMessage')})."
    info.setdefault("uuid", uid)
    card = cards.products(f"Added to your {info['title']} cart",
                          [{**_product(it, info), "detail": _line_text(landed),
                            "badge": f"x{quantity}" if quantity > 1 else ""}],
                          footer=f"Subtotal {sub} · Total {tot}" if sub else "")
    return msg + " The user checks out in the Uber Eats app; Jarvis never places the order." + card


@tool
async def ubereats_view_cart(store: str = "") -> str:
    """Show the user's Uber Eats carts: items, options and totals with fees.

    Uber keeps one cart per restaurant. Use for "what's in my Uber Eats cart".

    Args:
        store: a restaurant name or store id to show just that cart; empty = all.
    """
    try:
        drafts = [d for d in await _drafts() if d.get("shoppingCart", {}).get("items")]
        if store.strip():
            uid, title = await _resolve_store(store)
            drafts = [d for d in drafts if d.get("storeUuid") == uid]
            if not drafts:
                return f"There's no Uber Eats cart for {title or store}."
        if not drafts:
            return "The user's Uber Eats carts are empty."
        rows, text = [], []
        for d in drafts:
            name = await _store_name(d["storeUuid"])
            sub, tot = await _cart_totals(d["uuid"])
            items = "; ".join(_line_text(l) for l in d["shoppingCart"]["items"])
            rows.append([name, items, sub or "?", tot or "?"])
            text.append(f"- {name}: {items} (subtotal {sub or '?'}, total {tot or '?'})")
    except UberError as e:
        return str(e)
    card = cards.attach({"type": "table", "title": "Uber Eats carts",
                         "columns": ["Restaurant", "Items", "Subtotal", "Total"],
                         "rows": rows, "footer": "Check out in the Uber Eats app"})
    return f"{len(drafts)} Uber Eats cart(s):\n" + "\n".join(text) + card


@tool
async def ubereats_remove_from_cart(store: str, items: str) -> str:
    """Remove items from the user's Uber Eats cart for one restaurant.

    Use for "take the fries out of my Uber Eats cart" or "clear my Chipotle
    cart". Emptying a cart removes it.

    Args:
        store: restaurant name or store id.
        items: comma-separated item names to remove, or "all" to empty that cart.
    """
    try:
        uid, title = await _resolve_store(store)
        draft = next((d for d in await _drafts() if d.get("storeUuid") == uid), None)
        if not draft:
            return f"There's no Uber Eats cart for {title or store}."
        lines = draft["shoppingCart"]["items"]
        if not (items or "").strip():
            return "Say which items to remove, or items=\"all\" to empty the cart."
        if items.strip().lower() in ("all", "everything"):
            targets = lines
        else:
            targets, missing = [], []
            for want in [w.strip() for w in items.split(",") if w.strip()]:
                idx = _best_match(want, [l.get("title", "") for l in lines])
                if idx is None:
                    missing.append(want)
                else:
                    targets.append(lines[idx])
            if missing and not targets:
                return (f"None of those are in the {title} cart. It has: "
                        + "; ".join(_line_text(l) for l in lines))
        await _api("removeItemsFromDraftOrderV2", {
            "draftOrderUUID": draft["uuid"], "cartUUID": draft["shoppingCart"]["cartUuid"],
            "storeUUID": uid,
            "shoppingCartItemUUIDs": [l["shoppingCartItemUuid"] for l in targets]})
        after = next((d for d in await _drafts() if d.get("storeUuid") == uid), None)
        left = (after or {}).get("shoppingCart", {}).get("items") or []
    except UberError as e:
        return str(e)
    gone = "; ".join(_line_text(l) for l in targets)
    rest = ("The cart is now empty." if not left
            else "Still in the cart: " + "; ".join(_line_text(l) for l in left) + ".")
    return f"Removed {gone} from the {title} Uber Eats cart. {rest}"
