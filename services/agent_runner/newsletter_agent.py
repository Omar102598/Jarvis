"""Newsletter digest agent.

Searches the web for news on configured topics, then produces a concise daily
digest.

Search (2026-08): the native path uses Anthropic's server-side web search, so
the model searches each topic itself and can follow a lead rather than being
handed a fixed slice of results. It also means the digest no longer requires a
Tavily key at all — previously this agent hard-failed without one. The Tavily
fetch remains as the fallback.
"""

import os

import aiohttp

from base_agent import BaseAgent
from llm_helper import complete, complete_with_search, search_available

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# Native search is OFF by default here, unlike Ada/Jeeves. Measured 2026-08-04:
# one native-search digest cost ~$0.57 (148k input tokens across the
# server-side search loop + 6 searches) versus roughly a cent for the Tavily
# path — ~$17/month for a daily agent, against a total spend of ~$3-4/day.
# A news digest doesn't need iterative search the way open-ended research does,
# so the quality gain doesn't earn that. Flip this on if you want the better
# recency filtering and inline citations and don't mind the bill.
USE_NATIVE_SEARCH = os.environ.get(
    "NEWSLETTER_NATIVE_SEARCH", "false").lower() in ("1", "true", "yes")

_SYSTEM_PROMPT = (
    "You are JARVIS, a British AI assistant. Summarise these news articles into a "
    "concise daily digest for your user. Use short bullet points grouped by topic. "
    "Highlight the two or three most important stories at the top. Keep it under "
    "400 words total. Be informative but not verbose."
)

_SEARCH_PROMPT = (
    "You are JARVIS, a British AI assistant compiling your user's daily news "
    "digest. Search the web for genuinely RECENT developments on each topic "
    "given — prefer the last 24-48 hours and skip evergreen or undated pieces. "
    "One focused search per topic, plus a follow-up only when a story clearly "
    "warrants it.\n\n"
    "Then write the digest: lead with the two or three most important stories, "
    "then short bullets grouped by topic, each naming its source as [domain]. "
    "Under 400 words. If a topic genuinely had no notable news, say so in one "
    "line rather than padding it with filler."
)


class NewsletterAgent(BaseAgent):
    """Fetches news articles and produces a summarised digest."""

    async def run(self) -> str:
        topics: list[str] = self.params.get("topics", ["technology news"])
        max_articles: int = self.params.get("max_articles", 10)

        if USE_NATIVE_SEARCH and search_available():
            # One search per topic, plus headroom for a follow-up or two.
            digest = await complete_with_search(
                _SEARCH_PROMPT,
                "Topics for today's digest:\n" + "\n".join(f"- {t}" for t in topics),
                max_tokens=900,
                max_searches=len(topics) + 2,
            )
            if digest:
                return digest
            self.log_event("finding", "native search unavailable — Tavily fallback")

        return await self._run_tavily(topics, max_articles)

    # ---------------------------------------------------------------- fallback

    async def _run_tavily(self, topics: list, max_articles: int) -> str:
        """Original per-topic Tavily fetch + digest (pre-2026-08 path)."""
        if not TAVILY_API_KEY:
            return ("Newsletter agent: no search available — set ANTHROPIC_API_KEY "
                    "(native search) or TAVILY_API_KEY.")

        articles: list[str] = []
        per_topic = max(2, max_articles // max(len(topics), 1))

        async with aiohttp.ClientSession() as session:
            for topic in topics:
                try:
                    async with session.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": TAVILY_API_KEY,
                            "query": topic,
                            "search_depth": "advanced",
                            "max_results": per_topic,
                        },
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for r in data.get("results", []):
                                title = r.get("title", "Untitled")
                                snippet = r.get("content", "")[:400]
                                articles.append(f"**{title}**\n{snippet}")
                except Exception as exc:
                    articles.append(f"[Could not fetch results for '{topic}': {exc}]")

        if not articles:
            return "Newsletter agent: No articles could be retrieved at this time."

        article_text = "\n\n---\n\n".join(articles[:max_articles])

        return await complete(
            system=_SYSTEM_PROMPT,
            user=f"Today's articles:\n\n{article_text}",
            max_tokens=600,
        )
