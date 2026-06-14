"""Generic task agent — handles arbitrary one-off goals dispatched at runtime.

This is what spawn_task targets. Unlike the scheduled agents, it receives its
goal at trigger time (params["task"]). It decides whether the goal needs web
research, gathers what it can, and produces a result. Good for open-ended
"go find out / figure out X" requests that may run longer than a live
conversation should wait for.
"""

from base_agent import BaseAgent
from llm_helper import complete, tavily_search

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


class TaskAgent(BaseAgent):
    """Runs an arbitrary delegated task, optionally with web research."""

    async def run(self) -> str:
        task = self.params.get("task", "").strip()
        if not task:
            return "Task agent: no task description provided."

        # Triage: does this need fresh web data?
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
