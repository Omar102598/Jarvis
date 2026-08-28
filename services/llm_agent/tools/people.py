"""manage_people — a lightweight relationship memory (CRM-lite).

Remember the people in the user's life — who they are, birthdays, how you last
were in touch, and notes — so JARVIS can answer "when's Sarah's birthday?",
"when did I last talk to Mike?", and (via a Synapse rule) nudge to reconnect.

Redis: people:{USER_ID} — hash of normalized-name → person json:
    {name, relationship, birthday (MM-DD or YYYY-MM-DD), notes, last_contact (ISO), created}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
USER_ID = os.environ.get("JARVIS_USER_ID", "default")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)
_KEY = f"people:{USER_ID}"


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


def _find(name: str) -> tuple[str, dict] | tuple[None, None]:
    n = _norm(name)
    raw = _r.hget(_KEY, n)
    if raw:
        try:
            return n, json.loads(raw)
        except Exception:
            pass
    # loose contains-match on first name
    for field, val in (_r.hgetall(_KEY) or {}).items():
        if n in field or field.split()[0] == n.split()[0]:
            try:
                return field, json.loads(val)
            except Exception:
                continue
    return None, None


@tool
def manage_people(action: str, name: str = "", relationship: str = "",
                  birthday: str = "", notes: str = "") -> str:
    """Remember and recall people in the user's life.

    Actions:
      • "remember" — add/update a person (name required; optional relationship,
        birthday as MM-DD or YYYY-MM-DD, notes — notes append).
      • "get"      — recall what you know about someone.
      • "list"     — list everyone you remember.
      • "contacted"— mark that the user was just in touch with them (updates last_contact).

    Args:
        action: remember | get | list | contacted.
        name: the person's name.
        relationship: e.g. "sister", "coworker", "college friend".
        birthday: MM-DD or YYYY-MM-DD.
        notes: a detail to remember (appended to existing notes).
    """
    action = (action or "").strip().lower()

    if action == "list":
        people = _r.hgetall(_KEY) or {}
        if not people:
            return "I don't have anyone remembered yet. Tell me about someone."
        out = []
        for _, val in sorted(people.items()):
            try:
                p = json.loads(val)
                rel = f" ({p['relationship']})" if p.get("relationship") else ""
                out.append(f"• {p.get('name','')}{rel}")
            except Exception:
                continue
        return f"People I remember ({len(out)}):\n" + "\n".join(out)

    if not name.strip():
        return "Who do you mean? Give me a name."

    if action == "remember":
        field, p = _find(name)
        if not p:
            field, p = _norm(name), {"name": name.strip(),
                                     "created": datetime.now(timezone.utc).isoformat()}
        if relationship.strip():
            p["relationship"] = relationship.strip()
        if birthday.strip():
            p["birthday"] = birthday.strip()
        if notes.strip():
            p["notes"] = ((p.get("notes", "") + " ").strip() + " " + notes.strip()).strip()
        _r.hset(_KEY, field, json.dumps(p))
        return f"Got it — I'll remember {p['name']}" + (
            f", your {p['relationship']}" if p.get("relationship") else "") + "."

    field, p = _find(name)
    if not p:
        return f"I don't have anyone named “{name}” remembered yet."

    if action == "get":
        bits = [p.get("name", "")]
        if p.get("relationship"):
            bits.append(f"— {p['relationship']}")
        lines = [" ".join(bits)]
        if p.get("birthday"):
            lines.append(f"Birthday: {p['birthday']}")
        if p.get("last_contact"):
            lines.append(f"Last in touch: {p['last_contact'][:10]}")
        if p.get("notes"):
            lines.append(f"Notes: {p['notes']}")
        return "\n".join(lines)

    if action == "contacted":
        p["last_contact"] = datetime.now(timezone.utc).isoformat()
        _r.hset(_KEY, field, json.dumps(p))
        return f"Noted you were just in touch with {p['name']}."

    return "Unknown action. Use remember | get | list | contacted."
