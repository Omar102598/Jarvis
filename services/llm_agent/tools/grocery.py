"""Grocery agent tools — control the smart grocery agent via Jarvis conversation."""

import json
import os

import paho.mqtt.publish as mqtt_publish
import redis
from langchain_core.tools import tool

_r = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    decode_responses=True,
)
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

_PENDING_KEY = "grocery:pending_order"


@tool
def get_grocery_status() -> str:
    """Check the status of the current or pending grocery order.

    Returns the list of items, total cost, selected stores, cart links,
    and current status (pending_approval / placed / none).
    """
    raw = _r.get(_PENDING_KEY)
    if not raw:
        return (
            "No pending grocery order found, sir. "
            "The grocery agent runs every Monday morning, or say "
            "'run grocery agent' to trigger it now."
        )

    try:
        order = json.loads(raw)
    except Exception:
        return "Grocery order data appears corrupted. Try triggering a fresh run."

    status     = order.get("status", "unknown")
    total      = order.get("total_usd", 0)
    budget     = order.get("budget_usd", 150)
    stores     = ", ".join(order.get("stores", []))
    items      = order.get("items", [])
    cart_links = order.get("cart_links", {})
    created    = order.get("created_at", "")[:10]

    added   = [i for i in items if i.get("status") == "added"]
    failed  = [i for i in items if i.get("status") in ("not_found", "error")]

    if status == "pending_approval":
        lines = [
            f"Pending grocery order from {created} — awaiting your approval, sir.",
            f"Stores: {stores}",
            f"Items: {len(added)} added, {len(failed)} not found",
            f"Total: ${total:.2f} of ${budget:.0f} budget",
            "",
            "Cart links:",
        ]
        for store, link in cart_links.items():
            lines.append(f"  {store}: {link}")
        lines.append("")
        lines.append("Say 'approve grocery order' to proceed to checkout.")
        return "\n".join(lines)

    elif status == "placed":
        placed = order.get("placed_at", "")[:10]
        return (
            f"Order placed on {placed}, sir. "
            f"${total:.2f} across {stores}."
        )

    return f"Grocery order status: {status} (created: {created})."


@tool
def approve_grocery_order() -> str:
    """Approve the pending grocery order and trigger checkout at the selected stores.

    The visible browser will open each store's cart and proceed to checkout.
    You will need to confirm payment details in the browser window.
    """
    raw = _r.get(_PENDING_KEY)
    if not raw:
        return (
            "No pending grocery order to approve, sir. "
            "Run the grocery agent first."
        )

    try:
        order = json.loads(raw)
    except Exception:
        return "Grocery order data appears corrupted."

    if order.get("status") != "pending_approval":
        status = order.get("status", "unknown")
        return f"Order is already in status '{status}', sir. No action needed."

    stores = ", ".join(order.get("stores", []))
    total  = order.get("total_usd", 0)

    try:
        mqtt_publish.single(
            "jarvis/agents/grocery/trigger",
            json.dumps({"params": {"action": "checkout"}}),
            hostname=MQTT_HOST,
            port=MQTT_PORT,
        )
    except Exception as exc:
        return f"Failed to trigger checkout: {exc}"

    return (
        f"Approved, sir. Initiating checkout at {stores} — "
        f"total ${total:.2f}. "
        f"The visible browser will open each store's cart shortly. "
        f"Please complete any payment confirmation steps in the browser."
    )


@tool
def trigger_grocery_run() -> str:
    """Manually trigger the smart grocery agent to run a full weekly shop now.

    The agent will: fetch your Amazon order history, generate a fitness-aware
    grocery list based on your profile, compare prices across Target, HEB,
    Amazon Fresh, and Whole Foods, build carts, and send you a report.
    """
    try:
        mqtt_publish.single(
            "jarvis/agents/grocery/trigger",
            json.dumps({}),
            hostname=MQTT_HOST,
            port=MQTT_PORT,
        )
    except Exception as exc:
        return f"Failed to trigger grocery agent: {exc}"

    return (
        "Grocery agent triggered, sir. I'll have your weekly cart ready "
        "and send a report via iMessage and the Jarvis app. "
        "This typically takes 5-10 minutes."
    )


_USUAL_ORDER_KEY = "grocery:usual_order"
_FAVORITES_KEY = "grocery:favorites"
_STORE_NAME_TO_KEY = {
    "amazon fresh": "amazon", "whole foods": "whole_foods",
    "target": "target", "heb": "heb",
}


def _norm_item(item: str) -> str:
    import re
    return re.sub(r"\s+", " ", str(item).strip().lower())


@tool
def pin_favorite_product(item: str, action: str = "pin") -> str:
    """Pin, unpin, or list favorite grocery products.

    A pinned favorite locks a grocery-list item to the EXACT product from the
    user's last order — the resolver always uses it and never substitutes.
    Use when the user says "pin that chicken", "always buy this exact yogurt",
    "unpin the salsa", or "what favorites do I have pinned?".

    Args:
        item: The grocery item name as it appeared in the order (e.g.
            'chicken tenderloins'). Ignored for action='list'.
        action: 'pin' (lock the product matched in the last order),
            'unpin' (remove), or 'list' (show all pinned favorites).
    """
    action = action.strip().lower()

    if action == "list":
        favs = _r.hgetall(_FAVORITES_KEY)
        if not favs:
            return "No favorites pinned yet, sir. Pin one with 'pin the <item> from my order'."
        lines = [f"{len(favs)} pinned favorite(s):"]
        for field, raw in sorted(favs.items()):
            store, name = field.split(":", 1)
            try:
                title = json.loads(raw).get("title", "")
            except Exception:
                title = ""
            lines.append(f"  • {name} @ {store} → {title}")
        return "\n".join(lines)

    if not item.strip():
        return "Which item should I work with, sir?"
    norm = _norm_item(item)

    if action == "unpin":
        fields = [f for f in _r.hkeys(_FAVORITES_KEY) if f.split(":", 1)[1] == norm]
        if not fields:
            return f"No pinned favorite found for '{item}'."
        _r.hdel(_FAVORITES_KEY, *fields)
        return f"Unpinned '{item}' ({len(fields)} store entr{'ies' if len(fields) > 1 else 'y'})."

    # action == "pin": copy the resolved product from the last order
    raw = _r.get(_PENDING_KEY)
    if not raw:
        return "No recent grocery order to pin from, sir. Run the grocery agent first."
    try:
        order = json.loads(raw)
    except Exception:
        return "The last order data appears corrupted."

    match = next(
        (i for i in order.get("items", [])
         if _norm_item(i.get("item", "")) == norm and i.get("url")),
        None,
    ) or next(
        (i for i in order.get("items", [])
         if norm in _norm_item(i.get("item", "") + " " + (i.get("product") or ""))
         and i.get("url")),
        None,
    )
    if not match:
        options = ", ".join(i.get("item", "?") for i in order.get("items", [])[:12])
        return f"Couldn't find '{item}' in the last order. Items were: {options}"

    store_key = match.get("store_key") or _STORE_NAME_TO_KEY.get(
        (match.get("store") or "").lower(), ""
    )
    if not store_key:
        return f"Couldn't determine the store for '{item}' — pin not saved."

    _r.hset(
        _FAVORITES_KEY,
        f"{store_key}:{norm}",
        json.dumps({"href": match["url"], "title": match.get("product", "")}),
    )
    return (
        f"Pinned, sir: '{item}' is now locked to {match.get('product', 'that product')} "
        f"at {match.get('store', store_key)}. The resolver will always use this exact "
        f"product and never substitute it."
    )


@tool
def learn_fresh_cart(store: str = "amazon") -> str:
    """Scan the user's current store cart(s) and learn them as their usual order.

    Use when the user says "learn my Fresh cart", "scan my Target cart",
    "memorize what's in my carts", etc. The user builds the cart themselves;
    the grocery agent scrapes it, saves the items as their habitual staples,
    and every future weekly grocery list is built around them. Safe to run
    repeatedly — items seen across multiple scans rank higher.

    Args:
        store: Which cart to scan — 'amazon' (Fresh, default), 'whole_foods',
            'target', 'heb', or 'all' for every store at once.
    """
    store = store.strip().lower() or "amazon"
    if store not in ("amazon", "whole_foods", "target", "heb", "all"):
        return f"Unknown store '{store}'. Options: amazon, whole_foods, target, heb, all."
    try:
        mqtt_publish.single(
            "jarvis/agents/grocery/trigger",
            json.dumps({"params": {"action": "learn_cart", "stores": store}}),
            hostname=MQTT_HOST,
            port=MQTT_PORT,
        )
    except Exception as exc:
        return f"Failed to trigger the cart scan: {exc}"

    which = "all store carts" if store == "all" else f"your {store.replace('_', ' ')} cart"
    return (
        f"On it, sir — Remy is scanning {which} now. The items will be saved as "
        "your usual order and future grocery lists will be built around them. "
        "Ask for the grocery agent's report in a minute."
    )


@tool
def get_usual_order() -> str:
    """Show the user's learned usual grocery order (from their Amazon Fresh cart scans).

    Use when the user asks "what's my usual order?", "what staples do you have
    on file?", or before discussing changes to their regular groceries.
    """
    raw = _r.get(_USUAL_ORDER_KEY)
    if not raw:
        return (
            "No usual order on file yet, sir. Build a cart in Amazon Fresh with "
            "your regular items, then say 'learn my Fresh cart'."
        )
    try:
        data = json.loads(raw)
        items = data.get("items", []) or []
    except Exception:
        return "The usual-order data appears corrupted — try re-learning the cart."

    if not items:
        return "The usual order on file is empty — say 'learn my Fresh cart' to populate it."

    updated = (data.get("updated") or "")[:10]
    lines = [f"Usual order on file ({len(items)} items, last learned {updated}):"]
    for i in items[:40]:
        qty = i.get("qty", 1)
        seen = int(i.get("count", 1))
        marker = f" ×{qty}" if qty and int(qty) > 1 else ""
        freq = f" (seen {seen}×)" if seen > 1 else ""
        lines.append(f"  • {i.get('name') or i.get('title')}{marker}{freq}")
    return "\n".join(lines)


_MEAL_PLAN_KEY = "grocery:meal_plan"


@tool
def suggest_meals(ingredients: str = "") -> str:
    """Suggest meals from the groceries the user actually has on hand.

    Use for requests like "what can I make with chicken, rice and broccoli?",
    "what meals can I make from my groceries?", or "what's for dinner?".

    Args:
        ingredients: Optional comma-separated ingredients the user named
            (e.g. "chicken, rice, broccoli"). Leave empty to use their whole
            grocery inventory.

    Returns the user's current grocery inventory plus this week's planned meals as
    grounding. Compose specific, realistic meal ideas for the user from ONLY those
    groceries (note any requested item they don't actually have).
    """
    meal_plan: dict = {}
    raw = _r.get(_MEAL_PLAN_KEY)
    if raw:
        try:
            meal_plan = json.loads(raw)
        except Exception:
            meal_plan = {}

    groceries = meal_plan.get("groceries", []) or []
    # Fall back to the current order's added items if no stored meal-plan inventory
    if not groceries:
        raw2 = _r.get(_PENDING_KEY)
        if raw2:
            try:
                order = json.loads(raw2)
                groceries = [
                    (i.get("product") or i.get("item"))
                    for i in order.get("items", [])
                    if i.get("status") == "added"
                ]
            except Exception:
                pass

    # The learned usual order (Fresh-cart scans) = staples the user habitually
    # keeps on hand — include them so dinner ideas reflect the real kitchen.
    usual: list[str] = []
    raw3 = _r.get(_USUAL_ORDER_KEY)
    if raw3:
        try:
            usual = [
                i.get("name") or i.get("title", "")
                for i in json.loads(raw3).get("items", [])[:30]
            ]
        except Exception:
            pass

    if not groceries and not usual:
        return (
            "I don't have a record of your current groceries, sir. Run the grocery "
            "agent (say 'run grocery agent') or scan your Fresh cart ('learn my "
            "Fresh cart') and I'll know what's in your kitchen."
        )

    wanted = [w.strip() for w in ingredients.replace(" and ", ",").split(",") if w.strip()]
    lines: list[str] = []

    if wanted:
        low = [g.lower() for g in (groceries + usual) if g]
        have = [w for w in wanted if any(w.lower() in g for g in low)]
        missing = [w for w in wanted if w not in have]
        lines.append(
            "Requested — in your groceries: "
            + (", ".join(have) if have else "none")
            + (f"; NOT in your groceries: {', '.join(missing)}" if missing else "")
        )

    if groceries:
        lines.append("Current groceries (this week's order): " + ", ".join(g for g in groceries if g))
    if usual:
        lines.append("Usual staples (learned from their cart — likely on hand): "
                     + ", ".join(u for u in usual if u))

    meals = meal_plan.get("meals", []) or []
    if meals:
        lines.append("")
        lines.append("Meals already planned from this shop:")
        for m in meals:
            ings = ", ".join((m.get("ingredients") or [])[:6])
            lines.append(f"  • {m.get('name','')}" + (f" ({ings})" if ings else ""))

    lines.append("")
    focus = f", focusing on {', '.join(wanted)}" if wanted else ""
    lines.append(
        f"Now suggest specific meals the user can make from ONLY the groceries above{focus}. "
        "Give 3-5 concrete meals with the ingredients each uses."
    )
    return "\n".join(lines)
