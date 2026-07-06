"""Developer agent — "Forge", Jarvis's dedicated software engineer.

All self-modification of the Jarvis codebase and any coding project the user
starts goes through this agent. The main llm_agent keeps read-only code tools
for quick questions; every write/build/test is dispatched here via
spawn_task(task, agent="developer").

Unlike the other background agents (single LLM completion), Forge runs a full
Anthropic tool-use loop: it reads code, writes files, runs shell commands, and
verifies its own work before reporting back. All filesystem/shell access goes
through mac_bridge on the host, so it inherits the same path rules as the
llm_agent's tools:

  jarvis_* endpoints — repo-relative paths, writes restricted to the Jarvis repo
  dev_*    endpoints — any project on the Mac by absolute or ~ path

Progress is streamed to Redis (agent:developer:progress) so the dashboard and
the user can watch what Forge is doing mid-task.
"""

import json
import os
from datetime import datetime, timezone

import aiohttp

from base_agent import BaseAgent

MAC_BRIDGE_HOST = os.environ.get("MAC_BRIDGE_HOST", "host.docker.internal")
MAC_BRIDGE_PORT = int(os.environ.get("MAC_BRIDGE_PORT", "7777"))
_BASE = f"http://{MAC_BRIDGE_HOST}:{MAC_BRIDGE_PORT}"

# Coding is the one place the powerful tier earns its cost. On-demand only.
DEV_MODEL = os.environ.get("DEV_LLM_MODEL", "claude-opus-4-8")
MAX_STEPS = int(os.environ.get("DEV_AGENT_MAX_STEPS", "40"))

# Prefer the Claude Code CLI on the host (billed to the user's Claude PRO
# subscription, NOT API credits). Falls back to the API tool-loop when the
# CLI is missing/fails. The CLI runs on the Mac via mac_bridge /dev/shell
# with full repo access and its own tools — it completes the task itself.
DEV_PREFER_CLI = os.environ.get("DEV_PREFER_CLI", "true").lower() == "true"
DEV_CLI_BIN = os.environ.get("DEV_CLI_BIN", "/usr/local/bin/claude")
DEV_CLI_TIMEOUT_S = int(os.environ.get("DEV_CLI_TIMEOUT_S", "900"))
DEV_CLI_ARGS = os.environ.get(
    "DEV_CLI_ARGS",
    '--permission-mode acceptEdits '
    '--allowedTools "Bash(git:*)" "Bash(python3:*)" "Bash(python:*)" '
    '"Bash(npm:*)" "Bash(pytest:*)" "Bash(docker:*)" "Bash(ls:*)" "Bash(grep:*)"',
)
_TOOL_RESULT_LIMIT = 20_000  # chars per tool result kept in context

_SYSTEM_PROMPT = """You are Forge, JARVIS's dedicated developer agent. You handle all \
self-modification of the Jarvis codebase and any software project the user delegates.

Environment:
- jarvis_read / jarvis_write / jarvis_find / jarvis_grep operate on the JARVIS repo \
with repo-relative paths (e.g. 'services/llm_agent/tools/mac.py'). Writes are \
restricted to the repo.
- dev_read / dev_write / dev_list / dev_search / shell operate on ANY project on the \
Mac by absolute or ~ path.
- After changing Jarvis service code, call rebuild_service (docker services: \
llm_agent, dashboard, agent_runner, mobile_gateway; native: stt, tts_mac, wake_word, \
speaker_verify, mac_bridge). Plugins in services/llm_agent/plugins/ hot-reload — no \
rebuild needed.

Method — follow strictly:
1. Explore before editing: locate the relevant files and READ them first. Writes \
replace the whole file, so never write a file you haven't just read.
2. Make the smallest change that accomplishes the task. Match the existing style.
3. Verify your work: py_compile for Python, the project's own build/test command \
otherwise (shell with cwd set). Fix failures before finishing.
4. Use git (via shell) to review your own diff before reporting. Never commit or \
push unless the task explicitly asks.
5. Never read or write .env, credentials, or API keys. Never delete files unless \
the task explicitly asks.

When done, reply with a plain-text report: what changed (file paths), how you \
verified it, and anything the user should follow up on. If you could not finish, \
say exactly what's blocking and what you tried."""

# ---------------------------------------------------------------------------
# Tool schemas (Anthropic native format)
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "jarvis_read",
        "description": "Read a file in the JARVIS repo by repo-relative path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative path, e.g. 'services/llm_agent/main.py'"}},
            "required": ["path"],
        },
    },
    {
        "name": "jarvis_write",
        "description": "Write/overwrite a file in the JARVIS repo (repo-relative path). Read the file first — this replaces the entire file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "Full new file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "jarvis_find",
        "description": "Find files by name/glob pattern anywhere in the JARVIS repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "e.g. 'mac.py', '*.yml'"},
                "directory": {"type": "string", "description": "Optional repo-relative subdirectory"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "jarvis_grep",
        "description": "Search file contents across the JARVIS repo for a text/regex pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "directory": {"type": "string", "description": "Optional repo-relative subdirectory"},
                "file_pattern": {"type": "string", "description": "Optional extension filter, e.g. '*.py'"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "rebuild_service",
        "description": "Rebuild/restart a JARVIS service so code changes take effect.",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string", "description": "e.g. 'llm_agent', 'agent_runner', 'dashboard'"}},
            "required": ["service"],
        },
    },
    {
        "name": "dev_read",
        "description": "Read any file on the Mac by absolute or ~ path (non-Jarvis projects).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "e.g. '~/Projects/myapp/src/index.ts'"}},
            "required": ["path"],
        },
    },
    {
        "name": "dev_write",
        "description": "Write/create any file on the Mac by absolute or ~ path. Creates parent dirs; keeps a .bak of the original. Replaces the entire file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "Full new file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "dev_list",
        "description": "List a directory on the Mac by absolute or ~ path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "dev_search",
        "description": "Search file contents across any project directory on the Mac.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Project root, e.g. '~/Projects/myapp'"},
                "pattern": {"type": "string"},
                "file_pattern": {"type": "string", "description": "Optional, e.g. '*.ts'"},
            },
            "required": ["directory", "pattern"],
        },
    },
    {
        "name": "shell",
        "description": "Run a shell command on the Mac (builds, tests, git, package managers). Returns combined output + exit code. 120s timeout.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "e.g. 'python -m py_compile services/llm_agent/main.py'"},
                "cwd": {"type": "string", "description": "Working directory (absolute or ~ path)"},
            },
            "required": ["cmd"],
        },
    },
]

# ---------------------------------------------------------------------------
# Write safety guard (mirrors llm_agent/tools/self_modify.py)
# ---------------------------------------------------------------------------

_BLOCKED_BASENAMES = {".env", "docker-compose.yml", "docker-compose.gpu.yml"}
_BLOCKED_CONTENT = [
    "ANTHROPIC_API_KEY=", "OPENAI_API_KEY=", "MOBILE_API_KEY=",
    "GITHUB_TOKEN=", "SPOTIFY_CLIENT_SECRET=", "BANKSYNC_API_KEY=",
]


def _write_blocked(path: str, content: str) -> str | None:
    if os.path.basename(path) in _BLOCKED_BASENAMES:
        return f"Blocked: '{os.path.basename(path)}' must be edited manually."
    for pat in _BLOCKED_CONTENT:
        if pat in content:
            return f"Blocked: content contains credential pattern '{pat.rstrip('=')}'."
    return None


class DeveloperAgent(BaseAgent):
    """Forge — tool-use coding loop over mac_bridge, dispatched on demand."""

    async def run(self) -> str:
        task = (self.params.get("task") or "").strip()
        if not task:
            return "Developer agent: no task provided."

        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            return "Developer agent: anthropic SDK not installed in agent_runner."

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "Developer agent: ANTHROPIC_API_KEY not set."

        client = AsyncAnthropic(api_key=api_key)
        self.r.delete("agent:developer:progress")
        self._progress(f"Task accepted: {task[:200]}")

        # CLI-first: run the task through Claude Code on the host (Pro plan)
        if DEV_PREFER_CLI:
            cli_report = await self._run_via_cli(task)
            if cli_report is not None:
                return cli_report
            self._progress("CLI path unavailable/failed — falling back to API loop")

        project_dir = (self.params.get("project_dir") or "").strip()
        user_msg = f"Task: {task}"
        if project_dir:
            user_msg += f"\n\nProject directory: {project_dir}"

        messages: list[dict] = [{"role": "user", "content": user_msg}]
        files_touched: list[str] = []

        async with aiohttp.ClientSession() as http:
            for step in range(MAX_STEPS):
                resp = await client.messages.create(
                    model=DEV_MODEL,
                    max_tokens=8192,
                    system=_SYSTEM_PROMPT,
                    tools=_TOOLS,
                    messages=messages,
                )

                if resp.stop_reason != "tool_use":
                    report = "".join(
                        b.text for b in resp.content if getattr(b, "type", "") == "text"
                    ).strip()
                    self._progress("Done.")
                    return self._final_report(task, report, files_touched)

                # Execute every tool call in this turn
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for block in resp.content:
                    if getattr(block, "type", "") != "tool_use":
                        continue
                    self._progress(f"[{step + 1}/{MAX_STEPS}] {block.name} {self._preview(block.input)}")
                    output = await self._exec_tool(http, block.name, block.input or {})
                    if block.name in ("jarvis_write", "dev_write") and not output.startswith("Blocked"):
                        files_touched.append(str((block.input or {}).get("path", "?")))
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output[:_TOOL_RESULT_LIMIT],
                    })
                messages.append({"role": "user", "content": results})

        self._progress("Step limit reached.")
        return self._final_report(
            task,
            f"Stopped after {MAX_STEPS} steps without a final answer — the task may be "
            "partially done. Check `git status`/`git diff` in the repo, or re-dispatch "
            "with a narrower task.",
            files_touched,
        )

    # -- Claude Code CLI path (Pro-plan billing, no API tokens) --------------

    async def _run_via_cli(self, task: str) -> str | None:
        """Run the whole task through the Claude Code CLI on the host.

        Returns the report string on success, or None to trigger the API
        fallback (CLI missing, auth expired, non-zero exit, empty output).
        """
        import shlex

        project_dir = (self.params.get("project_dir") or "").strip()
        cwd = project_dir or "~/Documents/GitHub/Jarvis"
        prompt = (
            f"{task}\n\n"
            "Work autonomously; verify your changes (py_compile / build / "
            "tests as appropriate). Finish with a short report: files changed, "
            "how verified, anything the user must follow up on."
        )
        cmd = (f"{DEV_CLI_BIN} -p {shlex.quote(prompt)} "
               f"--output-format text {DEV_CLI_ARGS}")

        self._progress(f"Running via Claude Code CLI (Pro plan) in {cwd} …")
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{_BASE}/dev/shell",
                    json={"cmd": cmd, "cwd": cwd, "timeout": DEV_CLI_TIMEOUT_S},
                    timeout=aiohttp.ClientTimeout(total=DEV_CLI_TIMEOUT_S + 30),
                ) as resp:
                    data = await resp.json()
        except Exception as exc:
            self._progress(f"CLI transport error: {exc}")
            return None

        rc = data.get("returncode", 1)
        out = (data.get("output") or "").strip()
        if rc != 0 or not out:
            self._progress(f"CLI exited rc={rc}: {out[:200]}")
            return None
        self._progress("CLI run complete.")
        return (f"Dev task (via Claude Code CLI — Pro plan, no API tokens): "
                f"{task[:150]}\n\n{out[-3000:]}")

    # -- tool execution ------------------------------------------------------

    async def _exec_tool(self, http: aiohttp.ClientSession, name: str, args: dict) -> str:
        try:
            if name == "jarvis_read":
                return await self._get(http, "/jarvis/read", path=args["path"])
            if name == "jarvis_write":
                blocked = _write_blocked(args["path"], args.get("content", ""))
                return blocked or await self._post(http, "/jarvis/write", **args)
            if name == "jarvis_find":
                return await self._post(http, "/jarvis/find", pattern=args["pattern"],
                                        directory=args.get("directory", ""))
            if name == "jarvis_grep":
                return await self._post(http, "/jarvis/grep", pattern=args["pattern"],
                                        directory=args.get("directory", ""),
                                        file_pattern=args.get("file_pattern", ""))
            if name == "rebuild_service":
                return await self._post(http, "/jarvis/rebuild", service=args["service"])
            if name == "dev_read":
                return await self._get(http, "/dev/read", path=args["path"])
            if name == "dev_write":
                blocked = _write_blocked(args["path"], args.get("content", ""))
                return blocked or await self._post(http, "/dev/write", **args)
            if name == "dev_list":
                return await self._get(http, "/dev/list", path=args["path"])
            if name == "dev_search":
                return await self._post(http, "/dev/search", directory=args["directory"],
                                        pattern=args["pattern"],
                                        file_pattern=args.get("file_pattern", ""))
            if name == "shell":
                return await self._post(http, "/dev/shell", cmd=args["cmd"],
                                        cwd=args.get("cwd", ""), timeout=120)
            return f"Unknown tool: {name}"
        except KeyError as e:
            return f"Missing required argument {e} for {name}."
        except Exception as e:
            return f"Tool {name} failed: {e}"

    # First positional arg is the endpoint, NOT named 'path' — tool kwargs
    # legitimately include path=... (jarvis_read, dev_write, …).
    async def _get(self, http: aiohttp.ClientSession, endpoint: str, **params) -> str:
        async with http.get(f"{_BASE}{endpoint}", params=params,
                            timeout=aiohttp.ClientTimeout(total=30)) as resp:
            return self._render(await resp.json(), resp.status)

    async def _post(self, http: aiohttp.ClientSession, endpoint: str, **body) -> str:
        async with http.post(f"{_BASE}{endpoint}", json=body,
                             timeout=aiohttp.ClientTimeout(total=130)) as resp:
            return self._render(await resp.json(), resp.status)

    @staticmethod
    def _render(data: dict, status: int) -> str:
        """Flatten a mac_bridge JSON response into text for the model."""
        if status >= 400:
            return f"Error ({status}): {data.get('detail', data)}"
        if "content" in data:
            return data["content"] or "(empty file)"
        if "output" in data:  # shell
            rc = data.get("returncode", 0)
            out = (data.get("output") or "").strip()
            return f"exit {rc}\n{out}" if out else f"exit {rc}"
        if "matches" in data:
            matches = data["matches"]
            return "\n".join(str(m) for m in matches) if matches else "(no matches)"
        if "items" in data:  # dev_list
            return "\n".join(
                f"{'[DIR] ' if i.get('type') == 'dir' else '      '}{i.get('name')}"
                for i in data["items"]
            ) or "(empty directory)"
        if "files" in data:
            return "\n".join(data["files"]) or "(empty directory)"
        if "status" in data:
            return str(data["status"])
        return json.dumps(data)[:2000]

    # -- progress + report ---------------------------------------------------

    def _progress(self, line: str) -> None:
        entry = json.dumps({
            "line": line,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        try:
            self.r.lpush("agent:developer:progress", entry)
            self.r.ltrim("agent:developer:progress", 0, 99)
        except Exception:
            pass
        print(f"[Forge] {line}")

    @staticmethod
    def _preview(args: dict | None) -> str:
        if not args:
            return ""
        slim = {k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v)
                for k, v in args.items() if k != "content"}
        if "content" in (args or {}):
            slim["content"] = f"<{len(args['content'])} chars>"
        return json.dumps(slim, ensure_ascii=False)[:160]

    @staticmethod
    def _final_report(task: str, report: str, files_touched: list[str]) -> str:
        header = f"Dev task: {task}\n"
        if files_touched:
            header += "Files written: " + ", ".join(dict.fromkeys(files_touched)) + "\n"
        return f"{header}\n{report or '(no report text)'}"
