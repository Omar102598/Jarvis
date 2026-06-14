"""Shell execution tool for JARVIS — allowlisted host commands.

Lets JARVIS run system commands, but only those whose first token is on an
allowlist (SHELL_ALLOWLIST, comma-separated). This keeps "open the browser"
or "check disk space" available while refusing arbitrary/destructive commands.

Set SHELL_ALLOWLIST="*" to allow everything (only on a trusted machine).
Default allowlist is a safe, mostly read-only set.
"""

import asyncio
import os
import shlex

from langchain_core.tools import tool

_DEFAULT_ALLOW = (
    "echo,ls,cat,pwd,whoami,date,uptime,df,du,free,ps,uname,hostname,"
    "open,xdg-open,say,osascript,curl,ping,docker,systemctl,nvidia-smi"
)
ALLOWLIST = os.environ.get("SHELL_ALLOWLIST", _DEFAULT_ALLOW)
TIMEOUT = float(os.environ.get("SHELL_TIMEOUT", "30"))


def _is_allowed(command: str) -> bool:
    if ALLOWLIST.strip() == "*":
        return True
    try:
        first = shlex.split(command)[0]
    except (ValueError, IndexError):
        return False
    first = os.path.basename(first)
    allowed = {c.strip() for c in ALLOWLIST.split(",") if c.strip()}
    return first in allowed


@tool
async def run_shell(command: str) -> str:
    """Run a shell command on the JARVIS host and return its output.

    Use for system actions like opening an app, checking disk space, or
    restarting a service. Only allowlisted commands are permitted.

    Args:
        command: The shell command to run, e.g. 'df -h', 'open -a Spotify'.
    """
    if not _is_allowed(command):
        first = command.split()[0] if command.split() else command
        return (
            f"Command '{first}' is not on the allowlist. "
            f"Allowed: {ALLOWLIST}. (Set SHELL_ALLOWLIST to change this.)"
        )

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        return f"Command timed out after {TIMEOUT}s."
    except Exception as exc:
        return f"Command failed: {exc}"

    output = stdout.decode("utf-8", errors="replace").strip()
    rc = proc.returncode
    if not output:
        return f"(exit {rc}, no output)"
    return f"(exit {rc})\n{output[:4000]}"
