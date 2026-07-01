"""ClassPass Agent — v1.0

Health-aware fitness-class discovery and booking on ClassPass, built on the same
persistent-session browser automation as the grocery agent.

What it does
------------
  * Scans your ClassPass schedule via the logged-in Mac Bridge browser session.
  * Ranks available classes by three signals:
      - Favorites   — studios / instructors / class types you've marked as favorites.
      - Recovery    — HealthKit HRV / resting-HR / sleep / today's workout load steer
                      the appropriate intensity (hard day vs. active-recovery day).
      - Calendar    — only classes that fit an open slot around your calendar events
                      are surfaced; conflicts are heavily penalised.
  * Auto-books a favorite the moment its booking window opens (allowlist only);
    everything else is held as a suggestion for you to confirm ("book the 6pm class").
  * Fires a proactive alert (via the ambient hook) when a favorite opens or is booked.

Login model
-----------
ClassPass requires authentication. Following the grocery-agent precedent, you log in
*once* in the visible Mac Bridge browser; the session is persisted to
``~/.jarvis/browser_state.json`` via ``/browser/save-state`` and reused on every run.

Modes
-----
  scan  (default)          — discover + rank + auto-book favorites + store suggestions.
  book  (action="book")    — book a specific class by id from the stored suggestions.

Note on selectors
-----------------
ClassPass ships a React SPA whose class names are hashed and change over time. Rather
than depend on brittle CSS selectors, the scanner reads the rendered page text and uses
the LLM to extract a structured class list — the same robust approach the grocery agent
uses as its price fallback. The booking click path tries several resilient selectors
plus a text-match fallback ("Book", "Reserve").
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import aiohttp

from base_agent import BaseAgent
from llm_helper import complete

MAC_BRIDGE_URL = os.environ.get("MAC_BRIDGE_URL", "http://host.docker.internal:7777")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# Redis keys
FAVORITES_KEY   = "classpass:favorites"        # JSON list of favorite rules
SUGGESTIONS_KEY = "classpass:suggestions"      # JSON ranked suggestions from last scan
PENDING_KEY     = "classpass:pending_booking"  # top suggestion awaiting confirmation
BOOKED_KEY      = "classpass:booked"           # list of booked class records (history/dedup)
# NB: "classpass:seen" (favorite-alert dedup) is owned by the ambient agent.

# Default ClassPass entry point (overridable via profile.classpass_schedule_url)
DEFAULT_SCHEDULE_URL = "https://classpass.com/search"


def _log(msg: str) -> None:
    print(f"[ClassPassAgent] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Intensity taxonomy — maps class-type keywords to an intensity tier
# ---------------------------------------------------------------------------
_INTENSITY_KEYWORDS = {
    "low": [
        "yoga", "pilates", "barre", "stretch", "mobility", "restorative", "yin",
        "meditation", "breath", "recovery", "foam", "gentle", "walk", "tai chi",
    ],
    "moderate": [
        "strength", "sculpt", "weights", "lift", "dance", "reformer", "boxing",
        "kickbox", "rowing", "row", "circuit", "megaformer", "total body",
    ],
    "high": [
        "hiit", "bootcamp", "cycle", "cycling", "spin", "spinning", "run", "running",
        "crossfit", "interval", "tabata", "conditioning", "assault", "burn", "shred",
        "power", "amrap",
    ],
}

# Which intensities are appropriate at each recovery tier
_TIER_ALLOWED = {
    "low":      {"low"},
    "moderate": {"low", "moderate"},
    "high":     {"low", "moderate", "high"},
}


def _classify_intensity(class_name: str, class_type: str = "") -> str:
    text = f"{class_name} {class_type}".lower()
    for tier in ("high", "moderate", "low"):  # prefer the strongest signal
        if any(kw in text for kw in _INTENSITY_KEYWORDS[tier]):
            return tier
    return "moderate"  # unknown → treat as moderate


# ---------------------------------------------------------------------------
# Profile / favorites
# ---------------------------------------------------------------------------
def _load_profile(r) -> dict:
    try:
        profile = json.loads(r.get("user:profile") or "{}")
    except Exception:
        profile = {}
    return {
        "classpass_home_location": "",
        "classpass_schedule_url":  DEFAULT_SCHEDULE_URL,
        "classpass_preferred_times": [],   # e.g. ["06:00-09:00", "17:00-20:00"]
        "classpass_lookahead_days": 2,
        "imessage_to": "",
        **profile,
    }


def _load_favorites(r) -> list[dict]:
    """Favorite rules: [{id, match:{studio, class_type, instructor}, auto_book}]."""
    try:
        favs = json.loads(r.get(FAVORITES_KEY) or "[]")
        return favs if isinstance(favs, list) else []
    except Exception:
        return []


def _matches_favorite(cls: dict, fav: dict) -> bool:
    """A class matches a favorite if every non-empty match field is a substring."""
    m = fav.get("match", {})
    for field_name, redis_field in (("studio", "studio"),
                                    ("class_type", "name"),
                                    ("instructor", "instructor")):
        want = str(m.get(field_name, "")).strip().lower()
        if want and want not in str(cls.get(redis_field, "")).lower():
            return False
    # At least one field must be specified, else it would match everything
    return any(str(m.get(k, "")).strip() for k in ("studio", "class_type", "instructor"))


def _favorite_for(cls: dict, favorites: list[dict]) -> Optional[dict]:
    for fav in favorites:
        if _matches_favorite(cls, fav):
            return fav
    return None


# ---------------------------------------------------------------------------
# Recovery scoring from HealthKit snapshots
# ---------------------------------------------------------------------------
def _load_health(r) -> tuple[dict, dict]:
    """Return (latest_snapshot, baseline_averages) from Redis HealthKit history."""
    try:
        latest = json.loads(r.get("user:health:latest") or "{}")
    except Exception:
        latest = {}
    try:
        hist = r.lrange("user:health:history", 1, 14) or []
    except Exception:
        hist = []

    def _avg(key: str) -> Optional[float]:
        vals = []
        for item in hist:
            try:
                snap = json.loads(item)
            except Exception:
                continue
            if snap.get(key) is not None:
                vals.append(float(snap[key]))
        return sum(vals) / len(vals) if vals else None

    baseline = {
        "hrv_ms": _avg("hrv_ms"),
        "resting_heart_rate": _avg("resting_heart_rate"),
        "sleep_hours": _avg("sleep_hours"),
    }
    return latest, baseline


def _recovery_assessment(latest: dict, baseline: dict) -> dict:
    """Compute a 0-100 recovery score and a tier (low/moderate/high) with a reason."""
    score = 65.0  # neutral-moderate default
    reasons: list[str] = []

    hrv, hrv_base = latest.get("hrv_ms"), baseline.get("hrv_ms")
    if hrv is not None and hrv_base:
        delta_pct = (float(hrv) - hrv_base) / hrv_base * 100
        score += max(-20, min(20, delta_pct * 0.8))
        if delta_pct <= -12:
            reasons.append(f"HRV {float(hrv):.0f}ms is below your {hrv_base:.0f}ms baseline")
        elif delta_pct >= 12:
            reasons.append(f"HRV {float(hrv):.0f}ms is above baseline — well recovered")

    rhr, rhr_base = latest.get("resting_heart_rate"), baseline.get("resting_heart_rate")
    if rhr is not None and rhr_base:
        delta = float(rhr) - rhr_base
        score -= max(-15, min(15, delta * 1.5))
        if delta >= 6:
            reasons.append(f"resting HR {float(rhr):.0f} is elevated vs {rhr_base:.0f}")

    sleep, sleep_base = latest.get("sleep_hours"), baseline.get("sleep_hours")
    if sleep is not None:
        if sleep < 6:
            score -= 15
            reasons.append(f"only {float(sleep):.1f}h sleep last night")
        elif sleep_base and sleep >= sleep_base:
            score += 5

    # Already trained hard today → bias toward recovery
    workouts = latest.get("workouts_today") or 0
    minutes = latest.get("workout_minutes_today") or 0
    if workouts and minutes and float(minutes) >= 45:
        score -= 15
        reasons.append(f"already {int(float(minutes))} min of training logged today")

    score = max(0, min(100, score))
    if score < 45:
        tier = "low"
    elif score < 70:
        tier = "moderate"
    else:
        tier = "high"

    if not reasons:
        reasons.append("metrics near your baseline")

    return {
        "score": round(score),
        "tier": tier,
        "reason": "; ".join(reasons),
        "allowed_intensities": sorted(_TIER_ALLOWED[tier]),
    }


# ---------------------------------------------------------------------------
# Calendar busy intervals (Home Assistant calendar API)
# ---------------------------------------------------------------------------
async def _fetch_busy_intervals(
    session: aiohttp.ClientSession, days: int
) -> list[tuple[datetime, datetime]]:
    """Return list of (start, end) UTC busy intervals from HA calendars."""
    if not HA_TOKEN:
        _log("  No HA_TOKEN set — skipping calendar-gap filtering.")
        return []

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    busy: list[tuple[datetime, datetime]] = []

    # Discover calendar entities, then pull events from each.
    try:
        async with session.get(
            f"{HA_URL}/api/calendars",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            calendars = await resp.json() if resp.status == 200 else []
    except Exception as exc:
        _log(f"  Calendar discovery failed: {exc}")
        return []

    cal_ids = [c.get("entity_id") for c in calendars if c.get("entity_id")] or ["calendar.personal"]

    for cal in cal_ids:
        try:
            async with session.get(
                f"{HA_URL}/api/calendars/{cal}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                params={"start": now.isoformat(), "end": end.isoformat()},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                events = await resp.json() if resp.status == 200 else []
        except Exception:
            continue
        for e in events:
            s = (e.get("start") or {}).get("dateTime")
            en = (e.get("end") or {}).get("dateTime")
            if not s or not en:
                continue  # all-day events don't block a class slot
            try:
                busy.append((
                    datetime.fromisoformat(s.replace("Z", "+00:00")),
                    datetime.fromisoformat(en.replace("Z", "+00:00")),
                ))
            except Exception:
                continue
    _log(f"  Loaded {len(busy)} busy calendar interval(s).")
    return busy


def _fits_calendar(
    start: datetime, busy: list[tuple[datetime, datetime]], class_minutes: int = 75
) -> bool:
    """A class fits if its window (+15 min travel buffer each side) is free."""
    buf = timedelta(minutes=15)
    cls_start = start - buf
    cls_end = start + timedelta(minutes=class_minutes) + buf
    for b_start, b_end in busy:
        if cls_start < b_end and b_start < cls_end:
            return False
    return True


def _in_preferred_times(start: datetime, windows: list[str]) -> bool:
    """True if start (local) falls in any 'HH:MM-HH:MM' window, or no windows set."""
    if not windows:
        return True
    local = start.astimezone()
    for w in windows:
        try:
            a, b = w.split("-")
            ah, am = map(int, a.split(":"))
            bh, bm = map(int, b.split(":"))
            if time(ah, am) <= local.time() <= time(bh, bm):
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Mac Bridge browser helpers
# ---------------------------------------------------------------------------
async def _bridge_post(session, path: str, payload: dict, timeout: int = 60) -> dict:
    try:
        async with session.post(
            f"{MAC_BRIDGE_URL}{path}", json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            return await resp.json()
    except asyncio.TimeoutError:
        return {"error": f"Timeout calling {path}"}
    except Exception as exc:
        return {"error": str(exc)}


async def _bridge_get(session, path: str, timeout: int = 60) -> dict:
    try:
        async with session.get(
            f"{MAC_BRIDGE_URL}{path}",
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            return await resp.json()
    except asyncio.TimeoutError:
        return {"error": f"Timeout calling {path}"}
    except Exception as exc:
        return {"error": str(exc)}


async def _check_bridge(session) -> bool:
    try:
        async with session.get(
            f"{MAC_BRIDGE_URL}/health",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            return (await resp.json()).get("status") == "ok"
    except Exception as exc:
        _log(f"Bridge health check failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Class scraping — read rendered page text, LLM-parse into structured classes
# ---------------------------------------------------------------------------
@dataclass
class ClassOffer:
    id: str
    studio: str
    name: str
    instructor: str
    start_iso: str
    start_human: str
    spots: str = ""
    booking_open: bool = True
    intensity: str = "moderate"
    is_favorite: bool = False
    auto_book: bool = False
    fits_calendar: bool = True
    score: float = 0.0
    reason: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


async def _scrape_classes(session, profile: dict) -> list[dict]:
    """Navigate ClassPass and return raw class dicts parsed from the page."""
    url = profile.get("classpass_schedule_url") or DEFAULT_SCHEDULE_URL
    location = profile.get("classpass_home_location", "")
    _log(f"Navigating ClassPass: {url}")

    nav = await _bridge_post(
        session, "/browser/navigate",
        {"url": url, "wait_until": "networkidle", "timeout_ms": 45000},
        timeout=60,
    )
    if "error" in nav:
        _log(f"  ✗ Navigation failed: {nav['error']}")
        return []
    await asyncio.sleep(6)

    # Detect a logged-out state so we can tell the user to sign in once.
    page = await _bridge_get(session, "/browser/read", timeout=25)
    text = str(page.get("text") or "")
    if _looks_logged_out(text):
        _log("  ✗ ClassPass appears logged out.")
        return [{"__login_required__": True}]

    # Pull as much rendered class text as possible (schedule cards).
    extract_js = """
    (function() {
        function grab(sel) {
            return Array.from(document.querySelectorAll(sel))
                .map(function(el){ return (el.innerText||'').trim(); })
                .filter(function(t){ return t.length > 8; });
        }
        var cards = grab('[class*="ClassCard"], [class*="class-card"], [data-testid*="class"], article, li');
        var uniq = [...new Set(cards)];
        return uniq.slice(0, 60).join('\\n---\\n');
    })();
    """
    js = await _bridge_post(session, "/browser/js", {"script": extract_js}, timeout=20)
    blob = str(js.get("result") or "")
    if len(blob) < 40:  # fall back to whole-page text
        blob = text[:8000]

    return await _llm_parse_classes(blob, location)


def _looks_logged_out(text: str) -> bool:
    t = text.lower()
    signals = ["log in to classpass", "sign up for classpass", "welcome back",
               "log in / sign up", "create an account"]
    logged_in_signals = ["credits", "my reservations", "upcoming", "book", "reserve"]
    if any(s in t for s in signals) and not any(s in t for s in logged_in_signals):
        return True
    return False


async def _llm_parse_classes(page_blob: str, location: str) -> list[dict]:
    """Use the LLM to turn messy schedule text into structured class dicts."""
    if not page_blob.strip():
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    system = (
        "You extract fitness class listings from raw ClassPass schedule text. "
        f"Today is {today}. Location context: {location or 'unknown'}.\n"
        "Return ONLY a JSON array. Each element:\n"
        '{"studio": str, "name": str, "instructor": str, '
        '"start_iso": "YYYY-MM-DDTHH:MM:00" (local, infer date/time from text), '
        '"start_human": str, "spots": str, "booking_open": bool}\n'
        "booking_open is false if the text says the booking window is not yet open "
        "(e.g. 'opens', 'available soon', 'waitlist only'). Skip anything that is not a "
        "bookable class. If nothing parseable, return []. No prose, no markdown."
    )
    resp = await complete(system=system, user=page_blob[:7000], max_tokens=1500)
    try:
        match = re.search(r"\[.*\]", resp or "", re.DOTALL)
        if not match:
            return []
        data = json.loads(match.group())
        return data if isinstance(data, list) else []
    except Exception:
        _log("  ⚠ LLM class parse failed.")
        return []


# ---------------------------------------------------------------------------
# Booking automation
# ---------------------------------------------------------------------------
async def _book_class_on_page(session, offer: ClassOffer) -> tuple[bool, str]:
    """Best-effort: navigate to the class and click the book/reserve button."""
    if offer.url:  # navigate straight to the class page when we captured a URL
        nav = await _bridge_post(session, "/browser/navigate", {"url": offer.url}, timeout=45)
        if "error" in nav:
            return False, f"Could not open class page: {nav['error']}"
        await asyncio.sleep(5)

    # Try to find and click the matching class card by its human time + studio,
    # then click the book/reserve control.
    book_js = """
    (function() {
        var needle = %s;
        var cards = Array.from(document.querySelectorAll(
            '[class*="ClassCard"], [class*="class-card"], article, li, div'
        ));
        var card = cards.find(function(c){
            var t = (c.innerText||'').toLowerCase();
            return needle.every(function(n){ return t.includes(n); });
        });
        var scope = card || document;
        var selectors = [
            'button[data-testid*="book"]',
            'button[data-testid*="reserve"]',
            'button[aria-label*="Book"]',
            'a[href*="reserve"]',
        ];
        for (var s of selectors) {
            var btn = scope.querySelector(s);
            if (btn) { btn.click(); return 'clicked:' + s; }
        }
        var btns = Array.from(scope.querySelectorAll('button, a'))
            .filter(function(b){ return /\\b(book|reserve)\\b/i.test(b.innerText||''); });
        if (btns.length) { btns[0].click(); return 'clicked:text_match'; }
        return 'no_book_button';
    })();
    """ % json.dumps([offer.studio.lower()[:12], offer.start_human.lower()[:8]])

    res = await _bridge_post(session, "/browser/js", {"script": book_js}, timeout=20)
    val = str(res.get("result", "")).lower()
    if "clicked" not in val:
        return False, f"Book button not found ({val})."
    await asyncio.sleep(3)

    # Confirm modal (ClassPass often shows a "Confirm" / "Reserve" confirmation step).
    confirm_js = """
    (function() {
        var btns = Array.from(document.querySelectorAll('button'))
            .filter(function(b){
                return /\\b(confirm|reserve|book now|yes)\\b/i.test(b.innerText||'');
            });
        if (btns.length) { btns[0].click(); return 'confirmed'; }
        return 'no_confirm';
    })();
    """
    conf = await _bridge_post(session, "/browser/js", {"script": confirm_js}, timeout=15)
    await asyncio.sleep(3)
    return True, f"Booking submitted ({val}; {conf.get('result','')})."


# ---------------------------------------------------------------------------
# Report / delivery
# ---------------------------------------------------------------------------
async def _send_imessage(session, phone: str, message: str) -> None:
    if not phone:
        return
    safe = message.replace("\\", "\\\\").replace('"', "'").replace("\n", "\\n")
    script = (
        f'tell application "Messages"\n'
        f'    set s to 1st service whose service type = iMessage\n'
        f'    set b to buddy "{phone}" of s\n'
        f'    send "{safe}" to b\n'
        f'end tell'
    )
    await _bridge_post(session, "/applescript", {"script": script, "timeout": 30})


def _push_to_surfaces(text: str, title: str = "ClassPass") -> None:
    try:
        import paho.mqtt.publish as mqtt_pub
        mqtt_pub.single(
            "jarvis/surfaces/iphone/push",
            json.dumps({"text": text, "title": title}),
            hostname=MQTT_HOST, port=MQTT_PORT,
        )
    except Exception as exc:
        _log(f"  ✗ surface push failed: {exc}")


# ---------------------------------------------------------------------------
# Main agent
# ---------------------------------------------------------------------------
class ClasspassAgent(BaseAgent):
    async def run(self) -> str:
        action = (self.params or {}).get("action", "scan")
        if action == "book":
            return await self._run_book()
        return await self._run_scan()

    # -- scan -----------------------------------------------------------
    async def _run_scan(self) -> str:
        profile   = _load_profile(self.r)
        favorites = _load_favorites(self.r)
        latest, baseline = _load_health(self.r)
        recovery  = _recovery_assessment(latest, baseline)
        days      = int(profile.get("classpass_lookahead_days", 2))

        _log(f"=== ClassPass scan | recovery {recovery['score']}/{recovery['tier']} "
             f"| {len(favorites)} favorite rule(s) ===")

        async with aiohttp.ClientSession() as session:
            if not await _check_bridge(session):
                return f"Mac Bridge unreachable at {MAC_BRIDGE_URL}. Cannot scan ClassPass."

            raw = await _scrape_classes(session, profile)
            if raw and raw[0].get("__login_required__"):
                msg = ("ClassPass needs a sign-in, sir. Open the Jarvis browser, log in to "
                       "ClassPass once, and I'll persist the session for future scans.")
                _push_to_surfaces(msg)
                return msg
            if not raw:
                return "No ClassPass classes found on the schedule page this scan, sir."

            busy = await _fetch_busy_intervals(session, days)

            offers = self._rank(raw, favorites, recovery, busy, profile)

            # Auto-book favorites whose window is open and not already booked.
            booked_now: list[ClassOffer] = []
            for o in offers:
                if o.is_favorite and o.auto_book and o.booking_open and not self._already_booked(o):
                    ok, note = await _book_class_on_page(session, o)
                    if ok:
                        self._record_booked(o)
                        booked_now.append(o)
                        _log(f"  ✓ Auto-booked favorite: {o.studio} {o.start_human}")
                    else:
                        _log(f"  ✗ Auto-book failed for {o.studio}: {note}")
                    await asyncio.sleep(2)

            await _bridge_post(session, "/browser/save-state", {}, timeout=15)

            # Persist suggestions + a single pending confirmation (top non-favorite/unbooked).
            self._store_suggestions(offers, recovery)
            pending = next(
                (o for o in offers if o.booking_open and not self._already_booked(o)),
                None,
            )
            if pending:
                self.r.set(PENDING_KEY, json.dumps(pending.to_dict()), ex=86400)

            # Auto-booked favorites are actions → confirm them directly.
            # Favorite-*open* notifications are owned by the ambient loop
            # (which reads these suggestions), so the scan does not also alert.
            phone = profile.get("imessage_to", "")
            if booked_now:
                summary = "Booked for you: " + "; ".join(
                    f"{o.name} at {o.studio} ({o.start_human})" for o in booked_now
                )
                await _send_imessage(session, phone, summary)
                _push_to_surfaces(summary, title="ClassPass")

        return self._build_report(offers, recovery, booked_now)

    # -- book by id (approval flow for non-favorites) -------------------
    async def _run_book(self) -> str:
        target_id = (self.params or {}).get("class_id", "")
        raw = self.r.get(SUGGESTIONS_KEY)
        if not raw:
            return "No ClassPass suggestions in memory, sir. Say 'scan ClassPass' first."
        data = json.loads(raw)
        classes = data.get("classes", [])
        chosen = None
        if target_id:
            chosen = next((c for c in classes if c.get("id") == target_id), None)
        if not chosen:
            chosen = next((c for c in classes if c.get("booking_open")), None)
        if not chosen:
            return "I couldn't find that class in the current suggestions, sir."

        offer = ClassOffer(**{k: chosen.get(k) for k in ClassOffer.__annotations__ if k in chosen})
        async with aiohttp.ClientSession() as session:
            if not await _check_bridge(session):
                return f"Mac Bridge unreachable at {MAC_BRIDGE_URL}. Cannot book."
            ok, note = await _book_class_on_page(session, offer)
            await _bridge_post(session, "/browser/save-state", {}, timeout=15)
            profile = _load_profile(self.r)
            if ok:
                self._record_booked(offer)
                self.r.delete(PENDING_KEY)
                msg = (f"Booked {offer.name} at {offer.studio}, {offer.start_human}, sir. "
                       f"Confirm any final step in the browser if prompted.")
                await _send_imessage(session, profile.get("imessage_to", ""), msg)
                _push_to_surfaces(msg)
                return msg
            return f"I couldn't complete the booking, sir: {note}"

    # -- ranking --------------------------------------------------------
    def _rank(self, raw, favorites, recovery, busy, profile) -> list[ClassOffer]:
        allowed = set(recovery["allowed_intensities"])
        prefs = profile.get("classpass_preferred_times", [])
        offers: list[ClassOffer] = []

        for c in raw:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            start_iso = c.get("start_iso", "")
            try:
                start_dt = datetime.fromisoformat(start_iso)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.astimezone()
            except Exception:
                start_dt = None

            intensity = _classify_intensity(c.get("name", ""), c.get("class_type", ""))
            fav = _favorite_for(c, favorites)
            fits_cal = _fits_calendar(start_dt.astimezone(timezone.utc), busy) if start_dt else True
            in_pref = _in_preferred_times(start_dt, prefs) if start_dt else True

            score = 0.0
            reasons = []
            if fav:
                score += 100
                reasons.append("favorite")
            if intensity in allowed:
                score += 30
                reasons.append(f"{intensity}-intensity suits recovery")
            else:
                score -= 25
                reasons.append(f"{intensity}-intensity vs {recovery['tier']} recovery")
            if fits_cal:
                score += 25
            else:
                score -= 60
                reasons.append("calendar conflict")
            if in_pref:
                score += 10
            else:
                score -= 10
            if not c.get("booking_open", True):
                score -= 15
                reasons.append("window not open yet")

            oid = _class_id(c)
            offers.append(ClassOffer(
                id=oid,
                studio=str(c.get("studio", "")).strip(),
                name=str(c.get("name", "")).strip(),
                instructor=str(c.get("instructor", "")).strip(),
                start_iso=start_iso,
                start_human=str(c.get("start_human", "")).strip(),
                spots=str(c.get("spots", "")).strip(),
                booking_open=bool(c.get("booking_open", True)),
                intensity=intensity,
                is_favorite=fav is not None,
                auto_book=bool(fav and fav.get("auto_book")),
                fits_calendar=fits_cal,
                score=round(score, 1),
                reason=", ".join(reasons),
                url=str(c.get("url", "")).strip(),
            ))

        offers.sort(key=lambda o: o.score, reverse=True)
        return offers

    # -- persistence helpers -------------------------------------------
    def _store_suggestions(self, offers: list[ClassOffer], recovery: dict) -> None:
        self.r.set(SUGGESTIONS_KEY, json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "recovery": recovery,
            "classes": [o.to_dict() for o in offers[:15]],
        }), ex=86400)

    def _already_booked(self, o: ClassOffer) -> bool:
        try:
            booked = json.loads(self.r.get(BOOKED_KEY) or "[]")
        except Exception:
            booked = []
        return any(b.get("id") == o.id for b in booked)

    def _record_booked(self, o: ClassOffer) -> None:
        try:
            booked = json.loads(self.r.get(BOOKED_KEY) or "[]")
        except Exception:
            booked = []
        booked.insert(0, {**o.to_dict(), "booked_at": datetime.now(timezone.utc).isoformat()})
        self.r.set(BOOKED_KEY, json.dumps(booked[:100]))

    # -- report ---------------------------------------------------------
    def _build_report(self, offers, recovery, booked_now) -> str:
        lines = [
            "═══════════════════════════════════════════",
            "     J·A·R·V·I·S  CLASSPASS REPORT",
            f"     {datetime.now().strftime('%A, %B %-d')}",
            "═══════════════════════════════════════════",
            "",
            f"RECOVERY: {recovery['score']}/100 ({recovery['tier']})",
            f"  {recovery['reason']}",
            f"  Suggested intensity: {', '.join(recovery['allowed_intensities'])}",
            "",
        ]
        if booked_now:
            lines.append("AUTO-BOOKED (favorites)")
            for o in booked_now:
                lines.append(f"  ✓ {o.name} — {o.studio} — {o.start_human}")
            lines.append("")

        top = [o for o in offers if not (o.is_favorite and o.auto_book and o.booking_open)][:6]
        lines.append("TOP SUGGESTIONS")
        if not top:
            lines.append("  (none matched your filters this scan)")
        for o in top:
            tag = "★" if o.is_favorite else " "
            flags = []
            if not o.fits_calendar:
                flags.append("calendar conflict")
            if not o.booking_open:
                flags.append("opens later")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(
                f" {tag} {o.name} @ {o.studio}"
                f"{(' · ' + o.instructor) if o.instructor else ''}"
            )
            lines.append(f"     {o.start_human} · {o.intensity} · score {o.score:.0f}{flag_str}")
        lines += [
            "",
            "Say 'book the <time> class' or 'book it' to reserve the top pick.",
            "═══════════════════════════════════════════",
        ]
        return "\n".join(lines)


def _class_id(c: dict) -> str:
    """Stable id from studio + start + name (survives across scans)."""
    base = f"{c.get('studio','')}|{c.get('start_iso','')}|{c.get('name','')}".lower()
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:80]
