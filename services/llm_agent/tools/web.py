"""Web fetch tool for JARVIS.

Fetches an arbitrary URL, extracts readable text, and answers a question
about it using the LLM. Complements web_search (which only returns snippets)
by letting JARVIS read a full page.

    fetch_url("https://example.com/article", "What are the main points?")
"""

import os
import re

import aiohttp
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

MAX_CHARS = int(os.environ.get("FETCH_MAX_CHARS", "12000"))
TIMEOUT = aiohttp.ClientTimeout(total=25)
_UA = "Mozilla/5.0 (compatible; JARVIS/1.0; +https://github.com/Omar102598/Jarvis)"


def _html_to_text(html: str) -> str:
    """Strip a webpage down to readable text."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except ImportError:
        # Fallback: crude tag strip if BeautifulSoup isn't installed
        text = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)

    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


@tool
async def fetch_url(url: str, question: str = "Summarize this page.") -> str:
    """LAST RESORT page reader. Prefer `web_fetch`, then `scrape_page`.

    Reading a page has three tiers, in this order:

    1. `web_fetch` (Anthropic server tool) -- the default. Costs only the
       tokens it returns, and filters the page before it reaches context.
    2. `scrape_page` (Firecrawl) -- when web_fetch fails because the page is
       rendered with JavaScript, which web_fetch explicitly does not support.
    3. this tool -- only when both are unavailable. It does a plain HTTP GET
       with no JS rendering, pulls the WHOLE page into memory, and then spends
       a second LLM call to answer the question. Strictly worse on cost and
       capability; it exists so a missing API key cannot leave JARVIS unable
       to read anything at all.

    Args:
        url: The full URL to fetch (must start with http:// or https://).
        question: What to extract or answer about the page.
    """
    if not url.startswith(("http://", "https://")):
        return "Please provide a full URL starting with http:// or https://"

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, headers={"User-Agent": _UA}) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return f"Failed to fetch {url}: HTTP {resp.status}"
                html = await resp.text()
    except Exception as exc:
        return f"Could not fetch {url}: {exc}"

    text = _html_to_text(html)[:MAX_CHARS]
    if not text:
        return f"Fetched {url} but found no readable text."

    # Answer the question with the LLM
    try:
        from llm_factory import build_llm

        llm = build_llm(temperature=0.2)
        result = await llm.ainvoke([
            SystemMessage(content=(
                "You are JARVIS. Answer the user's question using ONLY the page "
                "content provided. Be concise. If the answer isn't present, say so."
            )),
            HumanMessage(content=f"Question: {question}\n\nPage content:\n{text}"),
        ])
        return result.content
    except Exception as exc:
        # Fall back to returning the raw text if the LLM step fails
        return f"(Could not run analysis: {exc})\n\nPage text:\n{text[:2000]}"
