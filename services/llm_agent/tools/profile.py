"""User profile tools — read and update personalization preferences stored in Redis."""

import json
import os

import redis
from langchain_core.tools import tool

_r = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    decode_responses=True,
)
_PROFILE_KEY = "user:profile"


def _load_profile() -> dict:
    raw = _r.get(_PROFILE_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_profile(profile: dict) -> None:
    _r.set(_PROFILE_KEY, json.dumps(profile))


@tool
def get_user_profile(key: str = "") -> str:
    """Read the user's stored profile preferences.

    Args:
        key: Dot-separated key path to read, e.g. "preferences.tts_voice".
             Leave empty to return the full profile as JSON.
    """
    profile = _load_profile()
    if not profile:
        return "No profile saved yet. Use update_user_profile to set preferences."

    if not key:
        return json.dumps(profile, indent=2)

    # Walk dot-separated path
    parts = key.split(".")
    node = profile
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return f"Key '{key}' not found in profile."
        node = node[part]
    return json.dumps(node) if isinstance(node, (dict, list)) else str(node)


@tool
def update_user_profile(key: str, value: str) -> str:
    """Update a preference in the user's profile (persisted in Redis across restarts).

    Args:
        key:   Dot-separated key path, e.g. "preferences.response_verbosity"
               or "enabled_plugins".
        value: New value as a JSON string or plain string.
               Lists and dicts must be valid JSON, e.g. '["finance", "calendar"]'.

    Examples:
        update_user_profile("preferences.response_verbosity", "concise")
        update_user_profile("enabled_plugins", '["finance"]')
        update_user_profile("dashboard_layout", '[{"widget":"finance","position":0}]')
    """
    profile = _load_profile()

    # Parse value — try JSON first, fall back to plain string
    try:
        parsed_value = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        parsed_value = value

    # Write at dot-separated path, creating intermediate dicts as needed
    parts = key.split(".")
    node = profile
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = parsed_value

    _save_profile(profile)
    return f"Profile updated: {key} = {value}"
