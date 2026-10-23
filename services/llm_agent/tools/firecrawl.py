"""scrape_page — fetch any web page as clean text (and optionally extract), via
Firecrawl. Handles JS rendering and anti-bot pages the plain fetch_url can't.

Use this over fetch_url when a page is JS-heavy, paywalled by a bot-check, or
when you want a specific fact/structured data pulled out of it. Key-gated on
FIRECRAWL_API_KEY (same key Scout uses); reports clearly if unset.
"""

from __future__ import annotations

import json
import os

import aiohttp
from langchain_core.tools import tool

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_URL = os.environ.get("FIRECRAWL_URL", "https://api.firecrawl.dev").rstrip("/")


@tool
async def scrape_page(url: str, extract: str = "") -> str:
    """Render a JavaScript-heavy page with Firecrawl. SECOND choice.

    Try `web_fetch` first -- it is free beyond tokens and filters content
    before it hits context. Come here when web_fetch cannot do the job, which
    in practice means one thing: the page is rendered with JavaScript (the
    web_fetch docs state it does not support those), or it blocks plain HTTP
    the way apartments.com returns 403.

    Firecrawl is a paid, metered service -- a run that exhausts the credits
    returns "Insufficient credits" and every caller fails at once, which is
    how the apartment watches went dark. Do not use it for pages that plain
    fetching already handles.

    Args:
        url: The full URL to render.
        extract: Optional description of the fields to pull out.
    """
    if not FIRECRAWL_API_KEY:
        return "Firecrawl isn't configured (set FIRECRAWL_API_KEY) — try fetch_url instead."
    if not url.strip():
        return "Give me a URL to fetch."

    body: dict = {"url": url, "onlyMainContent": True}
    if extract.strip():
        body["formats"] = ["markdown", "json"]
        body["jsonOptions"] = {"prompt": f"Extract: {extract.strip()}"}
    else:
        body["formats"] = ["markdown"]

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{FIRECRAWL_URL}/v1/scrape",
                headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                         "Content-Type": "application/json"},
                json=body,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as resp:
                if resp.status != 200:
                    return f"Firecrawl error {resp.status} for {url}."
                payload = await resp.json()
    except Exception as exc:
        return f"Couldn't fetch {url}: {exc}"

    data = payload.get("data") or {}
    out = []
    if extract.strip() and data.get("json"):
        out.append("Extracted: " + json.dumps(data["json"])[:1500])
    md = (data.get("markdown") or "").strip()
    if md:
        out.append(md[:6000] + ("…" if len(md) > 6000 else ""))
    return "\n\n".join(out) or f"No content returned for {url}."
