"""ClassPass tools — control the ClassPass agent via Jarvis conversation.

Lets the user, in natural language:
  * trigger a scan of the ClassPass schedule,
  * review the ranked, recovery-aware suggestions,
  * book a specific class (or the top pick) — the approval side of the flow,
  * manage the favorites allowlist that drives auto-booking.

Bookings that aren't from the favorites allowlist go through this confirmation
path, mirroring the grocery agent's approval design.
"""

import json
import os

import paho.mqtt.publish as mqtt_publish
import redis
from langchain_core.tools import tool

_r = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    decode_responses=True,
)
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

SUGGESTIONS_KEY = "classpass:suggestions"
PENDING_KEY     = "classpass:pending_booking"
FAVORITES_KEY   = "classpass:favorites"
BOOKED_KEY      = "classpass:booked"


def _trigger(params: dict) -> None:
    mqtt_publish.single(
        "jarvis/agents/classpass/trigger",
        json.dumps({"params": params}),
        hostname=MQTT_HOST,
        port=MQTT_PORT,
    )


@tool
def trigger_classpass_scan() -> str:
    """Scan ClassPass now for available classes and rank them by your recovery
    (HRV/sleep/resting-HR), open calendar slots, and favorite studios/instructors.

    Auto-books any favorite whose booking window is open; everything else is
    returned as suggestions you can confirm. Takes a couple of minutes.
    """
    try:
        _trigger({"action": "scan"})
    except Exception as exc:
        return f"Failed to trigger ClassPass scan: {exc}"
    return (
        "Scanning ClassPass now, sir. I'll rank classes by your recovery, calendar, "
        "and favorites, auto-book any favorite that's open, and report back shortly."
    )


@tool
def get_class_suggestions() -> str:
    """Show the latest ranked ClassPass suggestions from the most recent scan,
    including your recovery assessment and which classes fit your calendar.
    """
    raw = _r.get(SUGGESTIONS_KEY)
    if not raw:
        return ("No ClassPass suggestions yet, sir. Say 'scan ClassPass' and I'll "
                "pull the schedule and rank it.")
    try:
        data = json.loads(raw)
    except Exception:
        return "Suggestion data looks corrupted — try 'scan ClassPass' again."

    rec = data.get("recovery", {})
    classes = data.get("classes", [])
    generated = data.get("generated_at", "")[:16].replace("T", " ")

    lines = [
        f"ClassPass suggestions (scanned {generated} UTC):",
        f"Recovery {rec.get('score','?')}/100 ({rec.get('tier','?')}) — "
        f"{rec.get('reason','')}",
        "",
    ]
    if not classes:
        lines.append("No classes matched your filters in the last scan.")
        return "\n".join(lines)

    for i, c in enumerate(classes[:8], 1):
        star = "★" if c.get("is_favorite") else " "
        flags = []
        if not c.get("fits_calendar", True):
            flags.append("calendar conflict")
        if not c.get("booking_open", True):
            flags.append("opens later")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        instr = f" · {c['instructor']}" if c.get("instructor") else ""
        lines.append(
            f"{i}.{star} {c.get('name','?')} @ {c.get('studio','?')}{instr} — "
            f"{c.get('start_human','?')} ({c.get('intensity','?')}){flag_str}"
        )
    lines += ["", "Say 'book the <time> class' or 'book it' for the top pick."]
    return "\n".join(lines)


@tool
def book_class(when_or_studio: str = "") -> str:
    """Book a ClassPass class from the current suggestions.

    Args:
        when_or_studio: Optional hint to pick the class, e.g. '6pm', 'SoulCycle',
            or an instructor name. If omitted, books the top-ranked open class.

    This is the confirmation step for non-favorite classes. The visible browser
    will reserve the class; complete any final confirmation there if prompted.
    """
    raw = _r.get(SUGGESTIONS_KEY)
    if not raw:
        return "No suggestions to book from, sir. Say 'scan ClassPass' first."
    try:
        classes = json.loads(raw).get("classes", [])
    except Exception:
        return "Suggestion data looks corrupted — re-scan ClassPass."

    open_classes = [c for c in classes if c.get("booking_open", True)]
    if not open_classes:
        return "None of the current suggestions have an open booking window, sir."

    chosen = None
    hint = when_or_studio.strip().lower()
    if hint:
        for c in open_classes:
            hay = " ".join(str(c.get(k, "")) for k in
                           ("start_human", "studio", "instructor", "name")).lower()
            if hint in hay:
                chosen = c
                break
        if not chosen:
            return (f"I couldn't find a class matching '{when_or_studio}' in the current "
                    f"suggestions, sir. Try 'get class suggestions' to see the list.")
    else:
        chosen = open_classes[0]

    try:
        _trigger({"action": "book", "class_id": chosen.get("id", "")})
    except Exception as exc:
        return f"Failed to trigger booking: {exc}"

    instr = f" with {chosen['instructor']}" if chosen.get("instructor") else ""
    return (
        f"Booking {chosen.get('name','the class')} at {chosen.get('studio','')}"
        f"{instr}, {chosen.get('start_human','')}, sir. "
        f"The browser will reserve it — I'll confirm when it's done."
    )


@tool
def manage_classpass_favorites(action: str, studio: str = "", class_type: str = "",
                               instructor: str = "", auto_book: bool = True) -> str:
    """Manage the favorites allowlist that drives ClassPass auto-booking.

    Args:
        action: 'list', 'add', or 'remove'.
        studio: Studio name to match (substring, case-insensitive). Optional.
        class_type: Class name/type to match, e.g. 'spin', 'vinyasa'. Optional.
        instructor: Instructor name to match. Optional.
        auto_book: If True (default), a matching favorite is booked automatically
            the moment its window opens. If False, it's only prioritised in
            suggestions and you'll be asked to confirm.

    At least one of studio/class_type/instructor is required for add/remove.
    """
    try:
        favorites = json.loads(_r.get(FAVORITES_KEY) or "[]")
    except Exception:
        favorites = []

    action = action.strip().lower()

    if action == "list":
        if not favorites:
            return ("No ClassPass favorites set, sir. Add one like: 'favorite SoulCycle "
                    "spin classes and auto-book them'.")
        lines = ["ClassPass favorites:"]
        for f in favorites:
            m = f.get("match", {})
            parts = [f"{k}={v}" for k, v in m.items() if v]
            mode = "auto-book" if f.get("auto_book") else "suggest only"
            lines.append(f"  • {', '.join(parts) or 'any'} ({mode})")
        return "\n".join(lines)

    if action not in ("add", "remove"):
        return "Action must be 'list', 'add', or 'remove', sir."

    match = {"studio": studio.strip(), "class_type": class_type.strip(),
             "instructor": instructor.strip()}
    if not any(match.values()):
        return "Specify a studio, class type, or instructor for the favorite, sir."

    fav_id = "-".join(v.lower() for v in match.values() if v).replace(" ", "-")

    if action == "add":
        favorites = [f for f in favorites if f.get("id") != fav_id]  # de-dupe
        favorites.append({"id": fav_id, "match": match, "auto_book": bool(auto_book)})
        _r.set(FAVORITES_KEY, json.dumps(favorites))
        mode = "auto-book when the window opens" if auto_book else "prioritise in suggestions"
        desc = ", ".join(v for v in match.values() if v)
        return f"Added favorite: {desc}. I'll {mode}, sir."

    # remove
    before = len(favorites)
    favorites = [f for f in favorites if f.get("id") != fav_id]
    _r.set(FAVORITES_KEY, json.dumps(favorites))
    if len(favorites) < before:
        return f"Removed that favorite, sir. {len(favorites)} remaining."
    return "I couldn't find a matching favorite to remove, sir. Try 'list favorites'."


SEARCH_KEY   = "classpass:search_result"
WAITLIST_KEY = "classpass:waitlist"


def _find_class_by_hint(hint: str):
    """Find a class dict matching a hint from the latest search results, then suggestions."""
    hint = (hint or "").strip().lower()
    for key in (SEARCH_KEY, SUGGESTIONS_KEY):
        raw = _r.get(key)
        if not raw:
            continue
        try:
            classes = json.loads(raw).get("classes", [])
        except Exception:
            continue
        if not hint and classes:
            return classes[0]
        for c in classes:
            hay = " ".join(str(c.get(k, "")) for k in
                           ("start_human", "studio", "instructor", "name")).lower()
            if hint and hint in hay:
                return c
    return None


@tool
def search_classpass(studio: str = "", day: str = "") -> str:
    """Search ClassPass for classes at a SPECIFIC studio and/or on a SPECIFIC day.

    Use THIS (not trigger_classpass_scan) whenever the user asks about a particular
    studio or day — e.g. "what times does CorePower have Friday?" or "check SoulCycle
    tomorrow". Returns the actual class times so you can answer directly.

    Args:
        studio: Studio name to filter to, e.g. 'CorePower Yoga'. Optional.
        day: Day to check, e.g. 'Friday', 'tomorrow', 'Jul 5'. Optional (defaults to today).
    """
    import time
    _r.delete(SEARCH_KEY)  # so we only return a FRESH result
    _trigger({"action": "search", "studio": studio, "day": day})
    for _ in range(45):  # agent scrapes (+day nav) + LLM-parses, ~20-35s cold
        raw = _r.get(SEARCH_KEY)
        if raw:
            try:
                classes = json.loads(raw).get("classes", [])
            except Exception:
                break
            where = f" at {studio}" if studio else ""
            if not classes:
                when = f" for {day}" if day else ""
                return (f"No classes found{where}{when} on ClassPass, sir — the studio may not "
                        f"be on ClassPass in your area, or has nothing scheduled that day.")
            # Each class line shows its real date (start_human), so the day is self-evident.
            lines = [f"Classes{where}, sir:"]
            for c in classes[:12]:
                flag = "" if c.get("booking_open", True) else "  (full — waitlist available)"
                instr = f" · {c['instructor']}" if c.get("instructor") else ""
                lines.append(f"  {c.get('start_human','?')} — {c.get('name','?')}{instr}{flag}")
            lines.append("Say 'book the <time>' or 'join the waitlist for the <time>'.")
            return "\n".join(lines)
        time.sleep(1)
    return "The ClassPass search is still running, sir — ask me for the results in a few seconds."


@tool
def join_classpass_waitlist(when_or_studio: str = "", auto_book: bool = False) -> str:
    """Join the ClassPass waitlist for a FULL class, optionally auto-booking when a spot opens.

    Waitlists for popular classes are competitive — spots open and vanish fast. Kai can
    watch the waitlisted class and grab the spot the instant it frees up.

    IMPORTANT: waitlists are competitive, so BEFORE setting auto_book=True, ASK the user
    whether they want Kai to AUTOMATICALLY book it the moment a spot opens (vs. just being
    alerted). Pass their answer as auto_book.

    Args:
        when_or_studio: Which class — a time / studio / instructor hint, e.g. '6pm' or 'SoulCycle'.
        auto_book: True = Kai auto-books the instant a spot opens; False = Kai just alerts you.
            Ask the user which they want before choosing True.
    """
    chosen = _find_class_by_hint(when_or_studio)
    if not chosen:
        return ("I couldn't find that class to waitlist, sir. Search or scan ClassPass first, "
                "then tell me which class.")
    _trigger({"action": "waitlist_join", "class_id": chosen.get("id", ""), "auto_book": bool(auto_book)})
    instr = f" with {chosen['instructor']}" if chosen.get("instructor") else ""
    mode = ("and auto-book it the instant a spot opens"
            if auto_book else "and alert you the instant a spot opens")
    return (f"Joining the waitlist for {chosen.get('name','the class')} at {chosen.get('studio','')}"
            f"{instr} ({chosen.get('start_human','')}) {mode}, sir. I'll confirm once you're on it.")
