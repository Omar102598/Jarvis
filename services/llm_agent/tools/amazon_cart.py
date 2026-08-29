"""Build an Amazon add-to-cart link.

Amazon has no API that puts items in a customer's cart on their behalf:

  - Alexa Shopping Actions' AddToShoppingCart only runs inside an Alexa skill
    session via Connections.StartConnection, which exits the skill and hands off
    to Alexa's own voice purchase flow. It needs a published skill and an Alexa
    device, so it cannot be driven from here.
  - Product Advertising API dropped cart operations in 5.0, and PA-API 5 itself
    was deprecated in April 2026 in favour of the Creators API, which is
    catalogue-only.

What does still work is the Add to Cart form: a plain URL carrying ASINs and
quantities that fills the cart of whoever opens it while signed in. That suits
this assistant better than an API would — the items are staged, and the purchase
stays a deliberate act in the user's own browser.

IMPORTANT: this fills the RETAIL cart. Amazon Fresh and Whole Foods use separate
local-market carts keyed by almBrandId, which this URL cannot reach — groceries
go through the grocery agent's browser automation instead.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote

from langchain_core.tools import tool

# Optional: attribute the cart to an Associates tag when one is configured.
ASSOCIATE_TAG = os.environ.get("AMAZON_ASSOCIATE_TAG", "").strip()

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_MAX_ITEMS = 10          # the form accepts more, but long URLs get unwieldy
_MAX_QTY = 30


def _parse_items(items: str) -> tuple[list[tuple[str, int]], list[str]]:
    """Parse "ASIN" / "ASIN:qty" entries, returning (valid, problems)."""
    parsed: list[tuple[str, int]] = []
    problems: list[str] = []

    for raw in re.split(r"[,\n]+", items or ""):
        entry = raw.strip()
        if not entry:
            continue
        asin, _, qty_raw = entry.partition(":")
        asin = asin.strip().upper()

        if not _ASIN_RE.match(asin):
            # Catching this here matters: Amazon silently ignores a malformed
            # ASIN, so the link would open a cart missing that item with no
            # indication anything went wrong.
            problems.append(f"{entry!r} is not a 10-character ASIN")
            continue

        qty = 1
        if qty_raw.strip():
            try:
                qty = int(qty_raw.strip())
            except ValueError:
                problems.append(f"{entry!r} has a non-numeric quantity")
                continue
            if not 1 <= qty <= _MAX_QTY:
                problems.append(f"{entry!r} quantity must be 1-{_MAX_QTY}")
                continue

        if any(a == asin for a, _ in parsed):
            problems.append(f"{asin} listed more than once")
            continue
        parsed.append((asin, qty))

    return parsed, problems


@tool
def amazon_add_to_cart(items: str) -> str:
    """Build a link that adds Amazon items to the user's cart when opened.

    Use for regular Amazon retail orders. NOT for groceries — Amazon Fresh and
    Whole Foods have separate carts this cannot reach; use the grocery agent.

    You must supply ASINs (Amazon's 10-character product ids, e.g. B00I0TJHDO).
    Look them up first with a search or browser tool if you only have product
    names — do not guess an ASIN, since a wrong one silently adds the wrong
    product.

    Args:
        items: Comma-separated ASINs, each optionally ":quantity".
               e.g. "B00I0TJHDO, B007GCH756:2"
    """
    parsed, problems = _parse_items(items)

    if not parsed:
        detail = "; ".join(problems) if problems else "no items given"
        return (f"No valid items to add ({detail}). Provide ASINs like "
                f"'B00I0TJHDO' or 'B00I0TJHDO:2'.")

    params: list[str] = []
    for idx, (asin, qty) in enumerate(parsed[:_MAX_ITEMS], start=1):
        params.append(f"ASIN.{idx}={quote(asin)}&Quantity.{idx}={qty}")
    if ASSOCIATE_TAG:
        params.insert(0, f"AssociateTag={quote(ASSOCIATE_TAG)}")

    url = "https://www.amazon.com/gp/aws/cart/add.html?" + "&".join(params)

    lines = [f"Amazon cart link ({len(parsed[:_MAX_ITEMS])} item(s)):", url, ""]
    for asin, qty in parsed[:_MAX_ITEMS]:
        lines.append(f"  {asin} x{qty}")

    if len(parsed) > _MAX_ITEMS:
        lines.append(f"\nOnly the first {_MAX_ITEMS} items are in the link; "
                     f"{len(parsed) - _MAX_ITEMS} were left out.")
    if problems:
        # Report rather than swallow — a quietly dropped item is the failure
        # mode most likely to go unnoticed until the order arrives.
        lines.append("\nSkipped: " + "; ".join(problems))

    lines.append("\nOpening this link while signed in adds the items to the "
                 "RETAIL cart. Nothing is purchased — checkout stays manual.")
    return "\n".join(lines)
