"""Job monitor agent.

Searches job boards for listings that match configured keywords and location.
New listings (not seen in the past 7 days) are stored in Redis, summarised
with an LLM, and reported.
"""

import hashlib
import os

import aiohttp
from openai import AsyncOpenAI

from base_agent import BaseAgent

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
_openai = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "placeholder"))

_JOB_SITES = (
    "site:linkedin.com/jobs OR site:greenhouse.io OR site:lever.co OR site:indeed.com"
)

_SYSTEM_PROMPT = (
    "You are JARVIS. Summarise these new job listings for your user. "
    "For each listing include: job title, company (if known), location/remote status, "
    "and one sentence on why it looks interesting. Use bullet points. Be concise."
)


class JobMonitorAgent(BaseAgent):
    """Finds new job listings matching configured keywords."""

    async def run(self) -> str:
        keywords: list[str] = self.params.get("keywords", ["software engineer"])
        location: str = self.params.get("location", "remote")

        if not TAVILY_API_KEY:
            return "Job monitor agent: TAVILY_API_KEY is not configured."

        new_listings: list[dict] = []

        async with aiohttp.ClientSession() as session:
            for keyword in keywords:
                query = f"{keyword} {location} job opening {_JOB_SITES}"
                try:
                    async with session.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": TAVILY_API_KEY,
                            "query": query,
                            "search_depth": "basic",
                            "max_results": 5,
                        },
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        for result in data.get("results", []):
                            url = result.get("url", "")
                            url_hash = hashlib.md5(url.encode()).hexdigest()
                            seen_key = f"agent:job_monitor:seen:{url_hash}"
                            if not self.r.exists(seen_key):
                                self.r.set(seen_key, "1", ex=7 * 24 * 3600)
                                new_listings.append(
                                    {
                                        "title": result.get("title", "No title"),
                                        "url": url,
                                        "snippet": result.get("content", "")[:250],
                                    }
                                )
                except Exception as exc:
                    new_listings.append(
                        {"title": f"Search error: {exc}", "url": "", "snippet": ""}
                    )

        if not new_listings:
            return "Job monitor: No new listings found since the last check."

        listing_text = "\n\n".join(
            f"- **{j['title']}**\n  {j['snippet']}\n  {j['url']}"
            for j in new_listings
        )

        response = await _openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"New job listings found:\n\n{listing_text}",
                },
            ],
            max_tokens=600,
            temperature=0.3,
        )
        return response.choices[0].message.content
