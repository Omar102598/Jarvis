"""Price monitor agent — watches product URLs and reports notable price drops.

Configure with a list of {name, url} products. Each run fetches the page,
extracts a price, compares it to the last seen price (stored in Redis), and
reports changes. Works on its own schedule or on demand via spawn_task.
"""

import os
import re

import aiohttp

from base_agent import BaseAgent

_PRICE_RE = re.compile(r"[$£€]\s?([0-9][0-9,]*\.?[0-9]{0,2})")
_UA = "Mozilla/5.0 (compatible; JARVIS-PriceMonitor/1.0)"


def _extract_price(html: str) -> float | None:
    """Best-effort: return the most common-looking price on the page."""
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
