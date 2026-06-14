"""Python execution tool for JARVIS — run code in a restricted subprocess.

Runs short Python snippets for calculation, data parsing, and quick logic.
Executes in a separate process with a timeout so a runaway snippet can't hang
the agent. This is NOT a hardened sandbox — it runs on the host with the
service's permissions, so it's gated by CODE_EXEC_ENABLED (default off).

Enable with CODE_EXEC_ENABLED=true on a machine you trust.
"""

import asyncio
import os
import sys
import tempfile

from langchain_core.tools import tool

ENABLED = os.environ.get("CODE_EXEC_ENABLED", "false").lower() in ("1", "true", "yes")
TIMEOUT = float(os.environ.get("CODE_EXEC_TIMEOUT", "15"))


@tool
async def run_python(code: str) -> str:
    """Execute a short Python snippet and return its stdout.

    Use for arithmetic, date math, parsing/transforming data, or verifying
    logic. Print the result — only stdout is returned. No network or long
    loops; there's a hard timeout.

    Args:
        code: Python source to run. Use print() to output results.
    """
    if not ENABLED:
        return ("Code execution is disabled. Set CODE_EXEC_ENABLED=true to "
                "enable it (only on a trusted machine).")

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        return f"Execution timed out after {TIMEOUT}s."
    except Exception as exc:
        return f"Execution failed: {exc}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    out = stdout.decode("utf-8", errors="replace").strip()
    return out[:4000] if out else "(ran successfully, no output — did you print?)"
