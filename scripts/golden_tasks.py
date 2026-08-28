#!/usr/bin/env python3
"""Golden-task regression harness — a safety gate for Forge self-modification.

Since Forge edits the JARVIS codebase itself, this asserts the core stays intact
after any change. Fast, dependency-light static checks (safe to run on the host
without the service venvs), plus optional live health checks when services are up.

Run:  python3 scripts/golden_tasks.py
Exit: 0 = all green, 1 = a regression (Forge should NOT consider the edit done).
"""

from __future__ import annotations

import ast
import glob
import json
import os
import py_compile
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures: list[str] = []
checks = 0


def ok(msg): print(f"  ✅ {msg}")
def bad(msg): failures.append(msg); print(f"  ❌ {msg}")


# 1) Every service .py compiles ------------------------------------------------
print("1) Compiling all service modules…")
for path in glob.glob(f"{REPO}/services/**/*.py", recursive=True):
    if ".bak" in path or "/venv/" in path or "/.venv" in path:
        continue
    checks += 1
    try:
        py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as e:
        bad(f"compile: {os.path.relpath(path, REPO)} — {e.msg.splitlines()[-1][:80]}")
if not failures:
    ok(f"{checks} modules compile")

# 2) Config files parse --------------------------------------------------------
print("2) Parsing configs…")
try:
    import yaml
    for f in ("docker-compose.yml", "config/agents.yml", "config/mcp_servers.yml"):
        p = os.path.join(REPO, f)
        if os.path.exists(p):
            yaml.safe_load(open(p))
            ok(f"{f} parses")
except ImportError:
    print("  ⚠ pyyaml not available — skipping config parse (non-fatal)")
except Exception as e:
    bad(f"config parse: {e}")

# 3) Brain still registers the critical tools ---------------------------------
print("3) Critical brain tools present…")
CRITICAL = [
    "control_device", "web_search", "spawn_task", "remember", "get_weather",
    "get_calendar_events", "manage_tasks", "get_readiness", "log_meal",
    "recall_journal", "get_spending_insights",
]
main_src = ""
mp = os.path.join(REPO, "services/llm_agent/main.py")
if os.path.exists(mp):
    main_src = open(mp).read()
    # crude: names must appear in the file (import + tool list)
    missing = [t for t in CRITICAL if t not in main_src]
    if missing:
        bad(f"brain missing tool registrations: {', '.join(missing)}")
    else:
        ok(f"all {len(CRITICAL)} critical tools registered")
    # the tool-list must be syntactically valid Python (parse the whole file)
    try:
        ast.parse(main_src)
        ok("llm_agent/main.py parses as valid Python")
    except SyntaxError as e:
        bad(f"llm_agent/main.py syntax: {e}")
else:
    bad("services/llm_agent/main.py missing")

# 4) Optional live health (only if services are up) ---------------------------
print("4) Live health (optional)…")
try:
    import urllib.request
    with urllib.request.urlopen("http://localhost:8080/health", timeout=3) as r:
        if json.load(r).get("status") == "ok":
            ok("mobile_gateway /health ok")
        else:
            print("  ⚠ gateway health non-ok (non-fatal)")
except Exception:
    print("  ⚠ gateway not reachable (skipped — non-fatal)")

# Summary ----------------------------------------------------------------------
print("\n" + ("=" * 50))
if failures:
    print(f"❌ GOLDEN TASKS FAILED ({len(failures)} issue(s)):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ GOLDEN TASKS PASSED — core intact.")
sys.exit(0)
