"""Shared Firecrawl scrape + structured extraction for agents.

Scout and Remy each grew their own copy of this because they needed different
shapes at different times. This is the third and fourth use, so it lives in one
place — but deliberately does NOT touch those two: they work, and rewriting a
working scraper to share code is how working scrapers stop working. New callers
use this; the old two can migrate if they ever need to change anyway.

Why agents want this rather than a plain GET: retail and job pages render prices
in JS and sit behind bot checks, so a plain fetch returns either a 403 or a
challenge page that still parses as HTML. The second case is the dangerous one —
a regex over a bot page finds *a* dollar figure and returns it, so the agent
records a number that was never a price and reports a "drop" against it.
Measured on a real Target page: a plain GET returned 200 with a bot challenge
and the extractor produced $35.00 for a product that costs nothing like that.

Everything here degrades to None rather than raising. A monitoring agent that
crashes is worse than one that reports it could not check.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import aiohttp

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_URL = os.environ.get("FIRECRAWL_URL", "https://api.firecrawl.dev").rstrip("/")

# Extraction runs a model server-side, so it is seconds not milliseconds.
_TIMEOUT_S = int(os.environ.get("FIRECRAWL_TIMEOUT_S", "90"))


def available() -> bool:
    return bool(FIRECRAWL_API_KEY)


async def extract(
    url: str,
    prompt: str,
    schema: dict,
    *,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[dict]:
    """Scrape ``url`` and return structured data matching ``schema``.

    None means "could not get it" for any reason — unconfigured, HTTP error,
    timeout, or malformed response. Callers decide what to do about that; none
    of them should have to care which of those it was.

    A schema is required rather than optional: free-text extraction gives a
    string that each caller then has to parse, which is the fragile step this
    exists to remove.
    """
    if not FIRECRAWL_API_KEY or not (url or "").strip():
        return None

    body: dict[str, Any] = {
        "url": url,
        "onlyMainContent": True,
        "formats": ["json"],
        "jsonOptions": {"prompt": prompt, "schema": schema},
    }

    owned = session is None
    sess = session or aiohttp.ClientSession()
    try:
        async with sess.post(
            f"{FIRECRAWL_URL}/v1/scrape",
            headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json()
    except Exception:
        return None
    finally:
        if owned:
            await sess.close()

    data = (payload.get("data") or {}).get("json")
    return data if isinstance(data, dict) else None
