"""Research agent — multi-angle web research synthesized into a report.

Primary path (2026-08): Anthropic's native server-side web search. The model
plans its own angles, searches iteratively, and synthesizes in one call —
better than the old fixed "plan 3 queries, take 4 snippets each" pipeline,
which could only ever see one round of results and pasted raw snippets into
context. Dynamic filtering keeps irrelevant material out of the context window.

Fallback path: the original Tavily plan-and-gather flow, used when native
search is unavailable (no Anthropic key, non-Claude model, or an API failure).
Kept intact so research never hard-fails on a search outage.
"""

import json
import os

from base_agent import BaseAgent
from llm_helper import complete, complete_with_search, search_available, tavily_search

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

# Native-search prompt: the model owns the searching, so the instruction is
# about COVERAGE and rigour rather than about consuming a fixed result set.
_SEARCH_PROMPT = (
    "You are JARVIS, a British AI assistant doing web research. Search the web "
    "to cover the topic from several distinct angles — not rephrasings of one "
    "query — and follow up when a result raises an obvious next question. "
    "Prefer primary and recent sources; note publication dates when recency "
    "matters.\n\n"
    "Then write a briefing: a 2-3 sentence summary, key findings as bullets, "
    "and a short 'bottom line'. Cite sources inline as [domain]. Be accurate; "
    "if sources conflict or the evidence is thin, say so plainly rather than "
    "papering over it. Keep it under 450 words."
)

# Research is the one place extra searches genuinely pay for themselves.
RESEARCH_MAX_SEARCHES = int(os.environ.get("RESEARCH_MAX_SEARCHES", "8"))


class ResearchAgent(BaseAgent):
    """Plans search angles, gathers results, synthesizes a briefing."""

    async def run(self) -> str:
        topic = self.params.get("topic") or self.params.get("task", "")
        if not topic:
            return "Research agent: no topic provided."

        if search_available():
            self.log_event("thinking", f"native web search: {topic[:80]}")
            report = await complete_with_search(
                _SEARCH_PROMPT,
                f"Topic: {topic}",
                max_tokens=1400,
                max_searches=RESEARCH_MAX_SEARCHES,
            )
            if report:
                return f"Research: {topic}\n\n{report}"
            self.log_event("finding", "native search unavailable — Tavily fallback")

        return await self._run_tavily(topic)

    # ---------------------------------------------------------------- fallback

    async def _run_tavily(self, topic: str) -> str:
        """Original plan → gather → synthesize flow (pre-2026-08 path)."""
        try:
            plan_raw = await complete(_PLAN_PROMPT, f"Topic: {topic}", max_tokens=200)
            queries = json.loads(plan_raw[plan_raw.find("["): plan_raw.rfind("]") + 1])
            if not isinstance(queries, list) or not queries:
                raise ValueError
        except Exception:
            queries = [topic]  # fall back to a single search

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
        report = await complete(
            _SYNTH_PROMPT,
            f"Topic: {topic}\n\nSearch results:\n\n{evidence}",
            max_tokens=900,
        )
        return f"Research: {topic}\n\n{report}"
