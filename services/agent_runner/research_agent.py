"""Research agent — multi-angle web research synthesized into a report.

Given a topic, it plans several search angles, gathers results across all of
them, and asks the LLM to synthesize a structured briefing. This is the
specialist that spawn_task dispatches for "research X" requests.
"""

import json

from base_agent import BaseAgent
from llm_helper import complete, tavily_search

_PLAN_PROMPT = (
    "You are a research planner. Given a topic, output 3-4 distinct web search "
    "queries that together cover it well (different angles, not rephrasings). "
    "Respond ONLY with a JSON array of query strings, nothing else."
)

_SYNTH_PROMPT = (
    "You are JARVIS, a British AI assistant. Using the search results provided, "
    "write a clear, structured briefing on the topic. Lead with a 2-3 sentence "
    "summary, then key findings as bullets, then a short 'bottom line'. Cite "
    "sources inline as [domain]. Be accurate; if results conflict or are thin, "
    "say so. Keep under 450 words."
)


class ResearchAgent(BaseAgent):
    """Plans search angles, gathers results, synthesizes a briefing."""

    async def run(self) -> str:
        topic = self.params.get("topic") or self.params.get("task", "")
        if not topic:
            return "Research agent: no topic provided."

        # 1. Plan search angles
        try:
            plan_raw = await complete(_PLAN_PROMPT, f"Topic: {topic}", max_tokens=200)
            queries = json.loads(plan_raw[plan_raw.find("["): plan_raw.rfind("]") + 1])
            if not isinstance(queries, list) or not queries:
                raise ValueError
        except Exception:
            queries = [topic]  # fall back to a single search

        # 2. Gather results across all angles
        gathered = []
        for q in queries[:4]:
            for r in await tavily_search(str(q), max_results=4):
                title = r.get("title", "Untitled")
                snippet = r.get("content", "")[:400]
                url = r.get("url", "")
                gathered.append(f"**{title}** ({url})\n{snippet}")

        if not gathered:
            return f"Research agent: no results found for '{topic}'."

        evidence = "\n\n---\n\n".join(gathered[:16])

        # 3. Synthesize
        report = await complete(
            _SYNTH_PROMPT,
            f"Topic: {topic}\n\nSearch results:\n\n{evidence}",
            max_tokens=900,
        )
        return f"Research: {topic}\n\n{report}"
