"""Job monitor agent.

Searches job boards for listings that match configured keywords and location.
New listings (not seen in the past 7 days) are stored in Redis, summarised
with an LLM, and reported.
"""

import hashlib
import os

import aiohttp

import firecrawl_client
from base_agent import BaseAgent
from llm_helper import complete

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

_JOB_SITES = (
    "site:linkedin.com/jobs OR site:greenhouse.io OR site:lever.co OR site:indeed.com"
)

_SYSTEM_PROMPT = (
    "You are JARVIS. Summarise these new job listings for your user. "
    "For each listing include: job title, company (if known), location/remote status, "
    "and one sentence on why it looks interesting. Use bullet points. Be concise. "
    "Some listings carry extra details read from the posting itself (salary, "
    "requirements) — lead with pay when it is known, since that is the thing a "
    "search snippet never says."
)


_JOB_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "salary": {"type": "string"},
        "location": {"type": "string"},
        "remote": {"type": "string"},
        "requirements": {"type": "string"},
    },
}

# Enrichment costs a scrape per listing, so it is capped. The summary is more
# useful for five well-described roles than for twenty title-only ones, and an
# unbounded run would scrape every result of every keyword.
_MAX_ENRICH = int(os.environ.get("JOB_MONITOR_ENRICH_MAX", "5"))


async def _enrich(listing: dict, session: aiohttp.ClientSession) -> dict:
    """Add salary/location/requirements by reading the actual posting.

    Search results carry a ~250 character snippet, which is usually the first
    lines of a description and rarely says what the job pays or whether it is
    genuinely remote — the two things that decide whether a listing is worth
    opening. Reading the page gets those. Failure just leaves the listing as it
    was, which is what the agent reported before this existed.
    """
    if not listing.get("url"):
        return listing
    data = await firecrawl_client.extract(
        listing["url"],
        "Extract the hiring company, salary or pay range, location, whether the "
        "role is remote/hybrid/onsite, and the key requirements from this job posting.",
        _JOB_SCHEMA,
        session=session,
    )
    if data:
        listing["details"] = {k: v for k, v in data.items() if v}
    return listing


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

            # Enrich the newest listings with what the search snippet leaves out.
            if firecrawl_client.available() and new_listings:
                for listing in new_listings[:_MAX_ENRICH]:
                    if listing.get("url"):
                        await _enrich(listing, session)

        if not new_listings:
            return "Job monitor: No new listings found since the last check."

        def _fmt(j: dict) -> str:
            out = f"- **{j['title']}**\n  {j['snippet']}\n  {j['url']}"
            # Without this the enrichment would be scraped, stored, and never
            # seen by the model writing the summary.
            details = j.get("details") or {}
            if details:
                out += "\n  " + " | ".join(f"{k}: {v}" for k, v in details.items())
            return out

        listing_text = "\n\n".join(_fmt(j) for j in new_listings)

        return await complete(
            system=_SYSTEM_PROMPT,
            user=f"New job listings found:\n\n{listing_text}",
            max_tokens=600,
        )
