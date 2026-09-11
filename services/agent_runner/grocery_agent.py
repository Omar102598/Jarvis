"""Smart Weekly Grocery Agent — v5.0

What's new in v5.0:
  - User profile loaded from Redis user:profile (configurable via Jarvis conversation).
  - TDEE + macro calculation (Mifflin-St Jeor) for fitness-aware list generation.
  - Fitness-aware LLM list generation: targets calorie deficit, protein, and fiber goals.
  - Greedy 1-store-first consolidation: prefers 1 store, adds 2nd only if needed.
  - Persistent browser sessions via mac_bridge /browser/save-state.
  - Full cart report with direct cart links sent via iMessage + iOS app push.
  - Approval flow: pending order stored in Redis; checkout triggered on user approval.
  - Checkout action mode: navigates carts, clicks Proceed to Checkout, confirms.

Note: Target and HEB block unauthenticated API calls (CAPTCHA / redirect).
Price comparison uses the headless Playwright scraper (/scraper/* endpoints) for all
stores — this runs real Chromium with stealth settings which bypasses bot detection.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional
import aiohttp
import redis as redis_lib

from base_agent import BaseAgent
from llm_helper import complete

# An explicit MAC_BRIDGE_URL wins; otherwise build it from the vars compose
# actually exports (MAC_HOST -> MAC_BRIDGE_HOST). Defaulting straight to
# host.docker.internal silently stranded this service when the core stack
# moved to GCP, where that name does not resolve at all.
# 'or' throughout rather than get() defaults: compose expands an unset variable
# to an EMPTY string rather than omitting it, and a get() default only fires when
# the key is absent — so the default would never apply and the URL would come out
# as "http://:7777".
MAC_BRIDGE_URL = os.environ.get("MAC_BRIDGE_URL") or (
    f"http://{os.environ.get('MAC_BRIDGE_HOST') or 'host.docker.internal'}"
    f":{os.environ.get('MAC_BRIDGE_PORT') or '7777'}")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")

# ---------------------------------------------------------------------------
# Store configurations
# ---------------------------------------------------------------------------
# Must match nutrition.py's USER_TZ: Sage keys its rollups by local date, so a
# different zone here reads the wrong day near midnight.
USER_TZ = os.environ.get("USER_TZ", "America/Chicago")

STORES = {
    "amazon": {
        "name": "Amazon Fresh",
        "search_url": "https://www.amazon.com/s?k={query}&i=amazonfresh",
        # Amazon Fresh is its own shopping context. A plain /dp/ page loads in RETAIL
        # mode (its #add-to-cart-button adds to the regular cart). Appending
        # ?almBrandId=<brand>&fpw=alm switches the page into Fresh mode, where the add
        # control is #freshAddToCartButton and the item lands in the Fresh cart below.
        "cart_url":   "https://www.amazon.com/cart/localmarket?almBrandId=QW1hem9uIEZyZXNo",
        "alm_brand_id": "QW1hem9uIEZyZXNo",   # base64 "Amazon Fresh"
        "orderable":  True,
        "link_pattern": "/dp/",          # product-detail URL marker (for candidate extraction)
        "product_base": "https://www.amazon.com",
    },
    "whole_foods": {
        "name": "Whole Foods",
        "search_url": "https://www.amazon.com/s?k={query}&i=wholefoods",
        "cart_url":   "https://www.amazon.com/cart/localmarket?almBrandId=VUZHIFdob2xlIEZvb2Rz",
        "alm_brand_id": "VUZHIFdob2xlIEZvb2Rz",   # base64 "UFG Whole Foods"
        "orderable":  True,
        "link_pattern": "/dp/",
        "product_base": "https://www.amazon.com",
    },
    "target": {
        "name": "Target",
        "search_url": "https://www.target.com/s?searchTerm={query}&category=5xt1a",
        "cart_url":   "https://www.target.com/cart",
        "orderable":  True,
        "link_pattern": "/p/",
        "product_base": "https://www.target.com",
    },
    "heb": {
        "name": "HEB",
        "search_url": "https://www.heb.com/search/?q={query}",
        "cart_url":   "https://www.heb.com/cart",
        "orderable":  True,
        "link_pattern": "/product-detail/",
        "product_base": "https://www.heb.com",
    },
}

# Stores queried each run, in preference order for tie-breaks
STORE_KEYS = ("amazon", "whole_foods", "target", "heb")

# Pending order Redis key
PENDING_ORDER_KEY = "grocery:pending_order"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Bound to the running agent's BaseAgent.log_event by GroceryAgent.run(),
# so the module-level pipeline functions feed the unified observability
# stream without threading `self` through every helper.
_EMIT = None


def _log(msg: str, kind: str = "") -> None:
    print(f"[GroceryAgent] {msg}", flush=True)
    if _EMIT is None:
        return
    stripped = msg.strip()
    if not kind:
        if stripped[:1] in ("✓", "✗", "⚠", "…"):
            kind = "finding"
        elif stripped.startswith(("[cart]", "[checkout]")) or any(
            w in stripped for w in ("Scanning", "Fetching", "Navigating", "Resolving")
        ):
            kind = "tool"
        else:
            kind = "thinking"
    _EMIT(kind, stripped)


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------
def _load_user_profile(r: redis_lib.Redis) -> dict:
    """Load fitness + grocery profile from Redis user:profile key.

    If a recent HealthKit snapshot exists (``user:health:latest``), its measured
    metrics are merged in: current body weight overrides the static profile
    weight, and the average daily active-energy burn is attached so the TDEE
    calculation can use measured expenditure instead of a static multiplier.
    """
    try:
        raw = r.get("user:profile") or "{}"
        profile = json.loads(raw)
    except Exception:
        profile = {}

    merged = {
        "goal":                  "cutting",
        "weight_lbs":            201,
        "height_in":             72,       # 6'0"
        "age":                   25,
        "activity_level":        "moderately_active",
        "weekly_budget_usd":     150.0,
        "protein_goal_g_per_lb": 1.0,
        "fiber_goal_g":          35,
        "imessage_to":           "",
        "dietary_preferences":   ["high protein", "low sugar"],
        **profile,
    }

    health = _load_recent_health(r)
    if health:
        if health.get("body_mass_lbs"):
            merged["weight_lbs"] = round(float(health["body_mass_lbs"]), 1)
        if health.get("avg_active_energy_kcal"):
            merged["measured_active_energy_kcal"] = round(float(health["avg_active_energy_kcal"]))
    return merged


def _load_recent_health(r: redis_lib.Redis) -> dict:
    """Aggregate the last week of HealthKit snapshots into average metrics.

    Returns {} if no usable health history is present.
    """
    try:
        raw_hist = r.lrange("user:health:history", 0, 13) or []
    except Exception:
        return {}

    energies, masses = [], []
    latest_mass = None
    for item in raw_hist:
        try:
            snap = json.loads(item)
        except Exception:
            continue
        if snap.get("active_energy_kcal"):
            energies.append(float(snap["active_energy_kcal"]))
        if snap.get("body_mass_lbs"):
            masses.append(float(snap["body_mass_lbs"]))
            if latest_mass is None:
                latest_mass = float(snap["body_mass_lbs"])

    out: dict = {}
    if energies:
        out["avg_active_energy_kcal"] = sum(energies) / len(energies)
    if latest_mass is not None:
        out["body_mass_lbs"] = latest_mass
    return out


def _load_recent_nutrition(r: redis_lib.Redis, days: int = 7) -> dict:
    """Average of what the user ACTUALLY ate, from Sage's daily rollups.

    Sage (llm_agent/tools/nutrition.py) writes nutrition:totals:{day} whenever a
    meal is logged — including from a photo, since the brain estimates macros
    from the image directly. Atlas already reads these; Remy never did, so the
    plan was built purely from computed targets and had no idea whether they
    were being hit. Buying to a 180g protein target while consistently eating
    120g means restocking the same shortfall every week.

    Only days with a logged meal count. Averaging over silent days would report
    a fake shortfall on any day the user simply didn't log, which would push the
    list toward protein for no reason.
    """
    from datetime import timedelta
    tz_today = datetime.now(ZoneInfo(USER_TZ)).date()
    cals, prots, logged_days = [], [], 0
    for offset in range(days):
        day = (tz_today - timedelta(days=offset)).strftime("%Y-%m-%d")
        try:
            totals = r.hgetall(f"nutrition:totals:{day}") or {}
        except Exception:
            continue
        if not totals:
            continue
        try:
            cal = float(totals.get("calories", 0) or 0)
            pro = float(totals.get("protein_g", 0) or 0)
        except (TypeError, ValueError):
            continue
        if cal <= 0 and pro <= 0:
            continue
        cals.append(cal)
        prots.append(pro)
        logged_days += 1

    if not logged_days:
        return {}
    return {
        "days_logged":      logged_days,
        "avg_calories":     round(sum(cals) / logged_days),
        "avg_protein_g":    round(sum(prots) / logged_days),
    }


# ---------------------------------------------------------------------------
# TDEE + macro calculation (Mifflin-St Jeor)
# ---------------------------------------------------------------------------
def _calculate_targets(profile: dict) -> dict:
    weight_kg = float(profile["weight_lbs"]) * 0.453592
    height_cm = float(profile["height_in"]) * 2.54
    age       = int(profile.get("age", 25))

    bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + 5

    # Prefer measured expenditure from HealthKit (BMR + average active energy)
    # over the static activity multiplier, which is only an estimate.
    measured_active = profile.get("measured_active_energy_kcal")
    if measured_active:
        tdee = bmr + float(measured_active)
        tdee_source = "healthkit"
    else:
        multipliers = {
            "sedentary":        1.2,
            "lightly_active":   1.375,
            "moderately_active":1.55,
            "very_active":      1.725,
            "extremely_active": 1.9,
        }
        tdee = bmr * multipliers.get(profile.get("activity_level", "moderately_active"), 1.55)
        tdee_source = "estimated"

    goal = profile.get("goal", "cutting")
    if goal == "cutting":
        target_cal = tdee - 500
    elif goal == "bulking":
        target_cal = tdee + 300
    elif goal in ("recomp", "recomposition", "body_recomp"):
        # Body recomposition — losing fat and gaining muscle at once — is not a
        # cut. A 500 kcal deficit makes it a cut with extra protein and gives up
        # the muscle gain; maintenance alone gives up the fat loss. The shape
        # that actually works is a small deficit with protein pushed up, so it
        # gets its own goal rather than being approximated by the other two.
        target_cal = tdee - 200
    else:
        target_cal = tdee

    protein_per_lb = float(profile.get("protein_goal_g_per_lb", 1.0))
    if goal in ("recomp", "recomposition", "body_recomp"):
        # Protein is what makes a recomp work at all — it is the difference
        # between losing fat with muscle and losing fat and muscle together.
        # Raise the floor without lowering a deliberately higher setting.
        protein_per_lb = max(protein_per_lb, 1.1)
    protein_g = float(profile["weight_lbs"]) * protein_per_lb
    fiber_g   = int(profile.get("fiber_goal_g", 35))

    return {
        "bmr":             round(bmr),
        "tdee":            round(tdee),
        "tdee_source":     tdee_source,
        "target_calories": round(target_cal),
        "deficit":         round(tdee - target_cal),
        "protein_g":       round(protein_g),
        "protein_per_lb":  round(protein_per_lb, 2),
        "goal":            goal,
        "fiber_g":         fiber_g,
    }


# ---------------------------------------------------------------------------
# Mac Bridge helpers
# ---------------------------------------------------------------------------
async def _bridge_post(
    session: aiohttp.ClientSession, path: str, payload: dict, timeout: int = 60
) -> dict:
    try:
        async with session.post(
            f"{MAC_BRIDGE_URL}{path}",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            return await resp.json()
    except asyncio.TimeoutError:
        return {"error": f"Timeout calling {path}"}
    except Exception as exc:
        return {"error": str(exc)}


async def _bridge_get(session: aiohttp.ClientSession, path: str, timeout: int = 60) -> dict:
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


async def _check_bridge(session: aiohttp.ClientSession) -> bool:
    try:
        async with session.get(
            f"{MAC_BRIDGE_URL}/health",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json()
            return data.get("status") == "ok"
    except Exception as exc:
        _log(f"Bridge health check failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Price extraction helpers
# ---------------------------------------------------------------------------
def _extract_price(text: str) -> Optional[float]:
    match = re.search(r"\$\s*(\d+\.?\d*)", str(text) or "")
    return float(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class PriceResult:
    store:      str
    store_name: str
    item:       str
    price:      Optional[float] = None
    unit:       str = ""
    error:      str = ""
    title:      str = ""          # actual matched product title
    href:       str = ""          # product-detail URL (so the priced product IS the added one)
    from_cache: bool = False      # True if the product identity came from the resolution cache


@dataclass
class OrderResult:
    item:               str
    assigned_store:     str
    assigned_store_name:str
    best_price:         Optional[float]
    status:             str  # "added" | "not_found" | "error" | "budget_exceeded"
    note:               str = ""
    all_prices:         list[PriceResult] = field(default_factory=list)
    title:              str = ""
    href:               str = ""


# ---------------------------------------------------------------------------
# Product resolution cache (Redis)
#
# Staples repeat week to week, so once we've matched "chicken breast" → a
# specific Target product we store that mapping and reuse it — refreshing only
# the live price from the search page, never spending an LLM call again.
#
#   grocery:product_map   hash  field="{store}:{norm_item}"  value={href,title}
#   grocery:favorites     hash  same shape — user-pinned, always wins, never
#                               overwritten by the resolver.
# ---------------------------------------------------------------------------
_PRODUCT_MAP_KEY = "grocery:product_map"
_FAVORITES_KEY   = "grocery:favorites"


def _norm_item(item: str) -> str:
    return re.sub(r"\s+", " ", str(item).strip().lower())


def _cache_field(store_key: str, item: str) -> str:
    return f"{store_key}:{_norm_item(item)}"


def _cached_href(r, key: str, store_key: str, item: str) -> Optional[str]:
    try:
        raw = r.hget(key, _cache_field(store_key, item))
        return json.loads(raw).get("href") if raw else None
    except Exception:
        return None


def _cache_put(r, store_key: str, item: str, href: str, title: str) -> None:
    try:
        r.hset(_PRODUCT_MAP_KEY, _cache_field(store_key, item),
               json.dumps({"href": href, "title": title}))
    except Exception as exc:
        _log(f"  ⚠ cache write failed: {exc}")


# ---------------------------------------------------------------------------
# Candidate extraction — generic across stores
#
# Anchored on each store's product-detail URL marker (link_pattern). For every
# product link we walk up to the largest ancestor that still contains exactly
# ONE product and a price — the clean single-product card — and read its title
# and price. This keeps title+price+href coupled to the SAME product, which is
# the whole point: the item we price is the item we add.
# ---------------------------------------------------------------------------
_CANDIDATE_JS = r"""
(function(){
  var pat = "__PAT__";
  var anchors = Array.from(document.querySelectorAll('a[href*="' + pat + '"]'));
  var map = {};
  anchors.forEach(function(a){
    var href = (a.getAttribute('href') || '').split('?')[0];
    if(!href) return;
    // largest ancestor that still wraps exactly one product and has a price
    var node = a, card = null;
    for(var d=0; d<10 && node.parentElement; d++){
      node = node.parentElement;
      var hrefs = new Set(Array.from(node.querySelectorAll('a[href*="'+pat+'"]'))
        .map(function(x){ return (x.getAttribute('href')||'').split('?')[0]; }));
      if(hrefs.size > 1) break;                 // walked into a neighbouring product
      if(/\$\s?\d/.test(node.innerText||'')) card = node;
    }
    if(!card) return;
    var ctext = card.innerText || '';
    var pm = ctext.match(/\$\s?[0-9]+(\.[0-9]{2})?/);
    var atext = (a.innerText || '').replace(/\s+/g, ' ').trim();
    // A title is descriptive text — not a price, rating, or button label.
    var isTitle = atext.length > 10
        && atext.charAt(0) !== '$'
        && !/^\d/.test(atext)
        && !/out of 5 stars|reviews|add to cart|\/each|\/ounce|\/fluid/i.test(atext)
        && /[a-z]{4}/i.test(atext);
    var cur = map[href] || { href: href, title: null, price: null,
                             sponsored: /sponsored/i.test(ctext) };
    if(pm && !cur.price) cur.price = pm[0];
    if(isTitle && (!cur.title || atext.length > cur.title.length)) cur.title = atext;
    map[href] = cur;
  });
  return Object.keys(map).map(function(h){ return map[h]; })
    .filter(function(x){ return x.title && x.price; })
    .slice(0, 12);
})();
"""


# Strip quantity/size qualifiers ("4 lbs", "5 lb bag", "16 oz", "pack of 4") from a
# search query — those over-constrain the store search and cause zero matches.
_QTY_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:lb|lbs|pound|pounds|oz|ounce|ounces|ct|count|"
    r"pk|pack|pint|pints|quart|quarts|gallon|gallons|dozen|g|kg|ml|l|liter|liters)\b\.?",
    re.IGNORECASE,
)


def _clean_query(item: str) -> str:
    q = _QTY_PATTERN.sub(" ", item)
    q = re.sub(r"\bpack of \d+\b", " ", q, flags=re.IGNORECASE)
    q = re.sub(r"\s+", " ", q).strip(" ,-")
    return q or item


# ---------------------------------------------------------------------------
# Read path — Firecrawl first, browser as the fallback
#
# Remy's reads and writes have completely different requirements and only the
# writes actually need the logged-in browser. Price comparison and product
# matching are PUBLIC pages: no session, no cart, nothing personal. Routing
# them through the one headful Chromium made them share every one of its
# failure modes — the wedged browser that took Remy out for six weeks, and
# HEB's bot protection, which blocks price reads that need no login at all.
# When HEB reads fail, Amazon wins the >=60% coverage rule by default, which
# is a large part of why every order has been Amazon Fresh.
#
# Firecrawl runs from its own infrastructure with anti-bot handling as its
# product, so it is not subject to either. Cart writes stay on the browser
# because they need the session.
#
# Every failure falls back to the browser, so the worst case is exactly the
# behaviour we have today.
# ---------------------------------------------------------------------------
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_URL = os.environ.get("FIRECRAWL_URL", "https://api.firecrawl.dev").rstrip("/")
# Opt out without removing the key.
FIRECRAWL_READS = os.environ.get("GROCERY_FIRECRAWL_READS", "1").strip().lower() not in ("0", "false", "no")
# After this many consecutive failures, stop trying for the rest of the run.
# Without it, a bad key or an outage costs a failed request per item BEFORE
# each browser fallback — 24 items of pure added latency to reach the same
# place. Reset on any success, so one blip does not disable the whole run.
_FIRECRAWL_MAX_STRIKES = 2
_firecrawl_strikes = 0


def _firecrawl_ready() -> bool:
    return bool(FIRECRAWL_API_KEY) and FIRECRAWL_READS and _firecrawl_strikes < _FIRECRAWL_MAX_STRIKES


async def _firecrawl_candidates(
    session: aiohttp.ClientSession, item: str, store_key: str
) -> list[dict]:
    """Candidate products for one item at one store, via Firecrawl.

    Returns [] on ANY failure — the caller then uses the browser. Structured
    extraction is used rather than markdown parsing so this does not need a
    bespoke parser per store, and so a store redesign does not silently return
    junk the way a selector would.
    """
    global _firecrawl_strikes
    store = STORES[store_key]
    url = store["search_url"].format(query=_clean_query(item).replace(" ", "+"))
    body = {
        "url": url,
        "onlyMainContent": True,
        "formats": ["json"],
        "jsonOptions": {
            "prompt": (f"Extract up to 8 grocery products from these search "
                       f"results for '{item}'. Skip sponsored results."),
            "schema": {"type": "object", "properties": {"products": {
                "type": "array", "items": {"type": "object", "properties": {
                    "title": {"type": "string"},
                    "price": {"type": "number"},
                    "url": {"type": "string"},
                }, "required": ["title", "price"]}}}, "required": ["products"]},
        },
    }
    try:
        async with session.post(
                f"{FIRECRAWL_URL}/v1/scrape",
                headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                         "Content-Type": "application/json"},
                json=body, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                detail = (await resp.text())[:120].replace("\n", " ")
                _firecrawl_strikes += 1
                _log(f"  ⚠ Firecrawl {resp.status} for {store['name']} "
                     f"({detail}) — using the browser", "warn")
                return []
            payload = await resp.json()
    except Exception as exc:
        _firecrawl_strikes += 1
        _log(f"  ⚠ Firecrawl error for {store['name']} ({str(exc)[:80]}) — using the browser", "warn")
        return []

    products = ((payload.get("data") or {}).get("json") or {}).get("products") or []
    out = []
    for p in products:
        title = str(p.get("title") or "").strip()
        href = str(p.get("url") or "").strip()
        try:
            price = float(p.get("price"))
        except (TypeError, ValueError):
            continue
        # A candidate with no product URL cannot be added to a cart later, so
        # it is useless here even though it has a title and a price.
        if not title or not href or price <= 0:
            continue
        out.append({"href": href, "title": title, "price": price})

    if out:
        _firecrawl_strikes = 0          # a success clears earlier blips
    return out


async def _extract_candidates(
    session: aiohttp.ClientSession, item: str, store_key: str
) -> list[dict]:
    """Candidate products for one item at one store.

    Firecrawl first (public read, no session needed), browser as the fallback.
    """
    if _firecrawl_ready():
        cands = await _firecrawl_candidates(session, item, store_key)
        if cands:
            _log(f"  ✓ {STORES[store_key]['name']}: {len(cands)} candidates via Firecrawl")
            return cands

    store = STORES[store_key]
    url = store["search_url"].format(query=_clean_query(item).replace(" ", "+"))
    nav = await _bridge_post(session, "/browser/navigate", {"url": url}, timeout=40)
    if "error" in nav:
        _log(f"  ✗ {store['name']}: nav failed for '{item}'")
        return []
    await asyncio.sleep(4)

    js = _CANDIDATE_JS.replace("__PAT__", store["link_pattern"])
    res = await _bridge_post(session, "/browser/js", {"script": js}, timeout=20)
    cands = res.get("result") or []
    if not isinstance(cands, list):
        return []
    # Attach a parsed float price and drop sponsored / unpriced rows
    out = []
    for c in cands:
        price = _extract_price(c.get("price", ""))
        if price is None or c.get("sponsored"):
            continue
        out.append({"href": c["href"], "title": c["title"], "price": price})
    return out


# ---------------------------------------------------------------------------
# LLM product matcher — one batched call per item (cache-miss stores only)
# ---------------------------------------------------------------------------
async def _llm_pick_products(item: str, cands_by_store: dict[str, list[dict]]) -> dict[str, int]:
    """Return {store_key: chosen_index} picking the best real product per store.

    Only called for stores with no cached/pinned match, and batched across those
    stores in a single LLM call to keep token use low.
    """
    if not cands_by_store:
        return {}

    lines = [f'Grocery item to match: "{item}"', ""]
    for store_key, cands in cands_by_store.items():
        lines.append(f"Store: {store_key}")
        for i, c in enumerate(cands):
            lines.append(f"  [{i}] {c['title']} — ${c['price']:.2f}")
        lines.append("")

    system = (
        "You match a grocery shopping item to the single best real product from a "
        "store's search results. Prefer the plain, basic, raw staple version over "
        "prepared, flavored, pre-cooked, frozen, or organic-premium variants UNLESS "
        "the item name asks for them. Prefer a normal grocery pack size and a "
        "sensible price. Never pick an obviously unrelated product.\n"
        "Return ONLY JSON mapping each store to the chosen candidate index, e.g. "
        '{"target": {"index": 2}, "amazon": {"index": 0}}. '
        "Use index -1 for a store if none of its candidates is an acceptable match."
    )
    try:
        resp = await complete(system=system, user="\n".join(lines), max_tokens=400)
        match = re.search(r"\{.*\}", resp or "", re.DOTALL)
        data = json.loads(match.group()) if match else {}
    except Exception as exc:
        _log(f"  ⚠ LLM product match failed: {exc}")
        data = {}

    picks: dict[str, int] = {}
    for store_key in cands_by_store:
        entry = data.get(store_key) or {}
        idx = entry.get("index", -1) if isinstance(entry, dict) else -1
        picks[store_key] = int(idx) if isinstance(idx, (int, float)) else -1
    return picks


# ---------------------------------------------------------------------------
# Unified per-item resolution across all stores (cache-first, LLM on miss)
# ---------------------------------------------------------------------------
async def _resolve_item(
    session: aiohttp.ClientSession, item: str, store_keys: tuple[str, ...], r
) -> list[PriceResult]:
    """For one grocery item, return a PriceResult per store with the actual
    matched product (title + href + live price). Uses pinned favorites and the
    resolution cache first; only unresolved stores hit the LLM, batched together.
    """
    cands_by_store: dict[str, list[dict]] = {}
    for store_key in store_keys:
        cands_by_store[store_key] = await _extract_candidates(session, item, store_key)
        await asyncio.sleep(0.3)

    chosen: dict[str, dict] = {}
    need_llm: dict[str, list[dict]] = {}

    for store_key, cands in cands_by_store.items():
        if not cands:
            continue
        href_index = {c["href"]: c for c in cands}
        # 1) user-pinned favorite, if it's on the page
        fav = _cached_href(r, _FAVORITES_KEY, store_key, item)
        if fav and fav in href_index:
            chosen[store_key] = {**href_index[fav], "from_cache": True}
            continue
        # 2) previously-resolved product, if still on the page
        cached = _cached_href(r, _PRODUCT_MAP_KEY, store_key, item)
        if cached and cached in href_index:
            chosen[store_key] = {**href_index[cached], "from_cache": True}
            continue
        # 3) otherwise this store needs the LLM
        need_llm[store_key] = cands

    if need_llm:
        picks = await _llm_pick_products(item, need_llm)
        for store_key, idx in picks.items():
            cands = need_llm[store_key]
            if 0 <= idx < len(cands):
                c = cands[idx]
                chosen[store_key] = {**c, "from_cache": False}
                _cache_put(r, store_key, item, c["href"], c["title"])

    results: list[PriceResult] = []
    for store_key in store_keys:
        store_name = STORES[store_key]["name"]
        c = chosen.get(store_key)
        if c:
            results.append(PriceResult(
                store=store_key, store_name=store_name, item=item,
                price=c["price"], unit=f"${c['price']:.2f}",
                title=c["title"], href=c["href"], from_cache=c.get("from_cache", False),
            ))
            tag = "cache" if c.get("from_cache") else "LLM"
            _log(f"  ✓ {store_name}: ${c['price']:.2f} [{tag}] {c['title'][:45]}")
        else:
            results.append(PriceResult(store=store_key, store_name=store_name, item=item))
            _log(f"  ✗ {store_name}: no acceptable match for '{item}'")
    return results


# ---------------------------------------------------------------------------
# Store selection — greedy 1-store-first
# ---------------------------------------------------------------------------
def _select_ordering_stores(
    price_map: dict[str, list[PriceResult]],
) -> tuple[list[str], dict[str, str]]:
    """Pick 1 store (2nd only if 1st covers <60%). Assign each item to cheapest selected store."""
    store_coverage: dict[str, set[str]] = defaultdict(set)
    store_totals:   dict[str, float]    = defaultdict(float)

    for item, prices in price_map.items():
        for p in prices:
            if p.price is not None:
                store_coverage[p.store].add(item)
                store_totals[p.store] += p.price

    if not store_coverage:
        return ["amazon"], {item: "amazon" for item in price_map}

    total_items = len(price_map)

    def store_score(s: str) -> tuple[int, float]:
        n = len(store_coverage[s])
        avg = store_totals[s] / n if n else float("inf")
        return (n, -avg)

    ranked = sorted(store_coverage, key=store_score, reverse=True)
    best   = ranked[0]
    coverage_pct = len(store_coverage[best]) / total_items if total_items else 0

    if coverage_pct >= 0.60 or len(ranked) == 1:
        selected = [best]
    else:
        selected = ranked[:2]

    def assign(item: str) -> str:
        candidates = [p for p in price_map[item] if p.store in selected and p.price is not None]
        if candidates:
            return min(candidates, key=lambda p: p.price).store
        return selected[0]

    return selected, {item: assign(item) for item in price_map}


# ---------------------------------------------------------------------------
# Amazon order history
# ---------------------------------------------------------------------------
async def _fetch_amazon_order_history(session: aiohttp.ClientSession) -> list[str]:
    _log("Step 1: Fetching Amazon order history…")
    nav = await _bridge_post(
        session,
        "/browser/navigate",
        {"url": "https://www.amazon.com/gp/your-account/order-history"},
        timeout=40,
    )
    if "error" in nav:
        _log(f"  ✗ Order history nav failed: {nav['error']}")
        return []
    await asyncio.sleep(5)

    history_js = """
    (function() {
        var items = Array.from(document.querySelectorAll(
            '.yohtmlc-product-title, .a-link-normal[href*="/dp/"], .product-title'
        )).map(function(el) { return el.innerText.trim(); })
          .filter(function(t) { return t.length > 3 && t.length < 80; });
        return [...new Set(items)].slice(0, 30);
    })();
    """
    res = await _bridge_post(session, "/browser/js", {"script": history_js}, timeout=15)
    raw = res.get("result", [])
    items = [str(r) for r in raw if r] if isinstance(raw, list) else []

    if not items:
        page = await _bridge_get(session, "/browser/read", timeout=20)
        text = str(page.get("text") or "")
        items = [l.strip() for l in text.split("\n") if 5 < len(l.strip()) < 80][:20]

    _log(f"  ✓ Found {len(items)} past purchases.")
    return items


# ---------------------------------------------------------------------------
# Usual order — learned from the user's own Amazon Fresh cart
#
# The user builds a cart of their habitual items in Amazon Fresh, then says
# "learn my fresh cart" (action=learn_cart). We scrape that cart, merge it into
# grocery:usual_order (frequency-counted across scans), and every future list
# generation biases toward those staples.
# ---------------------------------------------------------------------------
USUAL_ORDER_KEY = "grocery:usual_order"

# The localmarket cart page renders the standard Amazon active-cart widget.
# Try the modern selectors first, then legacy fallbacks.
_CART_SCAN_JS = r"""
(function() {
    var rows = document.querySelectorAll(
        '[data-name="Active Items"] [data-asin], .sc-list-body [data-asin], .sc-list-item[data-asin]'
    );
    var out = [];
    rows.forEach(function(row) {
        var asin = row.getAttribute('data-asin') || '';
        if (!asin) return;
        // Amazon nests .a-truncate-full AND .a-truncate-cut inside the title;
        // reading the parent's textContent concatenates both copies. Prefer the
        // innermost full-text span.
        var titleEl = row.querySelector('.sc-product-title .a-truncate-full') ||
                      row.querySelector('.a-truncate-full') ||
                      row.querySelector('.sc-product-title, .a-truncate-cut, .sc-grid-item-product-title');
        var title = titleEl ? titleEl.textContent.replace(/\s+/g, ' ').trim() : '';
        if (!title) return;
        // Guard: if the string is still two concatenated copies, halve it.
        var half = Math.floor(title.length / 2);
        var a = title.slice(0, half).trim(), b = title.slice(half).trim();
        if (a.length > 10 && a === b) title = a;
        var qty = 1;
        var qtyEl = row.querySelector(
            '[data-a-selector="value"], .sc-quantity-textfield, input[name="quantityBox"], .a-dropdown-prompt'
        );
        if (qtyEl) {
            var q = parseInt(qtyEl.value || qtyEl.textContent, 10);
            if (!isNaN(q) && q > 0) qty = q;
        }
        var priceEl = row.querySelector('.sc-product-price, .sc-badge-price-to-pay .a-offscreen');
        var price = priceEl ? priceEl.textContent.trim() : '';
        out.push({asin: asin, title: title.slice(0, 120), qty: qty, price: price});
    });
    // Dedupe by asin
    var seen = {};
    return out.filter(function(i) {
        if (seen[i.asin]) return false;
        seen[i.asin] = true;
        return true;
    }).slice(0, 80);
})();
"""


async def _scan_amazon_cart(session: aiohttp.ClientSession, store_key: str) -> list[dict]:
    """Scrape an Amazon localmarket cart (Fresh or Whole Foods) precisely.

    Returns [{"asin", "title", "qty", "price"}] — empty list on failure.
    """
    cart_url = STORES[store_key]["cart_url"]
    _log(f"Scanning {STORES[store_key]['name']} cart: {cart_url}")
    nav = await _bridge_post(session, "/browser/navigate", {"url": cart_url}, timeout=40)
    if "error" in nav:
        _log(f"  ✗ Cart nav failed: {nav['error']}")
        return []

    # Cold browser starts render the cart slowly — poll up to ~30s.
    items: list[dict] = []
    for attempt in range(1, 6):
        await asyncio.sleep(6)
        res = await _bridge_post(session, "/browser/js", {"script": _CART_SCAN_JS}, timeout=20)
        if "error" in res:
            _log(f"  ⚠ Cart scan JS error (attempt {attempt}): {res['error']}")
            continue
        raw = res.get("result", [])
        items = [
            i for i in raw
            if isinstance(i, dict) and i.get("title") and i.get("asin")
        ] if isinstance(raw, list) else []
        if items:
            break
        _log(f"  … attempt {attempt}: cart not rendered yet, retrying")

    _log(f"  ✓ Scanned {len(items)} items from the {STORES[store_key]['name']} cart.")
    return items


_TEXT_CART_PROMPT = (
    "The text below is a grocery store's cart page. Extract the CART ITEMS "
    "(product name + quantity). Ignore recommendations, 'saved for later', ads, "
    "nav, and totals. Return ONLY a JSON array like "
    '[{"title": "H-E-B Ground Turkey 93/7", "qty": 1}] — empty array if the '
    "cart is empty or this doesn't look like a cart."
)


async def _scan_text_cart(session: aiohttp.ClientSession, store_key: str) -> list[dict]:
    """Scrape a non-Amazon cart (Target/HEB) via page text + LLM extraction.

    DOM selectors for these carts churn constantly; reading the rendered text
    and letting the LLM pick out the items is far more resilient.
    Returns [{"title", "qty"}] — no stable per-item id, so titles are the key.
    """
    cart_url = STORES[store_key]["cart_url"]
    _log(f"Scanning {STORES[store_key]['name']} cart: {cart_url}")
    nav = await _bridge_post(session, "/browser/navigate", {"url": cart_url}, timeout=40)
    if "error" in nav:
        _log(f"  ✗ Cart nav failed: {nav['error']}")
        return []

    text = ""
    for attempt in range(1, 5):
        await asyncio.sleep(6)
        page = await _bridge_get(session, "/browser/read", timeout=25)
        text = str(page.get("text") or "").strip()
        if len(text) > 400:
            break
        _log(f"  … attempt {attempt}: cart page still thin ({len(text)} chars), retrying")

    if len(text) < 100:
        _log("  ✗ Could not read the cart page.")
        return []

    try:
        resp = await complete(_TEXT_CART_PROMPT, text[:9000], max_tokens=1200)
        match = re.search(r"\[.*\]", resp or "", re.DOTALL)
        raw = json.loads(match.group()) if match else []
    except Exception as exc:
        _log(f"  ✗ LLM cart extraction failed: {exc}")
        return []

    items = [
        {"title": str(i.get("title", "")).strip()[:120],
         "qty": int(i.get("qty", 1) or 1)}
        for i in raw
        if isinstance(i, dict) and str(i.get("title", "")).strip()
    ][:60]
    _log(f"  ✓ Extracted {len(items)} items from the {STORES[store_key]['name']} cart.")
    return items


# Which learn-cart scanner each store uses
_LEARNABLE_STORES = {
    "amazon":      "amazon_dom",
    "whole_foods": "amazon_dom",
    "target":      "text_llm",
    "heb":         "text_llm",
}


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.lower()).strip()


async def _clean_item_names(titles: list[str]) -> dict[str, str]:
    """LLM-normalize raw Amazon titles into short searchable names.

    'Fairlife 2% Reduced Fat Ultra-Filtered Milk, 52 fl oz' → 'fairlife 2% milk'.
    Returns {raw_title: clean_name}; falls back to the raw title on failure.
    """
    if not titles:
        return {}
    batch = titles[:40]
    try:
        resp = await complete(
            system=(
                "Convert each raw Amazon product title into a short plain product name "
                "a grocery search box would find (keep brand if distinctive; drop sizes, "
                "counts, marketing words). Return ONLY a JSON array of the short names, "
                "in the SAME ORDER as the input array, same length. No other text."
            ),
            user=json.dumps(batch),
            max_tokens=1800,
        )
        match = re.search(r"\[.*\]", resp or "", re.DOTALL)
        if match:
            names = json.loads(match.group())
            if isinstance(names, list) and len(names) == len(batch):
                return {t: str(n) for t, n in zip(batch, names) if n}
        _log("  ⚠ Name cleanup returned a mismatched list — using raw titles.")
    except Exception as exc:
        _log(f"  ⚠ Name cleanup failed ({exc}) — using raw titles.")
    return {}


async def _filter_grocery_titles(titles: list[str]) -> list[dict]:
    """From raw Amazon ORDER-HISTORY titles (mixed groceries + electronics +
    household), keep ONLY edible grocery/food items and return short names.
    Returns [{title, name}]; empty on failure or if nothing is food."""
    if not titles:
        return []
    batch = titles[:40]
    try:
        resp = await complete(
            system=(
                "You are given raw Amazon order titles — a mix of groceries, "
                "electronics, household goods, etc. Keep ONLY edible grocery / "
                "food & drink items someone buys at Amazon Fresh. For each kept "
                "item return a short plain product name a grocery search box would "
                "find (keep a distinctive brand; drop sizes, counts, marketing). "
                'Return ONLY a JSON array of {"title": <original>, "name": <short>} '
                "objects. Drop every non-food item entirely. No other text."
            ),
            user=json.dumps(batch),
            max_tokens=1800,
        )
        match = re.search(r"\[.*\]", resp or "", re.DOTALL)
        raw = json.loads(match.group()) if match else []
    except Exception as exc:
        _log(f"  ⚠ Grocery filter failed ({exc}).")
        return []
    out = []
    for i in raw:
        if isinstance(i, dict) and str(i.get("title", "")).strip():
            out.append({
                "title": str(i["title"]).strip()[:120],
                "name": str(i.get("name") or i["title"]).strip()[:80],
            })
    return out


def _load_usual_order(r: redis_lib.Redis) -> list[dict]:
    """Return the learned usual-order items (may be empty)."""
    try:
        data = json.loads(r.get(USUAL_ORDER_KEY) or "{}")
        return data.get("items", []) or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# LLM meal planning + grocery list generation (fitness-aware, cohesive)
# ---------------------------------------------------------------------------
async def _generate_smart_list(
    past_purchases: list[str], profile: dict, targets: dict,
    usual_order: list[dict] | None = None, intake: dict | None = None,
) -> tuple[list[dict], list[str], dict[str, int]]:
    """Plan a cohesive week of meals, then derive the consolidated shopping list.

    Returns (meals, grocery_list, qty_map):
      meals        — [{"name", "kind": breakfast|lunch|dinner|snack, "servings_per_week",
                       "ingredients": [...]}]
      grocery_list — deduped ingredient/staple strings to actually shop for
      qty_map      — item -> retail units to buy for the week

    Quantity is kept OUT of the item name and carried here instead. The name has
    to stay a bare product string because it is fed to each store's search box,
    where "chicken tenderloins 4 lbs" matches nothing; but without a separate
    quantity every line was added once, so a week's plan arrived as one pack of
    each and ran out midweek.

    Planning meals FIRST (with deliberately overlapping ingredients) is what makes the
    items cohesive — everything bought maps to real meals instead of a random pile.
    """
    _log("Step 2: Planning cohesive meals + grocery list via LLM…")

    prefs      = profile.get("dietary_preferences", ["high protein"])
    budget     = float(profile.get("weekly_budget_usd", 150))
    goal       = profile.get("goal", "cutting")
    fav_meals  = profile.get("favorite_meals", [])
    preferred  = profile.get("preferred_items", [])
    avoid      = profile.get("avoid_items", [])

    pref_block = ""
    if usual_order:
        staples = ", ".join(
            f"{u.get('name') or u.get('title')} (x{u.get('qty', 1)})"
            for u in usual_order[:25]
        )
        pref_block += (
            "The user's USUAL ORDER — items they habitually buy, learned from their own "
            f"Amazon Fresh cart, with typical quantities: {staples}.\n"
            "This is the STRONGEST signal of what they actually eat. Build the week's "
            "meals and list AROUND these staples first (skip any that conflict with the "
            "avoid list), then add only what's needed to complete the meals and hit the "
            "macro targets.\n"
        )
    if fav_meals:
        pref_block += f"The user EATS THESE MEALS OFTEN — feature them prominently: {'; '.join(fav_meals)}.\n"
    if preferred:
        pref_block += f"Strongly PREFER these exact staples: {', '.join(preferred)}.\n"
    if avoid:
        pref_block += f"AVOID / substitute these (use the preferred alternative instead): {', '.join(avoid)}.\n"

    # What the user actually ate, from Sage's logs. Only mentioned when there is
    # a real gap: telling the model "you are on target" every week is noise that
    # dilutes the instructions that matter.
    intake_block = ""
    if intake and intake.get("days_logged"):
        avg_p = intake["avg_protein_g"]
        target_p = targets["protein_g"]
        if target_p and avg_p < target_p * 0.85:
            intake_block = (
                f"LOGGED INTAKE: over the last {intake['days_logged']} logged day(s) the user "
                f"averaged {avg_p}g protein against a {target_p}g target, and "
                f"{intake['avg_calories']} kcal against {targets['target_calories']}. They are "
                f"consistently UNDER on protein — weight this week's meals and quantities "
                f"toward easy, high-protein items they will actually eat.\n"
            )
        elif target_p and avg_p > target_p * 1.15:
            intake_block = (
                f"LOGGED INTAKE: the user averaged {avg_p}g protein against a {target_p}g "
                f"target over {intake['days_logged']} logged day(s) — already comfortably "
                f"over. Do not add more protein at the expense of vegetables and fibre.\n"
            )

    system_prompt = (
        f"You are JARVIS, a British AI assistant planning a week of meals for {goal}.\n"
        f"User stats: {profile['weight_lbs']} lbs, {profile['height_in']} in, "
        f"age {profile.get('age', 25)}, {profile.get('activity_level', 'moderately_active')}.\n"
        f"Daily targets: {targets['target_calories']} cal, {targets['protein_g']}g protein, "
        f"{targets['fiber_g']}g fiber. Dietary preferences: {', '.join(prefs)}. "
        f"Weekly budget: ${budget:.0f}.\n\n"
        f"{pref_block}{intake_block}\n"
        "Plan a COHESIVE week of simple, repeatable meals whose ingredients deliberately "
        "OVERLAP so the shop is efficient and nothing is wasted. Build the week AROUND the "
        "user's favourite meals above, then add complementary meals reusing those ingredients. "
        "THEN output the consolidated shopping list of exactly the ingredients those meals "
        "require, plus any breakfast/snack staples.\n"
        "Rules: 6-9 distinct meals across breakfast/lunch/dinner/snack, REPEATED to cover "
        "a full 7 days (~21 eating occasions) — say how many servings of each meal the week "
        "needs; high protein-per-dollar proteins; high-fibre veg/fruit; reuse ingredients "
        "across meals; spend most of the budget; favour items the user has bought before. "
        "Every grocery_list item must be used by at least one meal.\n"
        "QUANTITY IS CRITICAL. Work out how much of each ingredient 7 DAYS of those servings "
        "actually needs, then set qty to the number of retail units to buy (one pack, bag, "
        "carton or bunch = 1). A week of chicken for one adult is several packs, not one. "
        "A single unit of everything is the most common failure here and leaves the user out "
        "of food by midweek — if in doubt, round UP.\n"
        "The item name MUST stay a plain product name a store search box can find, with NO "
        "quantity, size or weight in the NAME (write 'chicken tenderloins' with qty 3, NOT "
        "'chicken tenderloins 4 lbs'). The amount goes in qty, never in the name.\n\n"
        "Return ONLY valid JSON, no markdown:\n"
        '{"meals": [{"name": "Basmati ground beef bowl with roasted veggies", "kind": "dinner", '
        '"servings_per_week": 4, '
        '"ingredients": ["ground beef", "basmati rice", "bell peppers", "herdez guacamole salsa"]}], '
        '"grocery_list": [{"item": "ground beef", "qty": 3}, {"item": "basmati rice", "qty": 1}, '
        '{"item": "bell peppers", "qty": 2}, {"item": "herdez guacamole salsa", "qty": 1}]}'
    )

    user_msg = (
        f"User's recent Amazon purchases:\n{json.dumps(past_purchases[:20], indent=2)}\n\n"
        "Plan the meals and consolidated shopping list now."
    )

    response = await complete(system=system_prompt, user=user_msg, max_tokens=1400)

    try:
        match = re.search(r"\{.*\}", response or "", re.DOTALL)
        if match:
            data = json.loads(match.group())
            if isinstance(data, dict):
                meals = [m for m in data.get("meals", [])
                         if isinstance(m, dict) and m.get("name")][:10]
                # Items arrive as {"item": name, "qty": n}. Plain strings are
                # still accepted so an older-format reply (or a model that
                # ignores the schema) degrades to qty 1 rather than a parse
                # failure that drops the whole plan onto fallback staples.
                glist, qty_map = [], {}
                for entry in data.get("grocery_list", [])[:24]:
                    if isinstance(entry, dict):
                        name = str(entry.get("item") or entry.get("name") or "").strip()
                        try:
                            qty = int(entry.get("qty", 1) or 1)
                        except (TypeError, ValueError):
                            qty = 1
                    else:
                        name, qty = str(entry).strip(), 1
                    if not name:
                        continue
                    # Cap per item: a hallucinated qty of 40 would blow the
                    # budget on one line before anything else got bought.
                    glist.append(name)
                    qty_map[name] = max(1, min(qty, 6))
                if glist:
                    units = sum(qty_map.values())
                    _log(f"  ✓ Planned {len(meals)} meals, {len(glist)} grocery items "
                         f"({units} units for the week).")
                    return meals, glist, qty_map
    except (json.JSONDecodeError, AttributeError):
        pass

    _log("  ⚠ LLM meal-plan parse failed — using fallback staples (no meal plan).")
    fallback = [
        "chicken breast boneless skinless", "large eggs", "non-fat greek yogurt",
        "low-fat cottage cheese", "93% lean ground beef", "tuna canned in water",
        "broccoli", "spinach", "sweet potato", "brown rice", "oatmeal rolled oats",
        "almonds", "blueberries", "olive oil",
    ]
    return [], fallback, {item: 1 for item in fallback}


# ---------------------------------------------------------------------------
# Cart automation — add the EXACT resolved product (visible browser)
#
# We navigate straight to the product-detail page we already matched and priced,
# then click its Add-to-Cart control. Because we go to the resolved href (not
# "first search result"), the item added is the item we compared on price.
# ---------------------------------------------------------------------------
# Selectors are per STORE, not one global list.
#
# They used to be a single ordered list tried against every store, which caused
# the wrong-cart bug: on an Amazon Fresh page where #freshAddToCartButton had
# not rendered yet, the loop fell through to #add-to-cart-button — the RETAIL
# button — and the item landed in the retail cart while we reported success.
# The two carts are separate, so that is silent data loss, not a near miss.
#
# Each store now sees only its own controls. Amazon Fresh and Whole Foods also
# opt out of the generic text fallback ("add to cart"), because on those pages
# the text that matches is usually the retail control.
_STORE_ADD_SELECTORS = {
    "amazon": [
        "#freshAddToCartButton",       # Fresh, in fpw=alm context
        "#addtodeliverystore",         # Fresh "Add to Delivery"
        "#sc-alm-buy-box-add-button",
    ],
    "whole_foods": [
        "#freshAddToCartButton",
        "#addtodeliverystore",
        "#sc-alm-buy-box-add-button",
    ],
    "target": [
        '[data-test="shippingButton"]',
        '[data-test="orderPickupButton"]',
        '[data-test="addToCartButton"]',
        '[data-test="chooseOptionsButton"]',
    ],
    # Verified against the live page 2026-09-09: HEB's attribute is
    # data-qe-id="addToCart". The old "addToCartButton" value matched NOTHING,
    # so every HEB add fell through to the generic text fallback below.
    "heb": [
        'button[data-qe-id="addToCart"]',
        'button[data-qe-id="addToCartButton"]',      # kept in case it returns
        'button[data-qa-automation="addToCart"]',
        'button[aria-label*="Add to cart" i]',
        'button[class*="AddToCart"]',
        '[data-testid="add-to-cart-button"]',
    ],
}

# Stores where a generic "add to cart" text match is DANGEROUS rather than a
# helpful fallback — on Amazon those pages carry the retail control too.
# HEB joins these for the same reason Amazon is here. A product page carries
# FOUR "Add to cart" buttons — the real one, a hidden duplicate, and one per
# recommendation card ("Add to cart, Fresh Bunch of Organic Bananas", "Add to
# cart, Dole Fresh Mini Bananas"). The text fallback takes the first match, so
# it can silently add a DIFFERENT product than the one whose price was compared.
_NO_TEXT_FALLBACK = {"amazon", "whole_foods", "heb"}


def _add_to_cart_js(store_key: str) -> str:
    """Build the click script for one store.

    Returns 'no_add_button' rather than clicking something plausible: a wrong
    click is worse than no click, because it lands in a different cart and we
    would report it as success.
    """
    selectors = _STORE_ADD_SELECTORS.get(store_key, [])
    allow_text = store_key not in _NO_TEXT_FALLBACK
    return (
        "(function() {\n"
        f"    var selectors = {json.dumps(selectors)};\n"
        "    for (var s of selectors) {\n"
        "        var btn = document.querySelector(s);\n"
        "        if (btn && !btn.disabled) { btn.click(); return 'clicked:' + s; }\n"
        "    }\n"
        + (
        "    var els = Array.from(document.querySelectorAll('button, input[type=\"submit\"]'));\n"
        "    var m = els.find(function(el) {\n"
        "        return /add to cart|add item|add for|add to order/i.test(\n"
        "            el.innerText || el.value || el.getAttribute('aria-label') || ''\n"
        "        );\n"
        "    });\n"
        "    if (m) { m.click(); return 'clicked:text_match'; }\n"
        if allow_text else ""
        ) +
        "    return 'no_add_button';\n"
        "})();"
    )


# HEB's own add-to-cart GraphQL mutation, captured from the live site while a
# human clicked the button. Clicking is what does not work: the button fires
# analytics but never issues a cart request (see the note in
# _add_product_to_cart), so the UI path silently no-ops behind Incapsula.
# Calling the mutation directly, from inside the page so it carries the
# logged-in cookies and same-origin headers, does work.
#
# quantity is ABSOLUTE — it SETS the line to N rather than adding N. Verified:
# a hand-click made it 1, then quantity:2 produced "quantity: 2 eachs" and
# $2.44, not 3. So this is called once per item, never in a loop.
_HEB_ADD_QUERY_HASH = "740b6637fc54740a4cb3e0e734845c66727b1c5b89af7c95a0695bb6eb89b4d9"

_HEB_IDS_JS = """(function(){
  var out = {productId: (location.pathname.match(/\\/(\\d+)(?:\\?|$)/)||[])[1] || null, skuId: null};
  var el = document.getElementById("__NEXT_DATA__");
  if (el) {
    var m = el.textContent.match(/"SKUs"\\s*:\\s*\\[\\s*\\{\\s*"id"\\s*:\\s*"([^"]+)"/);
    if (m) out.skuId = m[1];
  }
  return out;
})()"""


def _heb_add_js(product_id: str, sku_id: str, quantity: int) -> str:
    """In-page fetch of HEB's cart mutation, so it inherits the session."""
    payload = json.dumps({
        "operationName": "cartItemV2",
        "variables": {
            "userIsLoggedIn": True,
            "productId": str(product_id),
            "skuId": str(sku_id),
            "purchasePreferenceId": "default",
            "quantity": int(quantity),
        },
        "extensions": {"persistedQuery": {"version": 1,
                                          "sha256Hash": _HEB_ADD_QUERY_HASH}},
    })
    return (
        "(function(){return fetch('https://www.heb.com/graphql',{method:'POST',"
        "credentials:'include',headers:{'Content-Type':'application/json'},"
        f"body:JSON.stringify({payload})"
        "}).then(function(r){return r.text().then(function(t){"
        "return {status:r.status, body:t.slice(0,300)};});})"
        ".catch(function(e){return {status:0, body:String(e).slice(0,200)};});})()"
    )


async def _add_heb_via_graphql(
    session: aiohttp.ClientSession, quantity: int
) -> tuple[bool, str]:
    """Add the CURRENTLY OPEN HEB product page to the cart via its own API.

    Assumes the caller has already navigated to the product page, because both
    ids come from that page: productId from the URL and skuId from the Next.js
    payload.

    Returns (ok, note). Never raises — any failure hands back to the click path,
    which is no worse than where we started.
    """
    ids = await _bridge_post(session, "/browser/js", {"script": _HEB_IDS_JS}, timeout=20)
    got = ids.get("result") or {}
    product_id, sku_id = got.get("productId"), got.get("skuId")
    if not product_id or not sku_id:
        return False, f"could not read HEB ids from the page (got {got})"

    res = await _bridge_post(session, "/browser/js",
                             {"script": _heb_add_js(product_id, sku_id, quantity)},
                             timeout=30)
    out = res.get("result") or {}
    body = str(out.get("body") or "")
    if out.get("status") == 200 and "addItemToCartV2" in body:
        return True, f"Added {quantity}x to HEB via API (sku {sku_id})."
    # PersistedQueryNotFound means HEB redeployed and the hash moved on. Worth
    # naming explicitly: it is the one failure that is expected eventually, and
    # the fix is to re-capture the hash rather than to debug the request.
    if "PersistedQueryNotFound" in body:
        return False, "HEB persisted-query hash is stale — re-capture it"
    return False, f"HEB API add failed (status {out.get('status')}): {body[:120]}"


# Target's own cart API, captured the same way HEB's was: watch the traffic
# while a human clicks Add to cart once, then replay it.
#
#   POST https://carts.target.com/web_checkouts/v1/cart_items?key=…
#   {"cart_item":{"item_channel_id":"10","tcin":"15013944","quantity":1},
#    "fulfillment":{"type":"PICKUP","location_id":2550,"ship_method":"STORE_PICKUP"},
#    "cart_type":"REGULAR","channel_id":"10","shopping_context":"DIGITAL"}
#   -> 201 {"cart_id":…,"cart_item_id":…,"total_cart_item_quantity":2}
#
# QUANTITY IS ADDITIVE HERE, and that is the opposite of HEB. Verified rather
# than assumed: a line already at 1 plus a replay of quantity:2 came back as
# quantity 3, where the same move on HEB SETS the line to 2. Sending the week's
# figure to both stores therefore means two different things, so neither can be
# treated as the general case. Remy builds a cart from empty, where additive and
# absolute agree — but a retry, or an item already in the cart, is exactly where
# they diverge, and Target would silently double.
#
# Success is 201 Created, not 200.
#
# The tcin is the number in the product URL (/A-15013944), so it needs no page
# scraping the way HEB's skuId did. The key is Target's public web API key,
# visible in every request the site makes; it identifies the client, not the
# user — the SESSION is what authenticates, which is why this has to run inside
# the logged-in page.
_TARGET_CART_URL = ("https://carts.target.com/web_checkouts/v1/cart_items"
                    "?field_groups=CART%2CCART_ITEMS%2CSUMMARY&key=")
_TARGET_WEB_KEY = os.environ.get(
    "TARGET_WEB_API_KEY", "9f36aeafbe60771e321a7cc95a78140772ab3e96").strip()
# Store to fulfil from. Captured from a real add; override per household.
_TARGET_STORE_ID = int(os.environ.get("TARGET_STORE_ID", "2550"))


def _target_tcin(href: str) -> str:
    """The TCIN out of a Target product URL (…/-/A-15013944)."""
    m = re.search(r"/A-(\d+)", href or "")
    return m.group(1) if m else ""


def _target_add_js(tcin: str, quantity: int) -> str:
    """In-page fetch of Target's cart API, inheriting the logged-in session."""
    payload = json.dumps({
        "cart_item": {"item_channel_id": "10", "tcin": str(tcin),
                      "quantity": int(quantity)},
        "fulfillment": {"type": "PICKUP", "location_id": _TARGET_STORE_ID,
                        "ship_method": "STORE_PICKUP"},
        "cart_type": "REGULAR", "channel_id": "10", "shopping_context": "DIGITAL",
    })
    url = _TARGET_CART_URL + _TARGET_WEB_KEY
    return (
        "(function(){return fetch('" + url + "',{method:'POST',"
        "credentials:'include',headers:{'Content-Type':'application/json'},"
        f"body:JSON.stringify({payload})"
        "}).then(function(r){return r.text().then(function(t){"
        "return {status:r.status, body:t.slice(0,300)};});})"
        ".catch(function(e){return {status:0, body:String(e).slice(0,200)};});})()"
    )


async def _add_target_via_api(
    session: aiohttp.ClientSession, product: PriceResult, quantity: int
) -> tuple[bool, str]:
    """Add a Target item via its cart API. Never raises; False hands back to clicking."""
    tcin = _target_tcin(product.href or "")
    if not tcin:
        return False, f"no TCIN in Target URL ({str(product.href)[:60]})"

    res = await _bridge_post(session, "/browser/js",
                             {"script": _target_add_js(tcin, quantity)}, timeout=30)
    out = res.get("result") or {}
    body = str(out.get("body") or "")
    # 201 Created is the success here, not 200.
    if out.get("status") in (200, 201) and "cart_item_id" in body:
        return True, f"Added {quantity}x to Target via API (tcin {tcin})."
    return False, f"Target API add failed (status {out.get('status')}): {body[:120]}"


async def _add_product_to_cart(
    session: aiohttp.ClientSession, product: PriceResult, quantity: int = 1
) -> tuple[str, str]:
    """Navigate to the resolved product page and add it to the cart.

    ``quantity`` is the number of retail units the week's plan needs. It is
    added by clicking Add-to-Cart repeatedly rather than by driving each store's
    quantity widget: the widget is a different control on every store (select,
    stepper, text input) and silently does nothing when mis-targeted, whereas
    the add button is the one control already proven to work per store. Slower,
    but a wrong quantity here is the difference between a week of food and two
    days of it, and a silent failure would look exactly like success.
    """
    store = STORES[product.store]
    if not product.href:
        return "not_found", "No resolved product URL to add."

    url = product.href if product.href.startswith("http") else store["product_base"] + product.href
    # For Amazon Fresh / Whole Foods, force the product page into its Fresh shopping
    # context so the add lands in the Fresh cart (not the regular retail cart).
    brand = store.get("alm_brand_id")
    if brand:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}almBrandId={brand}&fpw=alm"
    _log(f"  [cart] {store['name']}: {product.title[:45]}")

    nav = await _bridge_post(session, "/browser/navigate", {"url": url}, timeout=40)
    if "error" in nav:
        return "error", f"Navigation failed: {nav['error']}"
    await asyncio.sleep(4)

    want = max(1, min(int(quantity or 1), 6))

    # HEB: use its own cart API. Clicking does not work there at all — the
    # button fires analytics and never issues a cart request — so the click
    # loop below would report success against an unchanged cart. The API also
    # takes quantity directly, so a week's worth is one call rather than N.
    if product.store == "heb":
        ok, note = await _add_heb_via_graphql(session, want)
        if ok:
            return "added", note
        _log(f"  ⚠ HEB API add failed ({note}) — falling back to clicking", "warn")

    # Target: same reasoning as HEB — its own API, with quantity in one call.
    if product.store == "target":
        ok, note = await _add_target_via_api(session, product, want)
        if ok:
            return "added", note
        _log(f"  ⚠ Target API add failed ({note}) — falling back to clicking", "warn")

    added = 0
    for attempt in range(want):
        if attempt:
            # The button re-renders after an add ("Added to cart", stepper
            # appears), so give the page a moment before clicking again.
            await asyncio.sleep(2)
        res = await _bridge_post(session, "/browser/js",
                                 {"script": _add_to_cart_js(product.store)}, timeout=15)
        val = str(res.get("result", "")).lower()
        if "clicked" not in val:
            if added == 0:
                return "not_found", f"No Add-to-Cart button found ({val})."
            # Partial success is reported honestly rather than as "added": the
            # plan assumed a week's worth and the cart has less than that.
            return "added", (f"Added {added} of {want} to {store['name']} "
                             f"(button stopped responding after {added}).")
        added += 1

    if want > 1:
        return "added", f"Added {added}x to {store['name']}."
    return "added", f"Added to {store['name']}."


async def _verify_cart(session: aiohttp.ClientSession, store_key: str) -> dict:
    """Navigate to the store's REAL cart and read the actual item count + subtotal.

    This is cart-truth: it catches adds that were clicked but didn't persist, and
    surfaces the store's real subtotal (which can differ from our summed prices
    due to unit sizes, promos, or unavailable items).
    """
    url = STORES[store_key]["cart_url"]
    nav = await _bridge_post(session, "/browser/navigate", {"url": url}, timeout=40)
    if "error" in nav:
        return {}
    await asyncio.sleep(4)
    page = await _bridge_get(session, "/browser/read", timeout=20)
    text = str(page.get("text") or "")

    out: dict = {}

    # HEB prices, stock and cart are all per-STORE, and Austin has several. A
    # cart built against the wrong location looks completely normal — right
    # items, plausible prices — and only fails at pickup. Record which store the
    # page says we are shopping so the report can show it, and flag a mismatch
    # against HEB_STORE when one is configured.
    if store_key == "heb":
        m_store = re.search(r"(?:Shopping at|Store|Pickup at|Curbside at)[:\s]+"
                            r"([A-Z][A-Za-z0-9 .'&/-]{3,40})", text)
        if m_store:
            out["store_location"] = m_store.group(1).strip()
        expected = os.environ.get("HEB_STORE", "").strip()
        if expected and out.get("store_location"):
            if expected.lower() not in out["store_location"].lower():
                out["store_mismatch"] = f"expected {expected}, page says {out['store_location']}"
                _log(f"  ⚠ HEB store mismatch: {out['store_mismatch']}", "warn")

    # A cart page can carry several "Subtotal (N items): $X" lines (active cart,
    # saved-for-later, buy-again). The main cart is the one with the most items.
    pairs = re.findall(r"[Ss]ubtotal\s*\((\d+)\s*items?\)\s*:?\s*\$?([\d,]+\.\d{2})", text)
    if pairs:
        cnt, sub = max(pairs, key=lambda p: int(p[0]))
        out["count"] = int(cnt)
        out["subtotal"] = float(sub.replace(",", ""))
        return out
    mc = re.search(r"\((\d+)\s*items?\)", text)
    if not mc:
        # "12 items in your cart" / "Cart (12)" — HEB and Target phrasings that
        # the parenthesised form above misses.
        mc = re.search(r"(\d+)\s*items?\s+in\s+(?:your\s+)?cart", text, re.I) \
             or re.search(r"[Cc]art\s*\((\d+)\)", text)
    if mc:
        out["count"] = int(mc.group(1))
    ms = re.search(r"[Ss]ubtotal[^$]{0,30}\$([\d,]+\.\d{2})", text)
    if not ms:
        ms = re.search(r"(?:Estimated\s+)?[Tt]otal[^$]{0,30}\$([\d,]+\.\d{2})", text)
    if ms:
        out["subtotal"] = float(ms.group(1).replace(",", ""))
    return out


# ---------------------------------------------------------------------------
# Checkout automation (approval flow)
# ---------------------------------------------------------------------------
async def _proceed_to_checkout(
    session: aiohttp.ClientSession, store_key: str
) -> tuple[bool, str]:
    """Navigate to cart and click Proceed to Checkout."""
    cart_url = STORES[store_key]["cart_url"]
    store_name = STORES[store_key]["name"]
    _log(f"  [checkout] Navigating to {store_name} cart…")

    nav = await _bridge_post(session, "/browser/navigate", {"url": cart_url}, timeout=40)
    if "error" in nav:
        return False, f"Cannot open {store_name} cart: {nav['error']}"
    await asyncio.sleep(4)

    checkout_js = """
    (function() {
        var selectors = [
            'input[name="proceedToRetailCheckout"]',
            '[data-test="proceed-to-checkout-button"]',
            '[data-test="checkout-button"]',
            'a[href*="checkout"]',
            'button[name*="checkout"]',
        ];
        for (var s of selectors) {
            var btn = document.querySelector(s);
            if (btn) { btn.click(); return 'clicked:' + s; }
        }
        var all = Array.from(document.querySelectorAll('button, a, input[type="submit"]'));
        var match = all.find(function(el) {
            return /proceed to checkout|go to checkout|checkout|place order/i.test(
                el.innerText || el.value || el.getAttribute('aria-label') || ''
            );
        });
        if (match) { match.click(); return 'clicked:text_match'; }
        return 'no_checkout_button';
    })();
    """
    res = await _bridge_post(session, "/browser/js", {"script": checkout_js}, timeout=15)
    val = str(res.get("result", "")).lower()
    if "clicked" in val:
        _log(f"  ✓ Checkout initiated at {store_name}")
        await asyncio.sleep(3)
        return True, f"Checkout started at {store_name}."
    return False, f"Could not find checkout button at {store_name} ({val})."


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------
def _build_report(
    profile: dict,
    targets: dict,
    selected_stores: list[str],
    order_results: list[OrderResult],
    total_spend: float,
    total_savings: float,
    cart_truth: dict | None = None,
    meals: list[dict] | None = None,
) -> str:
    cart_truth = cart_truth or {}
    meals = meals or []
    budget        = float(profile.get("weekly_budget_usd", 150))
    added         = [r for r in order_results if r.status == "added"]
    failed        = [r for r in order_results if r.status in ("not_found", "error")]
    skipped       = [r for r in order_results if r.status == "budget_exceeded"]
    week_of       = datetime.now(timezone.utc).strftime("%B %-d, %Y")

    store_lines = {}
    store_totals: dict[str, float] = defaultdict(float)
    for r in added:
        key = r.assigned_store
        store_totals[key] += r.best_price or 0
        if key not in store_lines:
            store_lines[key] = []
        price_str = f"${r.best_price:.2f}" if r.best_price else "N/A"
        # Show the actual product matched/added, not just the search phrase
        product = (r.title or r.item)[:40]
        store_lines[key].append(f"  ✓ {product:<40} {price_str:>7}")

    height_ft  = int(profile["height_in"]) // 12
    height_in  = int(profile["height_in"]) % 12
    goal_label = profile.get("goal", "cutting").capitalize()

    lines = [
        "═══════════════════════════════════════════",
        "      J·A·R·V·I·S  GROCERY REPORT",
        f"      Week of {week_of}",
        "═══════════════════════════════════════════",
        "",
        "PROFILE",
        f"  Goal: {goal_label} | {profile['weight_lbs']} lbs | {height_ft}'{height_in}\" | Age {profile.get('age', '?')}",
        f"  TDEE: {targets['tdee']:,} cal/day → Target: {targets['target_calories']:,} cal/day (-{targets['deficit']})",
        f"  Protein: {targets['protein_g']}g/day | Fiber: {targets['fiber_g']}g/day | Budget: ${budget:.0f}/wk",
        "",
        f"ORDERING FROM ({len(selected_stores)} store{'s' if len(selected_stores) > 1 else ''})",
    ]
    for s in selected_stores:
        lines.append(f"  ✦ {STORES[s]['name']} — {len(store_lines.get(s, []))} items — ${store_totals[s]:.2f}")

    for s in selected_stores:
        if s in store_lines:
            lines += ["", f"── {STORES[s]['name'].upper()} ──"]
            lines += store_lines[s]

    if failed:
        lines += ["", f"NOT FOUND ({len(failed)} items)"]
        for r in failed:
            lines.append(f"  ✗ {r.item}: {r.note}")

    if skipped:
        lines.append(f"\n  ⚠ {len(skipped)} items skipped (budget cap reached)")

    lines += [
        "",
        f"TOTAL: ${total_spend:.2f} / ${budget:.0f}",
        f"Budget remaining: ${budget - total_spend:.2f}",
    ]
    if total_savings > 0:
        lines.append(f"Savings vs. most expensive options: ${total_savings:.2f}")

    # Meals you can make from this shop (cohesive plan the list was built from)
    if meals:
        lines += ["", f"MEALS THIS WEEK ({len(meals)})"]
        for m in meals:
            kind = m.get("kind", "")
            tag = f"[{kind}] " if kind else ""
            ings = ", ".join(m.get("ingredients", [])[:6])
            lines.append(f"  • {tag}{m.get('name','')}")
            if ings:
                lines.append(f"      {ings}")
        lines.append("  Ask Jarvis: “what can I make with my groceries?”")

    # Cart-truth: what the store's real cart actually shows post-add
    if any(cart_truth.get(s) for s in selected_stores):
        lines += ["", "IN YOUR CART (verified)"]
        n_added = len([r for r in order_results if r.status == "added"])
        for s in selected_stores:
            v = cart_truth.get(s) or {}
            if not v:
                continue
            cnt = v.get("count")
            sub = v.get("subtotal")
            parts = []
            if cnt is not None:
                parts.append(f"{cnt} items")
            if sub is not None:
                parts.append(f"subtotal ${sub:.2f}")
            loc = v.get("store_location")
            if loc:
                parts.append(f"@ {loc}")
            lines.append(f"  {STORES[s]['name']:15} {', '.join(parts)}")
            if cnt is not None and cnt != n_added:
                lines.append(f"     ⚠ expected {n_added} added — cart shows {cnt}; review before checkout")
            # A cart built against the wrong HEB looks entirely normal until
            # pickup, so say so loudly rather than leaving it in the data.
            if v.get("store_mismatch"):
                lines.append(f"     ⚠ WRONG STORE — {v['store_mismatch']}")

    lines += ["", "CART LINKS"]
    for s in selected_stores:
        lines.append(f"  {STORES[s]['name']:15} → {STORES[s]['cart_url']}")

    lines += [
        "",
        "─────────────────────────────────────────────",
        "Reply 'approve grocery order' to Jarvis to",
        "proceed to checkout at each selected store.",
        "═══════════════════════════════════════════",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report delivery
# ---------------------------------------------------------------------------
async def _send_imessage(session: aiohttp.ClientSession, phone: str, message: str) -> None:
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
    result = await _bridge_post(session, "/applescript", {"script": script, "timeout": 30})
    if result.get("error"):
        _log(f"  ✗ iMessage failed: {result['error']}")
    else:
        _log(f"  ✓ iMessage sent to {phone}")


def _push_to_ios_app(text: str) -> None:
    try:
        import paho.mqtt.publish as mqtt_pub
        mqtt_pub.single(
            "jarvis/surfaces/iphone/push",
            json.dumps({"text": text}),
            hostname=MQTT_HOST,
            port=int(os.environ.get("MQTT_PORT", 1883)),
        )
        _log("  ✓ iOS app push sent")
    except Exception as exc:
        _log(f"  ✗ iOS push failed: {exc}")


# ---------------------------------------------------------------------------
# Main Agent
# ---------------------------------------------------------------------------
class GroceryAgent(BaseAgent):
    """
    Smart grocery agent — v5.0

    Normal mode workflow:
      0. Health-check Mac Bridge.
      1. Load user fitness profile from Redis.
      2. Calculate TDEE + macro targets.
      3. Pull Amazon order history (visible browser).
      4. LLM generates fitness-aware grocery list.
      5. Price-compare via Target API / HEB API / headless scraper.
      6. Select 1-2 stores (greedy coverage algorithm).
      7. Add items to carts (visible browser).
      8. Save browser session state.
      9. Store pending order in Redis.
     10. Send report via iMessage + iOS push.

    Checkout mode (params["action"] == "checkout"):
      1. Load pending order from Redis.
      2. Navigate visible browser to each cart.
      3. Click Proceed to Checkout.
      4. Update Redis status to "placed".
      5. Send confirmation.
    """

    async def run(self) -> str:
        global _EMIT
        _EMIT = self.log_event   # route _log() into the observability stream
        action = (self.params or {}).get("action", "")

        if action == "checkout":
            return await self._run_checkout()
        if action == "learn_cart":
            return await self._run_learn_cart()
        if action == "learn_orders":
            return await self._run_learn_orders()
        return await self._run_full()

    # ------------------------------------------------------------------
    # Learn the user's usual order from their current Amazon Fresh cart
    # ------------------------------------------------------------------

    async def _run_learn_cart(self) -> str:
        # Which carts to scan: params["stores"] = "all", one key, or a list.
        req = (self.params or {}).get("stores", "amazon")
        if req == "all":
            store_keys = list(_LEARNABLE_STORES)
        elif isinstance(req, str):
            store_keys = [req]
        else:
            store_keys = list(req)
        store_keys = [s for s in store_keys if s in _LEARNABLE_STORES]
        if not store_keys:
            return f"No scannable store in {req!r}. Options: {', '.join(_LEARNABLE_STORES)}, or 'all'."

        _log(f"=== Grocery Agent | learn_cart: scanning {', '.join(store_keys)} ===")
        async with aiohttp.ClientSession() as session:
            if not await _check_bridge(session):
                return f"Mac Bridge unreachable at {MAC_BRIDGE_URL}. Cannot scan carts."

            scanned: list[dict] = []           # each: {title, qty, asin?, store}
            per_store: dict[str, int] = {}
            for sk in store_keys:
                if _LEARNABLE_STORES[sk] == "amazon_dom":
                    found = await _scan_amazon_cart(session, sk)
                else:
                    found = await _scan_text_cart(session, sk)
                per_store[sk] = len(found)
                for f in found:
                    f["store"] = sk
                scanned.extend(found)

            if not scanned:
                return (
                    f"I couldn't read any items from the {', '.join(store_keys)} cart(s). "
                    "Make sure the carts have items and the browser session is logged in "
                    "(open the Jarvis browser and sign in once), then try again."
                )

            # Merge into the persistent usual-order profile
            try:
                data = json.loads(self.r.get(USUAL_ORDER_KEY) or "{}")
            except Exception:
                data = {}
            existing: list[dict] = data.get("items", []) or []
            by_norm = {_norm_title(e.get("title", "")): e for e in existing}

            now = datetime.now(timezone.utc).isoformat()
            new_titles = [
                i["title"] for i in scanned
                if _norm_title(i["title"]) not in by_norm
            ]
            clean_names = await _clean_item_names(new_titles)

            added, updated = 0, 0
            for item in scanned:
                key = _norm_title(item["title"])
                if key in by_norm:
                    entry = by_norm[key]
                    entry["count"] = int(entry.get("count", 1)) + 1
                    entry["qty"] = item.get("qty", entry.get("qty", 1))
                    entry["last_seen"] = now
                    entry.setdefault("store", item.get("store", "amazon"))
                    updated += 1
                else:
                    by_norm[key] = {
                        "title": item["title"],
                        "name": clean_names.get(item["title"], item["title"]),
                        "asin": item.get("asin", ""),
                        "store": item.get("store", "amazon"),
                        "qty": item.get("qty", 1),
                        "count": 1,
                        "first_seen": now,
                        "last_seen": now,
                    }
                    added += 1

            # Most-seen first; cap so the prompt stays bounded
            items = sorted(by_norm.values(), key=lambda e: -int(e.get("count", 1)))[:120]
            self.r.set(USUAL_ORDER_KEY, json.dumps({"items": items, "updated": now}))

        names = ", ".join(i["name"] for i in items[:12] if i.get("name"))
        by_store = ", ".join(
            f"{STORES[s]['name']}: {n}" for s, n in per_store.items()
        )
        report = (
            f"Learned your usual order ({by_store}): {added} new item(s), "
            f"{updated} seen before — {len(items)} staples on file now. "
            f"Top items: {names}. Future weekly grocery lists will be built around these."
        )
        _log(f"  ✓ {report}")
        return report

    async def _run_learn_orders(self) -> str:
        """Learn the usual order from the user's Amazon ORDER HISTORY (past
        purchases) instead of the current cart — food items only."""
        _log("=== Grocery Agent | learn_orders: reading Amazon order history ===")
        async with aiohttp.ClientSession() as session:
            if not await _check_bridge(session):
                return f"Mac Bridge unreachable at {MAC_BRIDGE_URL}. Cannot read order history."

            titles = await _fetch_amazon_order_history(session)
            if not titles:
                return (
                    "I couldn't read your Amazon order history. Open the Jarvis "
                    "browser and sign in to Amazon once, then try again."
                )

            grocery = await _filter_grocery_titles(titles)
            if not grocery:
                return (
                    f"I read {len(titles)} recent order item(s), but none looked like "
                    "Amazon Fresh groceries (they may be older, or non-food). Try again, "
                    "or build a Fresh cart and say 'learn my Fresh cart'."
                )

            # Merge into the persistent usual-order profile (same shape as learn_cart).
            try:
                data = json.loads(self.r.get(USUAL_ORDER_KEY) or "{}")
            except Exception:
                data = {}
            by_norm = {_norm_title(e.get("title", "")): e
                       for e in (data.get("items", []) or [])}
            now = datetime.now(timezone.utc).isoformat()
            added = updated = 0
            for item in grocery:
                key = _norm_title(item["title"])
                if key in by_norm:
                    entry = by_norm[key]
                    entry["count"] = int(entry.get("count", 1)) + 1
                    entry["last_seen"] = now
                    entry.setdefault("store", "amazon")
                    updated += 1
                else:
                    by_norm[key] = {
                        "title": item["title"], "name": item["name"],
                        "asin": "", "store": "amazon", "qty": 1, "count": 1,
                        "first_seen": now, "last_seen": now,
                    }
                    added += 1
            items = sorted(by_norm.values(), key=lambda e: -int(e.get("count", 1)))[:120]
            self.r.set(USUAL_ORDER_KEY, json.dumps({"items": items, "updated": now}))

        names = ", ".join(i["name"] for i in items[:12] if i.get("name"))
        report = (
            f"Learned from your Amazon order history: {added} new grocery item(s), "
            f"{updated} already known — {len(items)} staples on file now. "
            f"Top items: {names}. Future weekly lists will be built around these."
        )
        _log(f"  ✓ {report}")
        return report

    # ------------------------------------------------------------------
    # Full weekly run
    # ------------------------------------------------------------------

    async def _run_full(self) -> str:
        # ── 0. Load profile + targets ──────────────────────────────────
        profile = _load_user_profile(self.r)
        budget  = float(profile.get("weekly_budget_usd", 150))
        targets = _calculate_targets(profile)

        _log(f"=== Grocery Agent v5.0 | Budget: ${budget:.0f} | Goal: {profile['goal']} ===")
        _log(f"Targets: {targets['target_calories']} cal, {targets['protein_g']}g protein, "
             f"{targets['fiber_g']}g fiber")
        _log(f"Mac Bridge: {MAC_BRIDGE_URL}")

        order_results: list[OrderResult] = []
        total_spend   = 0.0

        async with aiohttp.ClientSession() as session:

            # ── 1. Health check ────────────────────────────────────────
            _log("Step 0: Checking Mac Bridge connectivity…")
            if not await _check_bridge(session):
                err = f"Mac Bridge unreachable at {MAC_BRIDGE_URL}. Cannot proceed."
                _log(f"  ✗ {err}")
                return err
            _log("  ✓ Mac Bridge reachable.")

            # ── 2. Amazon order history ────────────────────────────────
            past_purchases = await _fetch_amazon_order_history(session)

            # ── 3. Generate grocery list (biased toward the learned usual order) ─
            usual_order = _load_usual_order(self.r)
            if usual_order:
                _log(f"  Using learned usual order ({len(usual_order)} staples).")
            intake = _load_recent_nutrition(self.r)
            if intake:
                _log(f"  Sage: {intake['days_logged']} logged day(s), avg "
                     f"{intake['avg_protein_g']}g protein / {intake['avg_calories']} kcal.")
            meals, grocery_list, qty_map = await _generate_smart_list(
                past_purchases, profile, targets, usual_order, intake
            )
            _log(f"  {len(meals)} meals planned; grocery list ({len(grocery_list)} items): {grocery_list}")

            # ── 4. Resolve each item to a real product across all stores ─
            _log(f"Step 3: Resolving {len(grocery_list)} items × {len(STORE_KEYS)} stores "
                 f"(cache-first, LLM only on miss)…")
            price_map: dict[str, list[PriceResult]] = {}
            for i, item in enumerate(grocery_list, 1):
                _log(f"  [{i}/{len(grocery_list)}] {item}", kind="tool")
                price_map[item] = await _resolve_item(session, item, STORE_KEYS, self.r)

            priced = sum(
                1 for prices in price_map.values()
                if any(p.price is not None for p in prices)
            )
            _log(f"  ✓ {priced}/{len(grocery_list)} items matched to a product.")

            # ── 5. Select 1-2 best stores ──────────────────────────────
            _log("Step 4: Selecting stores…")
            selected_stores, item_to_store = _select_ordering_stores(price_map)
            selected_names = [STORES[s]["name"] for s in selected_stores]
            _log(f"  ✓ Selected: {', '.join(selected_names)}")

            # ── 6. Add items to carts (visible browser) ────────────────
            _log("Step 5: Adding items to carts…")
            for item in grocery_list:
                assigned = item_to_store.get(item, selected_stores[0])
                prices   = price_map[item]
                # the product resolved AT the assigned store — carries the href we add
                assigned_pr = next(
                    (p for p in prices if p.store == assigned and p.price is not None), None
                )

                if assigned_pr is None:
                    order_results.append(OrderResult(
                        item=item, assigned_store=assigned,
                        assigned_store_name=STORES[assigned]["name"],
                        best_price=None, status="not_found",
                        note="No matching product at the selected store.", all_prices=prices,
                    ))
                    continue

                # Check the FULL cost of this line, not just whether the budget
                # is already blown. A 3-unit line used to pass this guard on its
                # first unit and then spend three times what was checked.
                units_wanted = max(1, min(int(qty_map.get(item, 1) or 1), 6))
                line_cost = (assigned_pr.price or 0) * units_wanted
                if total_spend >= budget or (total_spend + line_cost) > budget:
                    order_results.append(OrderResult(
                        item=item, assigned_store=assigned,
                        assigned_store_name=STORES[assigned]["name"],
                        best_price=assigned_pr.price, status="budget_exceeded",
                        note=(f"Budget ${budget:.0f} reached "
                              f"({units_wanted}x @ ${assigned_pr.price:.2f} "
                              f"would take it to ${total_spend + line_cost:.2f})."),
                        all_prices=prices,
                        title=assigned_pr.title, href=assigned_pr.href,
                    ))
                    continue

                status, note = await _add_product_to_cart(
                    session, assigned_pr, units_wanted)
                if status == "added":
                    # Budget tracks what the cart actually costs, so a
                    # multi-unit line has to count all of its units.
                    total_spend += line_cost

                order_results.append(OrderResult(
                    item=item, assigned_store=assigned,
                    assigned_store_name=STORES[assigned]["name"],
                    best_price=assigned_pr.price, status=status,
                    note=note, all_prices=prices,
                    title=assigned_pr.title, href=assigned_pr.href,
                ))
                await asyncio.sleep(1)

            # ── 7. Verify carts (cart-truth) ───────────────────────────
            _log("Step 6: Verifying carts…")
            cart_truth: dict[str, dict] = {}
            for s in selected_stores:
                v = await _verify_cart(session, s)
                cart_truth[s] = v
                if v:
                    _log(f"  ✓ {STORES[s]['name']} cart: {v.get('count','?')} items, "
                         f"subtotal ${v.get('subtotal','?')}")
                else:
                    _log(f"  ⚠ {STORES[s]['name']} cart: could not read a subtotal")

            # ── 8. Save browser session state ──────────────────────────
            _log("Step 7: Saving browser session state…")
            save_res = await _bridge_post(session, "/browser/save-state", {}, timeout=15)
            if save_res.get("error"):
                _log(f"  ⚠ Save state failed: {save_res['error']}")
            else:
                _log(f"  ✓ Session saved.")

            # ── 9. Build report ────────────────────────────────────────
            _log("Step 8: Building report…")
            added = [r for r in order_results if r.status == "added"]

            total_savings = 0.0
            for r in order_results:
                valid_prices = [p.price for p in r.all_prices if p.price is not None]
                if len(valid_prices) >= 2 and r.best_price is not None:
                    total_savings += max(valid_prices) - r.best_price

            report = _build_report(
                profile, targets, selected_stores,
                order_results, total_spend, total_savings,
                cart_truth, meals,
            )

            # Persist the week's meal plan so Jarvis can answer "what can I make?"
            self.r.set("grocery:meal_plan", json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "meals": meals,
                "groceries": [r.title or r.item for r in order_results if r.status == "added"],
            }), ex=1209600)  # keep two weeks

            # ── 9. Store pending order in Redis ────────────────────────
            pending = {
                "status":     "pending_approval",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "stores":     selected_names,
                "store_keys": selected_stores,
                "cart_links": {s: STORES[s]["cart_url"] for s in selected_stores},
                "cart_truth": cart_truth,
                "meals":      meals,
                "items":      [
                    {
                        "item":      r.item,
                        "product":   r.title,        # the actual product matched/added
                        "store":     r.assigned_store_name,
                        "store_key": r.assigned_store,   # for pin_favorite_product
                        "price":     r.best_price,
                        "status":    r.status,
                        "url":     (STORES[r.assigned_store]["product_base"] + r.href)
                                   if r.href and not r.href.startswith("http") else r.href,
                    }
                    for r in order_results
                ],
                "total_usd":  round(total_spend, 2),
                "budget_usd": budget,
                "report":     report,
            }
            self.r.set(PENDING_ORDER_KEY, json.dumps(pending), ex=604800)
            _log("  ✓ Pending order stored in Redis.")

            # ── 10. Deliver report ─────────────────────────────────────
            _log("Step 8: Delivering report…")
            phone = profile.get("imessage_to", "")
            await _send_imessage(session, phone, report)
            _push_to_ios_app(report)

        summary = (
            f"Weekly grocery run complete, sir. "
            f"{len(added)} items added across {', '.join(selected_names)}. "
            f"Total: ${total_spend:.2f} of ${budget:.0f} budget. "
            f"Report sent via iMessage and Jarvis app. "
            f"Say 'approve grocery order' to proceed to checkout."
        )
        _log(f"=== Grocery Agent v5.0 complete. ===")
        return summary

    # ------------------------------------------------------------------
    # Checkout mode (called after user approval)
    # ------------------------------------------------------------------

    async def _run_checkout(self) -> str:
        _log("=== Grocery Agent: CHECKOUT MODE ===")

        raw = self.r.get(PENDING_ORDER_KEY)
        if not raw:
            return "No pending grocery order found, sir. Please run the grocery agent first."

        pending = json.loads(raw)
        if pending.get("status") != "pending_approval":
            status = pending.get("status", "unknown")
            return f"Order is already in status '{status}' — no action needed."

        store_keys  = pending.get("store_keys", [])
        store_names = pending.get("stores", [])

        if not store_keys:
            return "Pending order has no store information. Please re-run the grocery agent."

        checkout_results: list[str] = []
        async with aiohttp.ClientSession() as session:
            if not await _check_bridge(session):
                return f"Mac Bridge unreachable at {MAC_BRIDGE_URL}. Cannot place orders."

            for store_key in store_keys:
                success, msg = await _proceed_to_checkout(session, store_key)
                checkout_results.append(msg)
                await asyncio.sleep(2)

        # Update Redis status
        pending["status"]    = "placed"
        pending["placed_at"] = datetime.now(timezone.utc).isoformat()
        self.r.set(PENDING_ORDER_KEY, json.dumps(pending), ex=604800)

        stores_done = ", ".join(store_names)
        summary = (
            f"Orders placed at {stores_done}, sir. "
            f"Total: ${pending.get('total_usd', 0):.2f}. "
            f"Please complete any payment confirmation steps in the browser."
        )

        # Send confirmation
        profile = _load_user_profile(self.r)
        async with aiohttp.ClientSession() as session:
            await _send_imessage(session, profile.get("imessage_to", ""), summary)
        _push_to_ios_app(summary)

        _log("=== Checkout complete. ===")
        return summary
