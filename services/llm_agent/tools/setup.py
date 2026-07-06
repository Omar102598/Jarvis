"""Setup & personalization tools — make Jarvis self-configurable for any user.

get_setup_status  — audit which integrations are configured (env-var PRESENCE
                    only, never values) plus profile completeness, so Jarvis
                    can tell a new user exactly what works and what's missing.
personalize_jarvis — structured interview brief: Jarvis walks the user through
                    filling their profile conversationally, one question at a
                    time, writing answers via update_user_profile.
"""

import json
import os

import redis
from langchain_core.tools import tool

_r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)

# integration → (env vars ALL required, what it unlocks, how to get creds)
_INTEGRATIONS = [
    ("Anthropic (the brain)", ["ANTHROPIC_API_KEY"],
     "everything — Jarvis cannot think without it", "console.anthropic.com"),
    ("Web search", ["TAVILY_API_KEY"],
     "web_search + research/newsletter agents", "tavily.com (free tier)"),
    ("Spotify", ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"],
     "music control", "developer.spotify.com dashboard"),
    ("BankSync (Brad)", ["BANKSYNC_API_KEY"],
     "balances, spending, subscription detection", "banksync.io workspace key"),
    ("Email (Hermes)", ["IMAP_USER", "IMAP_PASSWORD"],
     "inbox triage, package tracking", "Gmail app password: myaccount.google.com/apppasswords"),
    ("Google Maps", ["GOOGLE_MAPS_API_KEY"],
     "directions, travel times, 'when should I leave'", "Google Cloud console"),
    ("Home Assistant", ["HA_URL", "HA_TOKEN"],
     "smart home control + calendar-gap ranking", "HA profile → long-lived token"),
    ("Email sending (SMTP)", ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"],
     "send_email tool", "your mail provider's SMTP settings"),
    ("SMS (Twilio)", ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"],
     "send_sms tool", "twilio.com console"),
    ("GitHub", ["GITHUB_TOKEN"],
     "repo/issue/PR tools + GitHub MCP", "github.com/settings/tokens"),
]

# profile field → (why it matters, example set-phrase)
_PROFILE_FIELDS = [
    ("weight_lbs", "TDEE for grocery/workout planning", "set my weight to 190"),
    ("height_in", "TDEE calculation", "I'm 6 feet tall"),
    ("age", "TDEE calculation", "I'm 25"),
    ("goal", "cutting / maintaining / bulking — shapes meals & training", "my goal is cutting"),
    ("activity_level", "TDEE multiplier when HealthKit absent", "I'm moderately active"),
    ("weekly_budget_usd", "grocery budget ceiling", "set my grocery budget to $150"),
    ("imessage_to", "where agent reports get texted", "my number is +1..."),
    ("classpass_home_location", "where Kai searches for classes", "my ClassPass city is Dallas, TX"),
    ("favorite_meals", "meals Remy builds the week around", "I love ground beef bowls"),
    ("preferred_items", "staples Remy always prefers", "I prefer chicken tenderloins"),
    ("avoid_items", "foods Remy substitutes away", "avoid chicken breasts"),
    ("workout_split", "Apollo's weekday programming", "my split is back/bi, chest/tri, legs/shoulders"),
]


@tool
def get_setup_status() -> str:
    """Audit Jarvis's configuration: which integrations are live, which are
    missing credentials, and how complete the user's profile is.

    Use when the user asks "what's set up?", "what can you do for me?",
    "why isn't X working?", or when onboarding a new user. Reports env-var
    PRESENCE only — never values.
    """
    lines = ["INTEGRATIONS:"]
    for label, keys, unlocks, how in _INTEGRATIONS:
        missing = [k for k in keys if not os.environ.get(k, "").strip()]
        if not missing:
            lines.append(f"  ✅ {label} — live ({unlocks})")
        else:
            lines.append(f"  ❌ {label} — needs {', '.join(missing)} in .env "
                         f"(get it: {how}); unlocks {unlocks}")

    try:
        profile = json.loads(_r.get("user:profile") or "{}")
    except Exception:
        profile = {}
    filled = [f for f, _, _ in _PROFILE_FIELDS if profile.get(f)]
    missing_fields = [(f, why, ex) for f, why, ex in _PROFILE_FIELDS if not profile.get(f)]
    lines.append(f"\nPROFILE: {len(filled)}/{len(_PROFILE_FIELDS)} fields set.")
    for f, why, ex in missing_fields[:8]:
        lines.append(f"  • {f} missing — {why} (say: \"{ex}\")")

    # Live data feeds
    lines.append("\nDATA FEEDS:")
    lines.append("  " + ("✅" if _r.get("user:health:latest") else "❌")
                 + " HealthKit snapshot (iOS app pushes on launch/foreground)")
    lines.append("  " + ("✅" if _r.get("grocery:usual_order") else "❌")
                 + " Usual grocery order (say 'learn my Fresh cart')")
    lines.append("  " + ("✅" if _r.get("workout:plan") else "❌")
                 + " Weekly workout plan (say 'plan my workout week')")

    if missing_fields or any(
        not all(os.environ.get(k, "").strip() for k in keys)
        for _, keys, _, _ in _INTEGRATIONS
    ):
        lines.append("\nOffer to walk the user through setup (personalize_jarvis) "
                     "or explain any single item they ask about.")
    return "\n".join(lines)


@tool
def personalize_jarvis() -> str:
    """Start a conversational setup interview to personalize Jarvis.

    Use when the user says "set yourself up for me", "personalize yourself",
    "let's configure you", or when get_setup_status shows a sparse profile.
    Returns the current profile plus the questions still worth asking —
    then interview the user ONE question at a time (don't dump the whole
    list), saving each answer with update_user_profile as you go.
    """
    try:
        profile = json.loads(_r.get("user:profile") or "{}")
    except Exception:
        profile = {}

    known, to_ask = [], []
    for f, why, ex in _PROFILE_FIELDS:
        if profile.get(f):
            known.append(f"  {f} = {json.dumps(profile[f])[:80]}")
        else:
            to_ask.append(f"  • {f} — {why}")

    return (
        "Current profile:\n" + ("\n".join(known) if known else "  (empty)") +
        "\n\nStill to collect:\n" + ("\n".join(to_ask) if to_ask else "  nothing — fully set!") +
        "\n\nInterview the user conversationally, ONE question at a time, in your "
        "own voice. Save each answer immediately with update_user_profile. Start "
        "with the field that unlocks the most (weight/goal for fitness+meals, "
        "imessage_to for reports). Skip anything they decline. When done, mention "
        "get_setup_status covers API-key integrations too."
    )
