"""Newsletter digest agent.

Searches the web for news on configured topics via the Tavily API, then
asks an LLM to produce a concise daily digest.
"""

import os

import aiohttp

from base_agent import BaseAgent
from llm_helper import complete

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

_SYSTEM_PROMPT = (
    "You are JARVIS, a British AI assistant. Summarise these news articles into a "
    "concise daily digest for your user. Use short bullet points grouped by topic. "
    "Highlight the two or three most important stories at the top. Keep it under "
    "400 words total. Be informative but not verbose."
)


class NewsletterAgent(BaseAgent):
    """Fetches news articles and produces a summarised digest."""

    async def run(self) -> str:
        topics: list[str] = self.params.get("topics", ["technology news"])
        max_articles: int = self.params.get("max_articles", 10)

        if not TAVILY_API_KEY:
            return "Newsletter agent: TAVILY_API_KEY is not configured."

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
                            "search_depth": "basic",
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
