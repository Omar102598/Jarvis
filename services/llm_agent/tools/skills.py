"""load_skill — fetch a SKILL.md playbook into the turn on demand."""

from langchain_core.tools import tool

import prompt_files


@tool
def load_skill(name: str) -> str:
    """Load a skill's full playbook (listed by name in your instructions under
    'Skills'). Call it BEFORE acting in that area — it names the tools to use
    and the rules to follow."""
    sk = prompt_files.skills()
    key = (name or "").strip().lower()
    s = sk.get(key) or next((v for k, v in sk.items() if key and key in k), None)
    if not s:
        return f"No skill '{name}'. Available: {', '.join(sk) or 'none'}."
    return s["body"]
