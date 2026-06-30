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
