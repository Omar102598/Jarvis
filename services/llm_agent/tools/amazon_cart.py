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

import json
import os
import re
from urllib.parse import quote, quote_plus

import aiohttp
import redis
from langchain_core.tools import tool

from tools import cards

_BRIDGE = (f"http://{os.environ.get('MAC_BRIDGE_HOST', 'host.docker.internal')}"
           f":{os.environ.get('MAC_BRIDGE_PORT', '7777')}")
_r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)
_SEARCH_TTL = 1800   # prices move, but not within half an hour

# Runs in the Mac browser on an amazon.com search page. Amazon's server-side
# pages bot-block the VM, a real signed-in browser gets the real results.
_EXTRACT_JS = r"""(() => {
  const out = [];
  for (const el of document.querySelectorAll('div[data-component-type="s-search-result"][data-asin]')) {
    const asin = el.getAttribute('data-asin'); if (!asin) continue;
    const h2 = el.querySelector('h2');
    let title = (h2 && (h2.getAttribute('aria-label') || h2.innerText) || '').trim();
    const sponsored = /^Sponsored/.test(title) || !!el.querySelector('.puis-sponsored-label-text');
    title = title.replace(/^Sponsored Ad -\s*/, '');
    const price = (el.querySelector('.a-price:not(.a-text-price) .a-offscreen') || {}).textContent || '';
    const img = (el.querySelector('img.s-image') || {}).src || '';
    const rating = ((el.querySelector('.a-icon-alt') || {}).textContent || '').replace(' out of 5 stars', '');
    const rc = el.querySelector('[aria-label$="ratings"]');
    out.push({asin, title, price, img, rating, sponsored,
              reviews: rc ? rc.getAttribute('aria-label').replace(' ratings', '') : ''});
  }
  return JSON.stringify({title: document.title, items: out});
})()"""

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


async def _bridge(path: str, body: dict | None = None, timeout: int = 45) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.request("POST" if body is not None else "GET", f"{_BRIDGE}{path}",
                             json=body) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Mac browser error {resp.status}")
            return await resp.json()


@tool
async def amazon_search(query: str, max_results: int = 6) -> str:
    """Search Amazon for products and show them to the user as product cards
    (photo, price, rating; tapping opens the listing).

    Use whenever the user wants to see or compare products to buy on Amazon
    ("show me protein powders on Amazon", "find a USB-C hub"). Results are live
    listings with real ASINs, so use them (not memory or review sites) before
    amazon_add_to_cart. Sponsored results are marked.

    Args:
        query: what to search for, as you'd type it into Amazon.
        max_results: how many products to return (1-10).
    """
    n = max(1, min(int(max_results or 6), 10))
    key = f"amazon:search:{query.strip().lower()}"
    raw = _r.get(key)
    if not raw:
        busy = _r.get("browser:busy")
        if busy:
            return (f"{busy.capitalize()} is using the Mac browser right now (a grocery run); "
                    "Amazon search will work again in a few minutes.")
        try:
            await _bridge("/browser/navigate",
                          {"url": "https://www.amazon.com/s?k=" + quote_plus(query)})
            raw = (await _bridge("/browser/js", {"script": _EXTRACT_JS})).get("result") or ""
            data = json.loads(raw)
        except Exception as e:
            return f"Couldn't search Amazon through the Mac browser ({e})."
        if not data.get("items"):
            return (f"Amazon showed no products for '{query}' (page: {data.get('title', '?')}). "
                    "It may be asking for a sign-in or a captcha in the Mac browser.")
        _r.set(key, raw, ex=_SEARCH_TTL)
    items = json.loads(raw)["items"]
    # Organic results first; a sponsored slot isn't a recommendation.
    items = sorted(items, key=lambda i: i.get("sponsored", False))[:n]

    lines = [f"Amazon results for '{query}':"]
    for i in items:
        bits = [i["price"] or "no price shown"]
        if i.get("rating"):
            bits.append(f"{i['rating']}★ ({i.get('reviews') or '?'} ratings)")
        if i.get("sponsored"):
            bits.append("sponsored")
        lines.append(f"- {i['title'][:120]} | ASIN {i['asin']} | " + ", ".join(bits))
    card = cards.products(f"Amazon: {query}", [{
        "name": i["title"][:90], "price": i["price"], "image": i["img"],
        "url": f"https://www.amazon.com/dp/{i['asin']}",
        "detail": f"{i['rating']}★ · {i['reviews']} ratings" if i.get("rating") else "",
        "badge": "Sponsored" if i.get("sponsored") else "",
    } for i in items])
    return "\n".join(lines) + card


@tool
def amazon_add_to_cart(items: str) -> str:
    """Build a link that adds Amazon items to the user's cart when opened.

    Use for regular Amazon retail orders. NOT for groceries — Amazon Fresh and
    Whole Foods have separate carts this cannot reach; use the grocery agent.

    You must supply ASINs (Amazon's 10-character product ids, e.g. B00I0TJHDO).
    Look them up first with a search or browser tool if you only have product
    names — do not guess an ASIN, since a wrong one silently adds the wrong
    product.

    In the app, put each product's page link (returned below) on its own line
    in your reply: the app shows it as a preview card with the product photo.

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
        lines.append(f"  {asin} x{qty}: https://www.amazon.com/dp/{asin}")

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
