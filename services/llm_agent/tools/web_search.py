"""Web Search tool for JARVIS — uses Tavily API."""

import os

import aiohttp
from langchain_core.tools import tool

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")


@tool
async def web_search(query: str) -> str:
    """Search the internet for current information.

    Args:
        query: The search query, e.g. 'weather in Austin today', 'latest news about SpaceX'
    """
    if not TAVILY_API_KEY:
        return "Web search is not configured. Set TAVILY_API_KEY in environment."

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
            },
        ) as resp:
            if resp.status != 200:
                return f"Search failed with status {resp.status}"
            data = await resp.json()
            results = []
            for r in data.get("results", []):
                title = r.get("title", "")
                content = r.get("content", "")[:300]
                url = r.get("url", "")
                results.append(f"**{title}**\n{content}\nSource: {url}")
            return "\n\n".join(results) if results else "No results found."
