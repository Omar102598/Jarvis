#!/usr/bin/env python3
"""Decide which services the image-build workflow should rebuild.

Prints a GitHub Actions output line: ``services=["llm_agent", ...]``

Rebuild everything when the run is manual, when force_all is set, or when the
workflow file itself changed; otherwise only services whose own directory has
commits in this push. Keeps a one-line dashboard tweak from rebuilding the
~2 GB vision image.

Lives here (not inline in YAML) so it is testable locally:
    EVENT=push python3 scripts/ci_changed_services.py
"""

from __future__ import annotations

import json
import os
import subprocess

SERVICES = [
    "agent_runner", "dashboard", "glasses_bridge", "llm_agent",
    "mobile_gateway", "speaker_verify", "stt", "synapse", "tts",
    "vision", "wake_word",
]
WORKFLOW = ".github/workflows/build-images.yml"


def changed_files() -> list[str]:
    """Files touched by this push. Empty list on any git failure (first commit,
    shallow clone) — callers treat that as 'build everything'."""
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "HEAD^", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def select() -> list[str]:
    if os.environ.get("EVENT") == "workflow_dispatch":
        return SERVICES
    if os.environ.get("FORCE_ALL", "").lower() == "true":
        return SERVICES

    files = changed_files()
    if not files:                      # unknown diff → safest is a full build
        return SERVICES
    if WORKFLOW in files:              # build rules changed → rebuild all
        return SERVICES
    return [s for s in SERVICES
            if any(f.startswith(f"services/{s}/") for f in files)]


if __name__ == "__main__":
    print(f"services={json.dumps(select())}")
