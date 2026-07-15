#!/usr/bin/env python3
"""Behavioral eval harness — golden conversations against the LIVE brain.

Complements golden_tasks.py (which checks compile/config/tool-registration) by
checking the brain actually RESPONDS sensibly: each case sends a prompt to
jarvis/llm/request and checks the spoken response for expected/forbidden
substrings. Forge (or you) can run this after changes to catch behavioral
regressions, not just syntax.

Uses the mosquitto container's pub/sub (no host Python deps). Requires the
stack running. Run: python3 scripts/eval_conversations.py

Exit 0 = all pass, 1 = a regression.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid

# Each case: prompt → the response should contain ANY of expect_any (case-
# insensitive) and NONE of forbid. Keep them robust to phrasing.
CASES = [
    {"name": "time", "prompt": "what time is it",
     "expect_any": ["utc", "am", "pm", ":", "o'clock", "morning", "afternoon", "evening", "night"],
     "forbid": ["error", "i encountered", "not one of my"]},
    {"name": "math", "prompt": "what is seventeen plus twenty-five",
     "expect_any": ["42", "forty-two", "forty two"], "forbid": ["error"]},
    {"name": "identity", "prompt": "who are you",
     "expect_any": ["jarvis", "assistant"], "forbid": ["error", "i encountered"]},
]
TIMEOUT_S = 40


def _sub(topic: str, seconds: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["docker", "exec", "jarvis-mqtt", "mosquitto_sub", "-t", topic, "-W", str(seconds)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


def _pub(topic: str, payload: dict) -> None:
    subprocess.run(
        ["docker", "exec", "jarvis-mqtt", "mosquitto_pub", "-t", topic, "-m", json.dumps(payload)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_case(c: dict) -> tuple[bool, str]:
    room = f"eval-{uuid.uuid4().hex[:6]}"
    sub = _sub(f"jarvis/tts/{room}/speak", TIMEOUT_S)
    time.sleep(1)
    _pub("jarvis/llm/request", {"text": c["prompt"], "room": room, "verified": True})
    try:
        out, _ = sub.communicate(timeout=TIMEOUT_S + 5)
    except subprocess.TimeoutExpired:
        sub.kill()
        out = ""
    text = " ".join(
        json.loads(line).get("text", "")
        for line in (out or "").splitlines() if line.strip().startswith("{")
    ).lower()
    if not text:
        return False, "no response"
    if any(f in text for f in c.get("forbid", [])):
        return False, f"forbidden phrase in: {text[:80]}"
    if not any(e.lower() in text for e in c["expect_any"]):
        return False, f"missing expected; got: {text[:80]}"
    return True, text[:60]


def main() -> int:
    print("Behavioral eval — golden conversations")
    fails = 0
    for c in CASES:
        ok, detail = run_case(c)
        print(f"  {'✅' if ok else '❌'} {c['name']}: {detail}")
        if not ok:
            fails += 1
    print("=" * 50)
    if fails:
        print(f"❌ {fails}/{len(CASES)} eval case(s) failed.")
        return 1
    print(f"✅ All {len(CASES)} eval cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
