"""Web monitor agent.

Watches configured search queries for newly published content.
Results seen in the past 3 days are skipped (deduplication via Redis).
"""

import hashlib
import os

import aiohttp
from openai import AsyncOpenAI

from base_agent import BaseAgent

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
_openai = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "placeholder"))

_SYSTEM_PROMPT = (
    "You are JARVIS. Summarise these newly detected web results for your user. "
    "Group by topic, highlight what is most interesting or actionable, and keep "
    "it under 300 words. Use bullet points."
)


class WebMonitorAgent(BaseAgent):
    """Monitors search queries and reports when notable new content appears."""

    async def run(self) -> str:
        queries: list[str] = self.params.get("queries", [])
        if not queries:
            return (
                "Web monitor agent: No queries configured. "
                "Add them under params.queries in config/agents.yml."
            )

        if not TAVILY_API_KEY:
            return "Web monitor agent: TAVILY_API_KEY is not configured."

        new_results: list[dict] = []

        async with aiohttp.ClientSession() as session:
            for query in queries:
                try:
                    async with session.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": TAVILY_API_KEY,
                            "query": query,
                            "search_depth": "basic",
                            "max_results": 3,
                        },
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        for result in data.get("results", []):
                            url = result.get("url", "")
                            url_hash = hashlib.md5(url.encode()).hexdigest()
                            seen_key = f"agent:web_monitor:seen:{url_hash}"
                            if not self.r.exists(seen_key):
                                self.r.set(seen_key, "1", ex=3 * 24 * 3600)
                                new_results.append(
                                    {
                                        "query": query,
                                        "title": result.get("title", ""),
                                        "url": url,
                                        "snippet": result.get("content", "")[:250],
                                    }
                                )
                except Exception as exc:
                    new_results.append(
                        {
                            "query": query,
                            "title": f"Search error: {exc}",
                            "url": "",
                            "snippet": "",
                        }
                    )

        if not new_results:
            return "Web monitor: No new content detected for any monitored query."

        results_text = "\n\n".join(
            f"[{r['query']}] **{r['title']}**\n{r['snippet']}\n{r['url']}"
            for r in new_results
        )

        response = await _openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Newly detected results:\n\n{results_text}"},
            ],
            max_tokens=500,
            temperature=0.3,
        )
        return response.choices[0].message.content
