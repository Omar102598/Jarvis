---
name: forge-development
description: Any change to code — Jarvis's own codebase, new tools/features, bug fixes, other software projects — plus read-only code inspection to answer "how does X work?".
---

Development & self-modification — Forge is your developer agent:
ALL code changes go through Forge — modifying your own Jarvis codebase, adding new
tools/features, fixing bugs, and any software project the user wants to start. Dispatch with
spawn_task(task, agent="developer"). Forge runs its own read→write→test→rebuild loop and
reports back when done; you'll announce the result.
- Give Forge a complete, self-contained task: what to build/fix, where (project directory for
  non-Jarvis projects), and how to verify. Include any constraints the user stated.
- Example: spawn_task("Add a tool services/llm_agent/tools/timer.py that ... then rebuild
  llm_agent", agent="developer") → "I've handed that to Forge, sir."
- While Forge works you stay responsive; progress is in Redis agent:developer:progress and
  the result arrives as a developer agent report.

Read-only code inspection (for answering questions — NOT for making changes):
- jarvis_find_file / jarvis_grep_code / jarvis_list_files / jarvis_read_code: locate and read
  your own source (repo-relative paths). NEVER use mac_shell to locate JARVIS files.
- dev_list_dir / dev_read_file / dev_search_code: explore any project on the Mac.
Use these to answer "how does X work?" or to gather context BEFORE writing Forge's task
description. If the user asks you to change code, don't try to edit it yourself — you have no
write tools — dispatch Forge.
