"""Grocery agent tools — control the smart grocery agent via Jarvis conversation."""

import json
import os

import paho.mqtt.publish as mqtt_publish
import redis
from langchain_core.tools import tool

from .dispatch import ACTIVE_ROOM   # so Remy announces his result in the room that asked

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
        flags = [] if order.get("qty_confirmed") else (order.get("qty_flags") or [])
        if flags:
            lines.append("")
            lines.append(f"⚠ {len(flags)} quantity warning(s) — above the user's usual, need confirmation:")
            for f in flags:
                lines.append(f"  • {f.get('product')} @ {f.get('store')}: "
                             f"cart {f.get('qty')}, usual {f.get('usual_qty')}")
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
def approve_grocery_order(confirm_quantities: bool = False) -> str:
    """Approve the pending grocery order and trigger checkout at the selected stores.

    If the order has quantity warnings (items above what the user usually
    orders), this REFUSES and lists them. Read them to the user; only after
    they explicitly confirm those quantities call again with
    confirm_quantities=True. Never set it on your own.

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

    flags = [] if order.get("qty_confirmed") else (order.get("qty_flags") or [])
    if flags and not confirm_quantities:
        lines = [
            f"  • {f.get('product')} @ {f.get('store')}: cart has {f.get('qty')}, "
            f"usual {f.get('usual_qty')}" for f in flags]
        return ("Not approved yet, sir — these quantities are above your usual:\n"
                + "\n".join(lines)
                + "\nAsk the user to confirm them (or have Remy fix them). Only then "
                  "call approve_grocery_order(confirm_quantities=True).")

    try:
        mqtt_publish.single(
            "jarvis/agents/grocery/trigger",
            json.dumps({"params": {"action": "checkout",
                                   "confirm_quantities": bool(confirm_quantities)}}),
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
def trigger_grocery_run(stores: str = "", budget: float = 0, notes: str = "") -> str:
    """Start Remy, the grocery agent, to build grocery carts now. This is the FULL
    weekly shop (plans a week of meals); he reads the cart first and only adds
    what the meals still need. When the user names specific items, use
    add_grocery_items instead. This IS "use Remy" / "build me a grocery
    cart" -- never hand groceries to
    spawn_task, which has no store logins.

    Remy plans a week of meals from the user's nutrition profile and recomp
    targets, finds each item at the stores, and ADDS it to the cart in the
    Jarvis browser (signed in to H-E-B, Target, Amazon Fresh, Whole Foods). It
    never removes what's already in the cart and never checks out; the user
    approves the order separately (approve_grocery_order).

    Args:
        stores: Limit to these stores, comma-separated: 'heb', 'target',
            'amazon', 'whole_foods'. Empty = compare all and pick the best 1-2.
        budget: Spending cap in dollars for this shop. 0 = the profile's
            weekly budget.
        notes: Anything else the user asked for, in their words ("use what's
            already in the cart as a reference", "no fish this week").
    """
    already = _r.get("browser:busy") == "remy"
    params = {"notify_room": ACTIVE_ROOM.get("room", "")}
    if stores.strip():
        params["stores"] = [x.strip() for x in stores.split(",") if x.strip()]
    if budget and budget > 0:
        params["budget"] = budget
    if notes.strip():
        params["notes"] = notes.strip()
    try:
        mqtt_publish.single(
            "jarvis/agents/grocery/trigger",
            json.dumps({"source": "llm", "params": params}),
            hostname=MQTT_HOST,
            port=MQTT_PORT,
        )
    except Exception as exc:
        return f"Failed to trigger grocery agent: {exc}"

    where = f" at {', '.join(params['stores'])}" if params.get("stores") else ""
    if already:
        return (f"Remy is already in the middle of a run, so this one is QUEUED{where}: "
                "it starts when the current run finishes and adds its items on top "
                "(it doesn't merge with or replace the current run). Tell the user it's "
                "queued, and that he'll report on both.")
    return (f"Remy is building the cart{where}, sir. He adds only (nothing removed, "
            "no checkout) and reports when done -- usually 5-10 minutes.")


@tool
def remove_grocery_items(items: str = "", stores: str = "all", added_by_remy: str = "") -> str:
    """Remove items from grocery carts -- any item, whoever added it (the
    user, Remy, or someone else). Use when the user asks to take something
    out of a cart or to empty a cart.

    Args:
        items: What to remove, comma-separated, in the user's words ("the
            poppi, chicken thighs"); Remy matches them to the exact cart
            lines. Use "all" ONLY when the user explicitly asks to empty the
            cart(s). Leave empty when using added_by_remy.
        stores: 'heb', 'target', 'amazon' (Fresh), 'whole_foods', comma-
            separated, or 'all' (default) to look in every cart.
        added_by_remy: To undo Remy's own shopping ("remove what Remy added",
            "take out today's run, keep my stuff"): 'today', 'last' (his most
            recent run only), or a date 'YYYY-MM-DD' (everything since then).
            Remy keeps a log of what he adds, so the user's own items stay.
    """
    since = added_by_remy.strip().lower()
    want = "all" if items.strip().lower() in ("all", "everything") else \
        [x.strip() for x in items.split(",") if x.strip()]
    if not want and not since:
        return "Which items should I remove?"
    st = stores.strip().lower() or "all"
    params = {"action": "remove", "items": want, "notify_room": ACTIVE_ROOM.get("room", ""),
              "stores": "all" if st == "all" else [x.strip() for x in st.split(",") if x.strip()]}
    if since:
        params["added_by_remy"] = since
        want = f"what he added ({since})"
    try:
        mqtt_publish.single("jarvis/agents/grocery/trigger",
                            json.dumps({"source": "llm", "params": params}),
                            hostname=MQTT_HOST, port=MQTT_PORT)
    except Exception as exc:
        return f"Failed to reach Remy: {exc}"
    what = want if isinstance(want, str) and want != "all" else \
        ("everything" if want == "all" else ", ".join(want))
    return (f"Remy is removing {what} from the {'carts' if st == 'all' else st + ' cart'}, sir. "
            "He'll report what he took out; ask for the grocery agent's report in a minute.")


@tool
def add_grocery_items(items: str, store: str = "heb", force: bool = False) -> str:
    """Add specific grocery items to a cart, and nothing else: no meal plan,
    no weekly shop. Use for "add eggs and 2 bags of spinach", "put basmati in
    my H-E-B cart", or topping up after the user edited the cart themselves.
    Prefer this over trigger_grocery_run whenever the user names the items.

    Remy reads the cart first and skips anything already in it (any brand or
    size of the same product), reporting what he skipped.

    Args:
        items: comma-separated, each optionally "qty x item" or "item:qty",
            e.g. "egg whites, 2 x spinach, basmati rice:1".
        store: 'heb' (default), 'target', 'amazon' (Fresh) or 'whole_foods'.
        force: True only when the user explicitly wants MORE of something that's
            already in the cart.
    """
    import re
    want = []
    for raw in items.split(","):
        e = raw.strip()
        if not e:
            continue
        qty = 1
        m = re.match(r"^(\d+)\s*x?\s+(.+)$", e, re.I) or re.match(r"^(\d+)\s*x\s*(.+)$", e, re.I)
        if m:
            qty, e = int(m.group(1)), m.group(2).strip()
        elif ":" in e and e.rsplit(":", 1)[1].strip().isdigit():
            e, q = e.rsplit(":", 1)
            e, qty = e.strip(), int(q)
        want.append({"item": e, "qty": max(1, min(qty, 6))})
    if not want:
        return "Which items should I add?"
    if _r.get("browser:busy") == "remy":
        queued = " He's mid-run, so this is queued and starts when he finishes."
    else:
        queued = ""
    params = {"action": "add", "items": want, "stores": [store.strip().lower() or "heb"],
              "force": bool(force), "notify_room": ACTIVE_ROOM.get("room", "")}
    try:
        mqtt_publish.single("jarvis/agents/grocery/trigger",
                            json.dumps({"source": "llm", "params": params}),
                            hostname=MQTT_HOST, port=MQTT_PORT)
    except Exception as exc:
        return f"Failed to reach Remy: {exc}"
    listed = ", ".join("%d x %s" % (w["qty"], w["item"]) for w in want)
    return (f"Remy is adding {listed} to the "
            f"{store} cart (skipping anything already in it).{queued} He reports back in a "
            "minute or two; nothing is checked out.")


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
def learn_fresh_cart(store: str = "", source: str = "cart") -> str:
    """Learn the user's usual order, from a store's current cart OR from past
    orders. The learned items bias every future grocery list.

    Use when the user says "learn my Fresh cart", "scan my Target cart",
    "learn what I usually order", "read my past orders". Safe to re-run:
    past orders already learned are skipped, so counts stay honest.

    Args:
        store: 'amazon' (Fresh), 'whole_foods', 'target', 'heb', or 'all'.
            Empty = 'amazon' for a cart scan, all stores for past orders.
        source: 'cart' scans the current cart; 'orders' reads past orders
            (H-E-B and Target order pages, Amazon order history), counts how
            many orders each item was in, and keeps household items too
            (meal planning only uses food).
    """
    source = source.strip().lower()
    if source in ("orders", "order", "history", "past", "past_orders", "order_history"):
        stores = store.strip().lower() or "all"
        try:
            mqtt_publish.single(
                "jarvis/agents/grocery/trigger",
                json.dumps({"params": {"action": "learn_orders", "stores": stores,
                                       "notify_room": ACTIVE_ROOM.get("room", "")}}),
                hostname=MQTT_HOST,
                port=MQTT_PORT,
            )
        except Exception as exc:
            return f"Failed to trigger the order-history scan: {exc}"
        return ("On it, sir: Remy is reading your past orders "
                f"({'all stores' if stores == 'all' else stores}) to learn what you usually "
                "buy. It takes a few minutes; ask for the grocery agent's report after.")

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
