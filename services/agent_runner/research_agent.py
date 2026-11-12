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
from prompt_files import agent_prompt

_PLAN_PROMPT = agent_prompt("research/plan")

_SYNTH_PROMPT = agent_prompt("research/synth")

# Native-search prompt: the model owns the searching, so the instruction is
# about COVERAGE and rigour rather than about consuming a fixed result set.
_SEARCH_PROMPT = agent_prompt("research/search")

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
