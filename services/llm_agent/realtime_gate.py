"""Decides whether a voice turn may use OpenAI's Realtime API, and tracks spend.

Realtime is native speech-to-speech: no STT -> LLM -> TTS handoffs, so it lands
around 160-220ms end to end against the ~5s the current pipeline manages. The
catch is that it bills per second of audio in BOTH directions, at roughly
$0.25-0.35/min all-in. Ten minutes of talking a day is $75-105/month against a
current total LLM bill nearer $20 -- so the interesting question was never "is
it faster" but "how do we stop it quietly becoming the whole bill".

Hence this gate. Nothing here talks to OpenAI; it only answers "may this turn
use realtime, and what has it cost so far", so the audio path can be added
later without the risk of an unbounded meter. Every failure mode falls back to
the existing stack rather than blocking a turn -- a voice assistant that goes
silent because Redis is down is worse than one that answers a bit slower.

Costs are ESTIMATED from published rates and audio duration, not read back from
OpenAI, so treat the running total as a guardrail rather than an invoice. It is
deliberately biased to overestimate: capping early is recoverable, discovering
an overspend at the end of the month is not.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

# Published rates, USD per million audio tokens (gpt-realtime 2.1).
AUDIO_INPUT_USD_PER_M = float(os.environ.get("REALTIME_AUDIO_IN_USD_PER_M", "32"))
AUDIO_OUTPUT_USD_PER_M = float(os.environ.get("REALTIME_AUDIO_OUT_USD_PER_M", "64"))

# Audio tokenizes at roughly 600 tokens per minute inbound and ~1200 outbound.
INPUT_TOKENS_PER_SECOND = 10.0
OUTPUT_TOKENS_PER_SECOND = 20.0

# Off unless explicitly switched on: this is the expensive path.
ENABLED = os.environ.get("REALTIME_VOICE_ENABLED", "").strip().lower() in ("1", "true", "yes")
DAILY_BUDGET_USD = float(os.environ.get("REALTIME_DAILY_USD", "1.00"))
MAX_SESSION_SECONDS = float(os.environ.get("REALTIME_MAX_SESSION_S", "120"))

SPEND_KEY_PREFIX = "jarvis:realtime:spend:"
SESSION_KEY = "jarvis:realtime:active_since"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def estimate_cost(seconds_in: float, seconds_out: float) -> float:
    """Estimated USD for a session of the given audio durations."""
    tokens_in = max(0.0, seconds_in) * INPUT_TOKENS_PER_SECOND
    tokens_out = max(0.0, seconds_out) * OUTPUT_TOKENS_PER_SECOND
    return (tokens_in / 1e6) * AUDIO_INPUT_USD_PER_M + \
           (tokens_out / 1e6) * AUDIO_OUTPUT_USD_PER_M


def spend_today(r) -> float:
    """USD spent on realtime so far today. 0.0 if Redis is unreachable."""
    try:
        return float(r.get(SPEND_KEY_PREFIX + _today()) or 0.0)
    except Exception:
        return 0.0


def record_session(r, seconds_in: float, seconds_out: float) -> float:
    """Add a finished session's estimated cost to today's total, returning it.

    Best-effort: a metering failure must not fail the turn the user just had.
    """
    cost = estimate_cost(seconds_in, seconds_out)
    try:
        key = SPEND_KEY_PREFIX + _today()
        pipe = r.pipeline()
        pipe.incrbyfloat(key, cost)
        pipe.expire(key, 60 * 60 * 24 * 8)   # a week of history for the widget
        pipe.execute()
    except Exception:
        pass
    return cost


def should_use_realtime(r, *, has_api_key: bool | None = None) -> tuple[bool, str]:
    """(use_realtime, reason). False always means 'fall back', never 'fail'.

    The reason is returned for both outcomes so the decision shows up in logs
    and the dashboard -- a silent fallback would look identical to realtime
    simply not being wired up, which is exactly the confusion to avoid.
    """
    if not ENABLED:
        return False, "realtime disabled (REALTIME_VOICE_ENABLED unset)"

    if has_api_key is None:
        has_api_key = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    if not has_api_key:
        return False, "no OPENAI_API_KEY configured"

    spent = spend_today(r)
    if spent >= DAILY_BUDGET_USD:
        return False, (f"daily realtime budget reached "
                       f"(${spent:.2f} of ${DAILY_BUDGET_USD:.2f})")

    # One more session at the cap must not blow past the budget, so require
    # room for a full-length session rather than checking only the total.
    worst_case = estimate_cost(MAX_SESSION_SECONDS, MAX_SESSION_SECONDS)
    if spent + worst_case > DAILY_BUDGET_USD:
        return False, (f"a full session would exceed the daily budget "
                       f"(${spent:.2f} + ${worst_case:.2f} > ${DAILY_BUDGET_USD:.2f})")

    return True, f"ok (${spent:.2f} of ${DAILY_BUDGET_USD:.2f} used today)"


def session_guard_expired(started_at: float, now: float | None = None) -> bool:
    """True when a live session has run past MAX_SESSION_SECONDS.

    A realtime socket left open by a crashed client bills for as long as it
    streams, so the caller is expected to poll this and hang up.
    """
    return ((now if now is not None else time.time()) - started_at) > MAX_SESSION_SECONDS
