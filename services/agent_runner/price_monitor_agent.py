"""Price monitor agent — watches product URLs and reports notable price drops.

Configure with a list of {name, url} products. Each run fetches the page,
extracts a price, compares it to the last seen price (stored in Redis), and
reports changes. Works on its own schedule or on demand via spawn_task.
"""

import os
import re

import aiohttp

import firecrawl_client
from base_agent import BaseAgent

_PRICE_RE = re.compile(r"[$£€]\s?([0-9][0-9,]*\.?[0-9]{0,2})")
_UA = "Mozilla/5.0 (compatible; JARVIS-PriceMonitor/1.0)"


_PRICE_SCHEMA = {
    "type": "object",
    "properties": {"price": {"type": "number"},
                   "in_stock": {"type": "boolean"}},
    "required": ["price"],
}


async def _price_via_firecrawl(url: str, name: str,
                               session: aiohttp.ClientSession) -> float | None:
    """The product's real current price, or None if it could not be read."""
    data = await firecrawl_client.extract(
        url,
        f"Extract the current selling price of the product '{name}' on this page. "
        f"If several prices are shown, use the one the customer would pay now.",
        _PRICE_SCHEMA,
        session=session,
    )
    if not data:
        return None
    try:
        price = float(data.get("price"))
    except (TypeError, ValueError):
        return None
    # 0 is what extraction returns when it found nothing, and a free product is
    # not a price drop worth waking someone for.
    return price if price > 0 else None


def _extract_price(html: str) -> float | None:
    """Best-effort price from raw HTML — the fallback when Firecrawl is not set.

    Kept deliberately, but it is the weak path: a bot-challenge page still
    parses as HTML, so this returns *a* dollar figure rather than nothing.
    Measured against a real Target page, a plain GET returned 200 with a
    challenge and this produced $35.00 for a product priced nothing like that.
    A fabricated baseline then makes every later comparison wrong too.
    """
    candidates = []
    for m in _PRICE_RE.finditer(html):
        try:
            candidates.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    # Heuristic: ignore implausible values, prefer the first plausible one
    plausible = [c for c in candidates if 1 <= c <= 100000]
    return plausible[0] if plausible else None


class PriceMonitorAgent(BaseAgent):
    """Tracks prices for configured products and flags changes."""

    async def run(self) -> str:
        products = self.params.get("products", [])
        if not products:
            return "Price monitor: no products configured (params.products is empty)."

        lines = []
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": _UA}) as session:
            for product in products:
                name = product.get("name", "item")
                url = product.get("url", "")
                if not url:
                    continue
                price = None
                if firecrawl_client.available():
                    price = await _price_via_firecrawl(url, name, session)

                if price is None:
                    # Either Firecrawl is unconfigured or it could not read the
                    # page. Fall back to fetching it ourselves, with the caveat
                    # in _extract_price's docstring.
                    try:
                        async with session.get(url) as resp:
                            html = await resp.text()
                    except Exception as exc:
                        lines.append(f"• {name}: could not check ({exc})")
                        continue
                    price = _extract_price(html)

                if price is None:
                    lines.append(f"• {name}: no price found on page")
                    continue

                key = f"agent:price_monitor:last:{name}"
                prev_raw = self.r.get(key)
                self.r.set(key, str(price))

                if prev_raw is None:
                    lines.append(f"• {name}: now ${price:.2f} (first check)")
                else:
                    prev = float(prev_raw)
                    if price < prev:
                        drop = prev - price
                        lines.append(
                            f"• {name}: ↓ DROPPED ${drop:.2f} — now ${price:.2f} "
                            f"(was ${prev:.2f})"
                        )
                    elif price > prev:
                        lines.append(f"• {name}: ↑ up to ${price:.2f} (was ${prev:.2f})")
                    else:
                        lines.append(f"• {name}: unchanged at ${price:.2f}")

        return "Price Monitor\n\n" + "\n".join(lines) if lines else \
            "Price monitor: nothing to report."
