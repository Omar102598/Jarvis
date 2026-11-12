"""Agent prompts live as markdown, not Python strings.

Each agent's system prompt is a file under config/prompts/agents/<agent>/<name>.md
so a persona or rule can be changed without editing code or rebuilding the
image: config/ is volume-mounted at /config, so editing the file and
restarting agent_runner is enough.

The text is used verbatim -- including any {placeholders} a module later
fills with str.format -- so a file is exactly the string that used to be
inline.
"""

from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_DIRS = [
    os.environ.get("AGENT_PROMPTS_DIR", "/config/prompts/agents"),
    # running from a repo checkout (tests, local dev)
    os.path.normpath(os.path.join(_HERE, "..", "..", "config", "prompts", "agents")),
]


def agent_prompt(path: str) -> str:
    """Return the prompt at `<agent>/<name>` (no extension). Fails loudly:
    an agent running on an empty prompt would be worse than one that doesn't
    start."""
    for base in _DIRS:
        full = os.path.join(base, path + ".md")
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as f:
                return f.read().rstrip("\n")
    raise FileNotFoundError(f"agent prompt '{path}.md' not found in {_DIRS}")
