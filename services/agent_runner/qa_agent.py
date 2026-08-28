"""Vega — nightly QA regression agent.

Runs a small suite of golden conversations against the LIVE brain (the same
MQTT path every surface uses: publish ``jarvis/llm/request`` → collect the
``jarvis/tts/{room}/speak`` reply) and checks each response for expected /
forbidden substrings. Complements the static harness (scripts/golden_tasks.py)
and the manual behavioral harness (scripts/eval_conversations.py) by running
unattended every night, remembering yesterday's results, and alerting ONLY on
a pass→fail transition — regressions get caught the night they ship, not when
the user hits them days later.

Suite lives in config/qa_tasks.yml (falls back to a builtin suite):
    cases:
      - name: time
        prompt: "what time is it"
        expect_any: ["am", "pm", ":"]
        forbid: ["error"]

Redis:
    qa:last     json {case_name: "pass"|"fail"} from the previous run
    qa:history  list of run summaries, newest first, capped
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

from base_agent import BaseAgent
from notify import route_notification

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
SUITE_PATH = Path(os.environ.get("QA_TASKS_CONFIG", "/config/qa_tasks.yml"))
CASE_TIMEOUT_S = int(os.environ.get("QA_CASE_TIMEOUT_S", "60"))
# After the first reply chunk, wait this long for more (sentence streaming).
QUIET_S = 2.5

_BUILTIN_CASES = [
    {"name": "time", "prompt": "what time is it",
     "expect_any": ["am", "pm", ":", "o'clock", "morning", "afternoon",
                    "evening", "night", "utc"],
     "forbid": ["error", "i encountered", "not one of my"]},
    {"name": "math", "prompt": "what is seventeen plus twenty-five",
     "expect_any": ["42", "forty-two", "forty two"], "forbid": ["error"]},
    {"name": "identity", "prompt": "who are you",
     "expect_any": ["jarvis", "assistant"],
     "forbid": ["error", "i encountered"]},
    {"name": "weather_tool", "prompt": "what's the weather right now",
     "expect_any": ["°", "degrees", "temperature", "cloud", "sun", "rain",
                    "clear", "wind", "humid"],
     "forbid": ["i encountered an error", "traceback"]},
    {"name": "memory_recall", "prompt": "what do you remember about me",
     "expect_any": ["remember", "you", "•"],
     "forbid": ["traceback", "i encountered an error"]},
]


class QAAgent(BaseAgent):
    async def run(self) -> str:
        cases = self._load_cases()
        results: dict[str, str] = {}
        details: list[str] = []

        client = mqtt.Client()
        replies: dict[str, list[str]] = {}
        finals: dict[str, threading.Event] = {}
        last_chunk: dict[str, float] = {}
        lock = threading.Lock()

        def on_message(_c, _u, msg):
            try:
                body = json.loads(msg.payload)
                room = body.get("room") or msg.topic.split("/")[2]
                with lock:
                    replies.setdefault(room, []).append(body.get("text", ""))
                    last_chunk[room] = time.time()
                    if body.get("is_final", True) and room in finals:
                        finals[room].set()
            except Exception:
                pass

        client.on_message = on_message
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.subscribe("jarvis/tts/+/speak")
        client.loop_start()
        try:
            for case in cases:
                name = case.get("name", "unnamed")
                room = f"qa-{uuid.uuid4().hex[:10]}"
                done = threading.Event()
                with lock:
                    finals[room] = done
                self.log_event("tool", f"case '{name}': {case.get('prompt','')}")
                client.publish("jarvis/llm/request", json.dumps({
                    "text": case.get("prompt", ""), "room": room,
                    "verified": True, "source": "qa",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }))
                # Wait for the final chunk, then a short quiet period in case
                # more streamed sentences follow the flag.
                got_final = done.wait(CASE_TIMEOUT_S)
                if got_final:
                    while time.time() - last_chunk.get(room, 0) < QUIET_S:
                        await asyncio.sleep(0.3)
                with lock:
                    text = " ".join(replies.pop(room, [])).strip()
                    finals.pop(room, None)
                if not text:
                    results[name] = "fail"
                    details.append(f"{name}: NO RESPONSE in {CASE_TIMEOUT_S}s")
                    continue
                low = text.lower()
                expect = [e.lower() for e in case.get("expect_any", [])]
                forbid = [f.lower() for f in case.get("forbid", [])]
                ok = (not expect or any(e in low for e in expect)) and \
                     not any(f in low for f in forbid)
                results[name] = "pass" if ok else "fail"
                if not ok:
                    details.append(f"{name}: FAIL — “{text[:140]}”")
        finally:
            client.loop_stop()
            client.disconnect()

        # ------------------------------------------------ regression diff
        try:
            previous = json.loads(self.r.get("qa:last") or "{}")
        except Exception:
            previous = {}
        regressions = [n for n, res in results.items()
                       if res == "fail" and previous.get(n) == "pass"]
        passed = sum(1 for res in results.values() if res == "pass")
        summary = (f"Vega QA: {passed}/{len(results)} passed."
                   + (f" REGRESSIONS: {', '.join(regressions)}." if regressions else "")
                   + ((" " + " | ".join(details)) if details else ""))

        self.r.set("qa:last", json.dumps(results))
        self.r.lpush("qa:history", json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "results": results, "regressions": regressions,
        }))
        self.r.ltrim("qa:history", 0, 59)

        if regressions:
            route_notification(
                "Vega", f"Overnight checks regressed: {', '.join(regressions)}. "
                        f"({passed}/{len(results)} passing.)",
                title="🔴 Jarvis QA regression", urgency="urgent",
                dedup_key=f"qa:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
        return summary

    def _load_cases(self) -> list[dict]:
        try:
            if SUITE_PATH.exists():
                import yaml
                data = yaml.safe_load(SUITE_PATH.read_text()) or {}
                cases = data.get("cases") or []
                if cases:
                    return cases
        except Exception as exc:
            self.log_event("finding", f"qa_tasks.yml unreadable ({exc}) — builtin suite")
        return _BUILTIN_CASES
