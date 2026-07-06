"""PetKit tools for JARVIS — pet feeders and water fountains.

This talks to the PetKit cloud API **directly** (no Home Assistant required),
using the same authentication flow and endpoints as the community PetKit Home
Assistant integration (github.com/hasscc/petkit). Jarvis logs in once with the
user's PetKit account, caches the session token, and drives the feeders /
fountains from that.

Configuration (.env):
    PETKIT_USERNAME   — PetKit app account (email or phone number)
    PETKIT_PASSWORD   — PetKit app password (sent MD5-hashed, like the app)
    PETKIT_REGION     — optional API region, e.g. 'us', 'eu', 'cn'
                        (default 'us'; controls the api.* host)

Endpoints (mirrors hasscc/petkit):
    POST <api>/user/login             → session token
    POST <api>/discovery/device_roster → device list
    POST <api>/<type>/device_detail   → per-device status / food & water levels
    POST <api>/<type>/save_dailyfeed  → manual feed (amount in grams)
    POST <api>/<type>/update          → toggle feeding plan (manualLock etc.)

Device <type> path varies by model (feeder, feedermini, d3, d4, d4s, d4h,
d4sh for feeders; w5 for the Eversweet fountain). We read it from the roster.

Everything degrades gracefully when credentials are missing.
"""

import hashlib
import os
import time
import uuid

import aiohttp
from langchain_core.tools import tool

PETKIT_USERNAME = os.environ.get("PETKIT_USERNAME")
PETKIT_PASSWORD = os.environ.get("PETKIT_PASSWORD")
PETKIT_REGION = os.environ.get("PETKIT_REGION", "us").lower().strip()

# Regional API hosts (hasscc/petkit uses api.petkt.com for CN and the
# regionalized api.<region>.petkit.com / apiaus/apius for the rest).
_API_HOSTS = {
    "cn": "http://api.petkt.com/latest",
    "us": "http://api.us.petkt.com/latest",
    "eu": "http://api.eu.petkt.com/latest",
    "asia": "http://api.asia.petkt.com/latest",
}

_LOCALE = "en_US"
_TIMEZONE = "0.0"
_CLIENT = "ios(14.0;iPhone13,2)"
_APP_VERSION = "8.28.0"

# Feeder-type roster keys → API path segment used for detail/feed/update.
_FEEDER_TYPES = {"feeder", "feedermini", "d3", "d4", "d4s", "d4h", "d4sh", "feederpro"}
_FOUNTAIN_TYPES = {"w5", "ctw3", "fountain"}

# Cached session: {"token": str, "expires": epoch}
_session: dict = {}


def _api_host() -> str:
    return _API_HOSTS.get(PETKIT_REGION, _API_HOSTS["us"])


def _configured() -> bool:
    return bool(PETKIT_USERNAME and PETKIT_PASSWORD)


def _base_headers(token: str | None = None) -> dict:
    headers = {
        "X-Timezone": _TIMEZONE,
        "X-Api-Version": _APP_VERSION,
        "X-Client": _CLIENT,
        "X-Locale": _LOCALE,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if token:
        headers["X-Session"] = token
    return headers


async def _login(session: aiohttp.ClientSession) -> str:
    """Authenticate with PetKit and return a session token (cached ~24h)."""
    now = time.time()
    if _session.get("token") and _session.get("expires", 0) > now:
        return _session["token"]

    pwd_md5 = hashlib.md5(PETKIT_PASSWORD.encode("utf-8")).hexdigest()
    payload = {
        "username": PETKIT_USERNAME,
        "password": pwd_md5,
        "encrypt": "1",
        "oldVersion": _APP_VERSION,
        "region": PETKIT_REGION,
        "timezone": _TIMEZONE,
        "locale": _LOCALE,
    }
    async with session.post(
        f"{_api_host()}/user/login",
        headers=_base_headers(),
        data=payload,
    ) as resp:
        body = await resp.json(content_type=None)

    if not isinstance(body, dict) or "result" not in body:
        raise RuntimeError(f"PetKit login failed: {str(body)[:200]}")
    session_info = body["result"].get("session", {})
    token = session_info.get("id")
    if not token:
        raise RuntimeError(f"PetKit login returned no session: {str(body)[:200]}")

    # Sessions are long-lived; refresh once a day to be safe.
    _session["token"] = token
    _session["expires"] = now + 23 * 3600
    return token


async def _post(session: aiohttp.ClientSession, path: str, data: dict) -> dict:
    token = await _login(session)
    async with session.post(
        f"{_api_host()}/{path.lstrip('/')}",
        headers=_base_headers(token),
        data=data,
    ) as resp:
        body = await resp.json(content_type=None)
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"PetKit API error: {body['error'].get('msg', body['error'])}")
    return body.get("result", body) if isinstance(body, dict) else {}


async def _device_roster(session: aiohttp.ClientSession) -> list[dict]:
    """Return list of {'id','name','type','type_code'} for every PetKit device."""
    result = await _post(session, "discovery/device_roster", {"day": time.strftime("%Y%m%d")})
    devices = []
    for entry in (result.get("devices", []) if isinstance(result, dict) else []):
        data = entry.get("data", {})
        raw_type = (entry.get("type") or data.get("type") or "").lower()
        devices.append({
            "id": data.get("id") or entry.get("id"),
            "name": data.get("name") or raw_type or "PetKit device",
            "type": raw_type,                       # roster type, e.g. 'Feeder'
            "type_code": raw_type,                  # API path segment
        })
    return devices


def _is_feeder(dev: dict) -> bool:
    t = (dev.get("type") or "").lower()
    return t in _FEEDER_TYPES or "feed" in t


def _is_fountain(dev: dict) -> bool:
    t = (dev.get("type") or "").lower()
    return t in _FOUNTAIN_TYPES or "fountain" in t or t.startswith("w")


def _match(devices: list[dict], device: str | None, predicate) -> list[dict]:
    hits = [d for d in devices if predicate(d)]
    if device:
        want = device.lower().strip()
        narrowed = [d for d in hits if want in (d.get("name") or "").lower()]
        return narrowed or hits if not narrowed else narrowed
    return hits


async def _device_detail(session: aiohttp.ClientSession, dev: dict) -> dict:
    """Fetch a single device's detail record (food/water levels, state)."""
    type_code = dev.get("type_code") or dev.get("type") or "feeder"
    result = await _post(session, f"{type_code}/device_detail", {"id": dev["id"]})
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# Tools (names/signatures kept stable so registration doesn't change)
# ---------------------------------------------------------------------------

@tool
async def list_petkit_devices() -> str:
    """List the PetKit feeders and water fountains on the user's account.

    Use this first to discover device names when unsure, e.g. before "feed the
    cats" or "check the water fountain".
    """
    if not _configured():
        return ("PetKit isn't set up yet — add PETKIT_USERNAME and PETKIT_PASSWORD "
                "to .env (your PetKit app login).")
    try:
        async with aiohttp.ClientSession() as session:
            devices = await _device_roster(session)
    except Exception as e:
        return f"Couldn't reach PetKit: {e}"

    if not devices:
        return "No PetKit devices found on this account."

    feeders = [d["name"] for d in devices if _is_feeder(d)]
    fountains = [d["name"] for d in devices if _is_fountain(d)]
    lines = ["PetKit devices on your account:"]
    if feeders:
        lines.append("  Feeders: " + ", ".join(sorted(feeders)))
    if fountains:
        lines.append("  Fountains: " + ", ".join(sorted(fountains)))
    other = [d["name"] for d in devices if not _is_feeder(d) and not _is_fountain(d)]
    if other:
        lines.append("  Other: " + ", ".join(sorted(other)))
    return "\n".join(lines)


@tool
async def feed_pet(device: str = None, portions: int = 1) -> str:
    """Dispense food from a PetKit feeder right now ("feed the cats").

    Args:
        device: Which feeder (e.g. 'kitchen feeder', a pet name, or leave empty
            to use the only/first feeder found). Fuzzy matched on name.
        portions: Number of ~10g portions to dispense (default 1, max 10).
    """
    if not _configured():
        return "PetKit isn't set up — PETKIT_USERNAME/PETKIT_PASSWORD missing from .env."
    portions = max(1, min(int(portions), 10))
    grams = portions * 10  # PetKit feeds in grams; ~10g per portion.
    try:
        async with aiohttp.ClientSession() as session:
            devices = await _device_roster(session)
            feeders = _match(devices, device, _is_feeder)
            if not feeders:
                opts = ", ".join(d["name"] for d in devices if _is_feeder(d)) or "none"
                return f"No feeder matching '{device or 'any'}'. Available feeders: {opts}"
            target = feeders[0]
            type_code = target.get("type_code") or "feeder"
            # save_dailyfeed schedules a feed for "now"; amount in grams.
            hhmm = time.strftime("%H:%M")
            secs = int(hhmm[:2]) * 3600 + int(hhmm[3:5]) * 60
            payload = {
                "deviceId": target["id"],
                "day": time.strftime("%Y%m%d"),
                "time": secs,
                "amount": grams,
            }
            await _post(session, f"{type_code}/save_dailyfeed", payload)
    except Exception as e:
        return f"Couldn't feed via PetKit: {e}"
    return f"Dispensing {portions} portion(s) (~{grams}g) from {target['name']}, sir."


@tool
async def get_feeder_status(device: str = None) -> str:
    """Report a PetKit feeder's food level, portions dispensed, and settings.

    Use for "how much food is left?", "check the feeder", "did the cats get fed
    today?".

    Args:
        device: Which feeder (fuzzy matched); empty = all feeders.
    """
    if not _configured():
        return "PetKit isn't set up — PETKIT_USERNAME/PETKIT_PASSWORD missing from .env."
    try:
        async with aiohttp.ClientSession() as session:
            devices = await _device_roster(session)
            feeders = _match(devices, device, _is_feeder)
            if not feeders:
                return f"No feeder matching '{device or 'any'}'. Try list_petkit_devices."
            lines = []
            for dev in feeders:
                detail = await _device_detail(session, dev)
                state = detail.get("state", {}) if isinstance(detail, dict) else {}
                parts = []
                # Food remaining: PetKit reports 'food' (0/1 low) or 'foodState'.
                food = state.get("food")
                if food is not None:
                    parts.append("food: " + ("LOW — refill soon!" if food in (0, "0", False)
                                             else "OK"))
                if "desiccantLeftDays" in state:
                    parts.append(f"desiccant: {state['desiccantLeftDays']} days left")
                if "batteryPower" in state:
                    parts.append(f"battery: {state['batteryPower']}")
                feed_state = detail.get("feed", {}) if isinstance(detail, dict) else {}
                if "amount" in feed_state:
                    parts.append(f"dispensed today: {feed_state['amount']}g")
                if state.get("wifi", {}).get("rsq") is not None:
                    parts.append(f"wifi: {state['wifi']['rsq']}")
                lines.append(f"{dev['name']} — " + ("; ".join(parts) if parts else "online"))
            return "\n".join(lines)
    except Exception as e:
        return f"Couldn't read feeder status from PetKit: {e}"


@tool
async def get_feeding_schedule(device: str = None) -> str:
    """Show a PetKit feeder's scheduled feeding plan.

    Use for "when do the cats get fed?", "what's the feeding schedule?".

    Args:
        device: Which feeder (fuzzy matched); empty = all feeders.
    """
    if not _configured():
        return "PetKit isn't set up — PETKIT_USERNAME/PETKIT_PASSWORD missing from .env."
    try:
        async with aiohttp.ClientSession() as session:
            devices = await _device_roster(session)
            feeders = _match(devices, device, _is_feeder)
            if not feeders:
                return f"No feeder matching '{device or 'any'}'. Try list_petkit_devices."
            lines = []
            for dev in feeders:
                type_code = dev.get("type_code") or "feeder"
                result = await _post(session, f"{type_code}/feed_daily_list",
                                     {"deviceId": dev["id"], "days": time.strftime("%Y%m%d")})
                items = []
                plan = result if isinstance(result, list) else result.get("items", []) \
                    if isinstance(result, dict) else []
                for it in plan:
                    for f in it.get("items", []) if isinstance(it, dict) else []:
                        secs = f.get("time", 0)
                        hhmm = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}"
                        items.append(f"{hhmm} → {f.get('amount', '?')}g")
                lines.append(f"{dev['name']}: " + (", ".join(items) if items
                             else "no scheduled feeds"))
            return "PetKit feeding plan:\n" + "\n".join(lines)
    except Exception as e:
        return (f"Couldn't read the feeding schedule from PetKit: {e}. "
                "You can still say 'feed the cats' for an immediate feed.")


@tool
async def toggle_feeding_plan(device: str = None, enable: bool = True) -> str:
    """Enable or disable a PetKit feeder's automatic feeding plan.

    Use for "turn the feeding schedule on/off".

    Args:
        device: Which feeder (fuzzy matched); empty = first feeder found.
        enable: True to enable the scheduled plan, False to disable it.
    """
    if not _configured():
        return "PetKit isn't set up — PETKIT_USERNAME/PETKIT_PASSWORD missing from .env."
    try:
        async with aiohttp.ClientSession() as session:
            devices = await _device_roster(session)
            feeders = _match(devices, device, _is_feeder)
            if not feeders:
                return f"No feeder matching '{device or 'any'}'. Try list_petkit_devices."
            target = feeders[0]
            type_code = target.get("type_code") or "feeder"
            # PetKit stores the plan on/off in the device settings ('settings' kv).
            import json as _json
            settings = {"manualLock": 0} if enable else {"manualLock": 1}
            await _post(session, f"{type_code}/update", {
                "id": target["id"],
                "kv": _json.dumps({"settings": settings}),
            })
    except Exception as e:
        return f"Couldn't change the feeding plan via PetKit: {e}"
    word = "enabled" if enable else "disabled"
    return f"Feeding plan {word} for {target['name']}, sir."


@tool
async def get_fountain_status(device: str = None) -> str:
    """Report a PetKit water fountain's water level, filter life, and power.

    Use for "check the water fountain", "how's the fountain water level?", "is
    the filter due?".

    Args:
        device: Which fountain (fuzzy matched); empty = all fountains.
    """
    if not _configured():
        return "PetKit isn't set up — PETKIT_USERNAME/PETKIT_PASSWORD missing from .env."
    try:
        async with aiohttp.ClientSession() as session:
            devices = await _device_roster(session)
            fountains = _match(devices, device, _is_fountain)
            if not fountains:
                return f"No water fountain matching '{device or 'any'}'. Try list_petkit_devices."
            lines = []
            for dev in fountains:
                detail = await _device_detail(session, dev)
                state = detail.get("status", detail.get("state", {})) \
                    if isinstance(detail, dict) else {}
                parts = []
                if "lackWarning" in state:
                    parts.append("water: " + ("LOW — refill!" if state["lackWarning"]
                                              else "OK"))
                elif "waterLevel" in state:
                    parts.append(f"water level: {state['waterLevel']}")
                if "filterPercent" in state:
                    parts.append(f"filter: {state['filterPercent']}% life left")
                elif "filterExpectedDays" in state:
                    parts.append(f"filter: {state['filterExpectedDays']} days left")
                if "powerStatus" in state:
                    parts.append("power: " + ("on" if state["powerStatus"] else "off"))
                if "runStatus" in state:
                    parts.append(f"mode: {state['runStatus']}")
                lines.append(f"{dev['name']} — " + ("; ".join(parts) if parts else "online"))
            return "\n".join(lines)
    except Exception as e:
        return f"Couldn't read fountain status from PetKit: {e}"
