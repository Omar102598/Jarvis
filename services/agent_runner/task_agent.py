"""Generic task agent — handles arbitrary one-off goals dispatched at runtime.

This is what spawn_task targets. Unlike the scheduled agents, it receives its
goal at trigger time (params["task"]). It decides whether the goal needs web
research, gathers what it can, and produces a result. Good for open-ended
"go find out / figure out X" requests that may run longer than a live
conversation should wait for.

Search (2026-08): the native path hands the model a web-search tool and lets it
decide whether to reach for it — which also retires the separate SEARCH/REASON
triage call the Tavily path needs, since that decision is now made in the same
turn as the work. The triage + Tavily flow remains as the fallback.
"""

from base_agent import BaseAgent
from llm_helper import complete, complete_with_search, search_available, tavily_search

_TRIAGE_PROMPT = (
    "Decide if this task needs live web information to answer well. "
    "Respond with exactly one word: SEARCH (needs current web data) or "
    "REASON (can be answered from general knowledge)."
)

_ANSWER_PROMPT = (
    "You are JARVIS, a British AI assistant working a task your user delegated. "
    "Complete it thoroughly and present the result clearly and concisely. "
    "If web results are provided, ground your answer in them and cite sources "
    "as [domain]. End with a one-line summary the user can act on."
)

_SEARCH_ANSWER_PROMPT = (
    "You are JARVIS, a British AI assistant working a task your user delegated. "
    "You can search the web — use it when the task depends on current or "
    "verifiable information, and answer directly when it does not. Do not "
    "search for things you already know reliably.\n\n"
    "Complete the task thoroughly and present the result clearly and concisely. "
    "Ground any factual claims in what you found and cite sources as [domain]. "
    "End with a one-line summary the user can act on."
)


class TaskAgent(BaseAgent):
    """Runs an arbitrary delegated task, optionally with web research."""

    async def run(self) -> str:
        task = self.params.get("task", "").strip()
        if not task:
            return "Task agent: no task description provided."

        if search_available():
            result = await complete_with_search(
                _SEARCH_ANSWER_PROMPT, f"Task: {task}", max_tokens=1200,
            )
            if result:
                return f"Task: {task}\n\n{result}"
            self.log_event("finding", "native search unavailable — Tavily fallback")

        return await self._run_tavily(task)

    # ---------------------------------------------------------------- fallback

    async def _run_tavily(self, task: str) -> str:
        """Original triage → search → answer flow (pre-2026-08 path)."""
        try:
            mode = (await complete(_TRIAGE_PROMPT, task, max_tokens=5)).strip().upper()
        except Exception:
            mode = "SEARCH"

        evidence = ""
        if "SEARCH" in mode:
            results = await tavily_search(task, max_results=6)
            if results:
                evidence = "\n\n".join(
                    f"**{r.get('title', '')}** ({r.get('url', '')})\n"
                    f"{r.get('content', '')[:400]}"
                    for r in results
                )

        user_msg = f"Task: {task}"
        if evidence:
            user_msg += f"\n\nRelevant web results:\n\n{evidence}"

        result = await complete(_ANSWER_PROMPT, user_msg, max_tokens=900)
        return f"Task: {task}\n\n{result}"
