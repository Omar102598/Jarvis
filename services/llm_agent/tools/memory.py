"""Persistent memory tools for JARVIS.

Lets JARVIS remember facts about you across conversations and recall them
later. Facts are stored in Redis (durable) as a hash, with simple keyword
search for recall.

    remember("I take the 8:05 train to work")
    recall("train")  ->  "I take the 8:05 train to work"
"""

import json
import os
import time
import uuid

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)

_MEM_KEY = "jarvis:memory"  # hash: fact_id -> json({text, ts})


@tool
def remember(fact: str) -> str:
    """Store a fact about the user for future conversations.

    Use this whenever the user tells you something worth remembering long
    term: preferences, names, schedules, account details, habits, etc.
    Examples: "Remember my wife's name is Sarah", "I'm allergic to peanuts".

    Args:
        fact: The fact to remember, phrased as a standalone statement.
    """
    fact = fact.strip()
    if not fact:
        return "Nothing to remember — the fact was empty."
    fact_id = uuid.uuid4().hex[:12]
    _r.hset(_MEM_KEY, fact_id, json.dumps({"text": fact, "ts": time.time()}))
    return f"Noted. I'll remember that: \"{fact}\""


@tool
def recall(topic: str = "") -> str:
    """Recall facts the user previously asked you to remember.

    Use this when the user references something they told you before, or asks
    "what do you know about X?". Leave topic empty to list everything.

    Args:
        topic: Keyword(s) to search remembered facts. Empty = return all.
    """
    raw = _r.hgetall(_MEM_KEY)
    if not raw:
        return "I don't have anything remembered yet."

    facts = []
    for fact_id, payload in raw.items():
        try:
            facts.append(json.loads(payload))
        except json.JSONDecodeError:
            continue

    topic = topic.strip().lower()
    if topic:
        terms = topic.split()
        facts = [f for f in facts if any(t in f["text"].lower() for t in terms)]

    if not facts:
        return f"I don't have anything remembered about '{topic}'."

    facts.sort(key=lambda f: f.get("ts", 0), reverse=True)
    return "\n".join(f"• {f['text']}" for f in facts[:25])


@tool
def forget(topic: str) -> str:
    """Forget remembered facts matching a topic.

    Args:
        topic: Keyword(s); any remembered fact containing them is deleted.
    """
    topic = topic.strip().lower()
    if not topic:
        return "Please specify what to forget."

    raw = _r.hgetall(_MEM_KEY)
    terms = topic.split()
    removed = 0
    for fact_id, payload in raw.items():
        try:
            text = json.loads(payload)["text"].lower()
        except (json.JSONDecodeError, KeyError):
            continue
        if any(t in text for t in terms):
            _r.hdel(_MEM_KEY, fact_id)
            removed += 1

    return f"Forgot {removed} fact(s) about '{topic}'." if removed else \
        f"Nothing remembered about '{topic}'."
