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
from typing import Optional
import aiohttp
import redis as redis_lib

from base_agent import BaseAgent
from llm_helper import complete

MAC_BRIDGE_URL = os.environ.get("MAC_BRIDGE_URL", "http://host.docker.internal:7777")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")

# ---------------------------------------------------------------------------
# Store configurations
# ---------------------------------------------------------------------------
STORES = {
    "amazon": {
        "name": "Amazon Fresh",
        "search_url": "https://www.amazon.com/s?k={query}&i=amazonfresh",
        "cart_url":   "https://www.amazon.com/cart/view",
        "orderable":  True,
        "price_selectors": [
            ".a-price .a-offscreen",
            "[data-cy='price-recipe'] .a-offscreen",
            ".s-price-instructions-style .a-offscreen",
            ".a-color-price",
            ".a-price-whole",
        ],
    },
    "whole_foods": {
        "name": "Whole Foods",
        "search_url": "https://www.amazon.com/s?k={query}&i=wholefoods",
        "cart_url":   "https://www.amazon.com/alm/storefront?almBrandId=V29sZSBGb29kcw==",
        "orderable":  True,
        "price_selectors": [
            ".a-price .a-offscreen",
            "[data-cy='price-recipe'] .a-offscreen",
            ".a-color-price",
            ".a-price-whole",
        ],
    },
    "target": {
        "name": "Target",
        "search_url": "https://www.target.com/s?searchTerm={query}&category=5xt1a",
        "cart_url":   "https://www.target.com/cart",
        "orderable":  True,
        "price_selectors": [
            "[data-test='product-price']",
            "[data-test='current-price']",
            "span[class*='CurrentPrice']",
            "span[class*='Price--']",
            "[class*='ProductCardPrice']",
        ],
    },
    "heb": {
        "name": "HEB",
        "search_url": "https://www.heb.com/search/?q={query}",
        "cart_url":   "https://www.heb.com/cart",
        "orderable":  True,
        "price_selectors": [
            "[data-qe-id='product-price']",
            ".price-tag",
            "[class*='ProductPrice']",
            "[class*='product-price']",
            "p[class*='price']",
            "span[class*='price']",
        ],
    },
}

# Pending order Redis key
PENDING_ORDER_KEY = "grocery:pending_order"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[GroceryAgent] {msg}", flush=True)


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
    else:
        target_cal = tdee

    protein_g = float(profile["weight_lbs"]) * float(profile.get("protein_goal_g_per_lb", 1.0))
    fiber_g   = int(profile.get("fiber_goal_g", 35))

    return {
        "bmr":             round(bmr),
        "tdee":            round(tdee),
        "tdee_source":     tdee_source,
        "target_calories": round(target_cal),
        "deficit":         round(tdee - target_cal),
        "protein_g":       round(protein_g),
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


def _extract_price_from_page_text(page_text: str) -> Optional[float]:
    prices = re.findall(r"\$\s*(\d{1,3}(?:\.\d{2})?)", str(page_text)[:8000])
    valid = [float(p) for p in prices if 0.50 <= float(p) <= 80.0]
    if valid:
        valid.sort()
        return valid[len(valid) // 2]
    return None


# ---------------------------------------------------------------------------
# Headless Playwright scraper — price extraction (fallback)
# ---------------------------------------------------------------------------
_PRICE_JS_TEMPLATE = """
(function() {{
    var selectors = {selectors_json};
    for (var s of selectors) {{
        var els = Array.from(document.querySelectorAll(s))
            .map(function(el) {{ return el.innerText || el.textContent || ''; }})
            .filter(function(t) {{ return t && t.includes('$'); }});
        if (els.length > 0) return els[0].trim();
    }}
    return null;
}})();
"""


async def _scrape_price_headless(
    session: aiohttp.ClientSession, item: str, store_key: str
) -> Optional[float]:
    store = STORES[store_key]
    url   = store["search_url"].format(query=item.replace(" ", "+"))
    _log(f"  [headless] {store['name']} → {item}")

    nav = await _bridge_post(
        session,
        "/scraper/navigate",
        {"url": url, "wait_until": "networkidle", "timeout_ms": 30000},
        timeout=45,
    )
    if "error" in nav or nav.get("status") != "ok":
        return None

    selectors_json = json.dumps(store["price_selectors"])
    js_res = await _bridge_post(
        session, "/scraper/js",
        {"script": _PRICE_JS_TEMPLATE.format(selectors_json=selectors_json)},
        timeout=15,
    )
    raw = js_res.get("result")
    if raw:
        price = _extract_price(str(raw))
        if price:
            return price

    page_res = await _bridge_get(session, "/scraper/read", timeout=20)
    return _extract_price_from_page_text(page_res.get("text") or "")


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


@dataclass
class OrderResult:
    item:               str
    assigned_store:     str
    assigned_store_name:str
    best_price:         Optional[float]
    status:             str  # "added" | "not_found" | "error" | "budget_exceeded"
    note:               str = ""
    all_prices:         list[PriceResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-store price fetching (all via headless Playwright scraper)
# ---------------------------------------------------------------------------
async def _get_price(session: aiohttp.ClientSession, item: str, store_key: str) -> PriceResult:
    result = PriceResult(store=store_key, store_name=STORES[store_key]["name"], item=item)
    price  = await _scrape_price_headless(session, item, store_key)
    if price:
        result.price = price
        result.unit  = f"${price:.2f}"
        _log(f"  ✓ {result.store_name}: ${price:.2f}")
    else:
        _log(f"  ✗ {result.store_name}: no price found for '{item}'")
    return result


async def _compare_prices(session: aiohttp.ClientSession, item: str) -> list[PriceResult]:
    results: list[PriceResult] = []
    for store_key in ("amazon", "whole_foods", "target", "heb"):
        r = await _get_price(session, item, store_key)
        results.append(r)
        await asyncio.sleep(0.3)
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
# LLM grocery list generation (fitness-aware)
# ---------------------------------------------------------------------------
async def _generate_smart_list(
    past_purchases: list[str], profile: dict, targets: dict
) -> list[str]:
    _log("Step 2: Generating fitness-aware grocery list via LLM…")

    prefs  = profile.get("dietary_preferences", ["high protein"])
    budget = float(profile.get("weekly_budget_usd", 150))
    goal   = profile.get("goal", "cutting")

    system_prompt = (
        f"You are JARVIS, a British AI assistant optimising a user's nutrition for {goal}.\n"
        f"User stats: {profile['weight_lbs']} lbs, {profile['height_in']} in, "
        f"age {profile.get('age', 25)}, {profile.get('activity_level', 'moderately_active')}.\n"
        f"Daily targets: {targets['target_calories']} cal "
        f"(TDEE {targets['tdee']} - {targets['deficit']} deficit), "
        f"{targets['protein_g']}g protein, {targets['fiber_g']}g fiber.\n"
        f"Dietary preferences: {', '.join(prefs)}.\n"
        f"Weekly grocery budget: ${budget:.0f}.\n\n"
        "Generate a smart weekly grocery list of 12-16 items. Prioritise:\n"
        "1. High protein-per-dollar (chicken, eggs, tuna, cottage cheese, Greek yogurt)\n"
        "2. High-fiber vegetables and fruits (broccoli, spinach, berries, sweet potato)\n"
        "3. Variety across protein, vegetable, carb, and healthy-fat categories\n"
        "4. Items the user has bought before (familiarity)\n"
        "5. Staying within the weekly budget\n\n"
        "Return ONLY a valid JSON array of item name strings. No explanation, no markdown.\n"
        'Example: ["chicken breast boneless skinless", "large eggs", "non-fat greek yogurt", ...]'
    )

    user_msg = (
        f"User's recent Amazon purchases:\n{json.dumps(past_purchases[:20], indent=2)}\n\n"
        "Generate the weekly grocery list now."
    )

    response = await complete(system=system_prompt, user=user_msg, max_tokens=500)

    try:
        match = re.search(r"\[.*\]", response or "", re.DOTALL)
        if match:
            items = json.loads(match.group())
            if isinstance(items, list):
                _log(f"  ✓ Generated list of {len(items)} items.")
                return [str(i) for i in items[:16]]
    except (json.JSONDecodeError, AttributeError):
        pass

    _log("  ⚠ LLM list parse failed — using fallback staples.")
    return [
        "chicken breast boneless skinless", "large eggs", "non-fat greek yogurt",
        "low-fat cottage cheese", "93% lean ground beef", "tuna canned in water",
        "broccoli", "spinach", "sweet potato", "brown rice", "oatmeal rolled oats",
        "almonds", "blueberries", "olive oil",
    ]


# ---------------------------------------------------------------------------
# Cart automation helpers (visible browser)
# ---------------------------------------------------------------------------
async def _add_to_amazon_cart(
    session: aiohttp.ClientSession, item: str, store_key: str
) -> tuple[str, str]:
    store_name = STORES[store_key]["name"]
    search_url = STORES[store_key]["search_url"].format(query=item.replace(" ", "+"))
    _log(f"  [cart] {store_name}: {item}")

    nav = await _bridge_post(session, "/browser/navigate", {"url": search_url}, timeout=40)
    if "error" in nav:
        return "error", f"Navigation failed: {nav['error']}"
    await asyncio.sleep(4)

    click_js = """
    (function() {
        var links = Array.from(document.querySelectorAll(
            'h2 a.a-link-normal, .s-product-image-container a, [data-cy="title-recipe"] a'
        )).filter(function(a) { return a.href && a.href.includes('/dp/'); });
        if (!links.length) return 'no_products_found';
        links[0].click();
        return 'clicked_product';
    })();
    """
    res = await _bridge_post(session, "/browser/js", {"script": click_js}, timeout=15)
    if "no_products" in str(res.get("result", "")).lower():
        return "not_found", "No product results found."
    await asyncio.sleep(4)

    add_js = """
    (function() {
        var selectors = [
            '#add-to-cart-button',
            'input[name="submit.add-to-cart"]',
            'button[name="submit.add-to-cart"]',
        ];
        for (var s of selectors) {
            var btn = document.querySelector(s);
            if (btn) { btn.click(); return 'clicked:' + s; }
        }
        var btns = Array.from(document.querySelectorAll('button, input[type="submit"]'))
            .filter(function(el) { return /add to cart/i.test(el.innerText || el.value || ''); });
        if (btns.length) { btns[0].click(); return 'clicked:text_match'; }
        return 'no_add_button_found';
    })();
    """
    res = await _bridge_post(session, "/browser/js", {"script": add_js}, timeout=15)
    val = str(res.get("result", "")).lower()
    if "clicked" in val:
        return "added", f"Added to {store_name}."
    return "not_found", f"No Add-to-Cart button found ({val})."


async def _add_to_target_cart(session: aiohttp.ClientSession, item: str) -> tuple[str, str]:
    url = STORES["target"]["search_url"].format(query=item.replace(" ", "+"))
    _log(f"  [cart] Target: {item}")

    nav = await _bridge_post(session, "/browser/navigate", {"url": url}, timeout=40)
    if "error" in nav:
        return "error", f"Navigation failed: {nav['error']}"
    await asyncio.sleep(5)

    click_js = """
    (function() {
        var links = Array.from(document.querySelectorAll(
            'a[data-test="product-title"], [data-test="product-details"] a, a[href*="/p/"]'
        )).filter(function(a) { return a.href; });
        if (!links.length) return 'no_products';
        links[0].click();
        return 'clicked_product';
    })();
    """
    res = await _bridge_post(session, "/browser/js", {"script": click_js}, timeout=15)
    if "no_products" in str(res.get("result", "")).lower():
        return "not_found", "No product results found on Target."
    await asyncio.sleep(4)

    add_js = """
    (function() {
        var selectors = [
            '[data-test="shippingButton"]',
            '[data-test="orderPickupButton"]',
            '[data-test="addToCartButton"]',
            'button[id*="addToCart"]',
        ];
        for (var s of selectors) {
            var btn = document.querySelector(s);
            if (btn) { btn.click(); return 'clicked:' + s; }
        }
        var btns = Array.from(document.querySelectorAll('button'))
            .filter(function(b) { return /add to cart/i.test(b.innerText); });
        if (btns.length) { btns[0].click(); return 'clicked:text_match'; }
        return 'no_add_button';
    })();
    """
    res = await _bridge_post(session, "/browser/js", {"script": add_js}, timeout=15)
    val = str(res.get("result", "")).lower()
    if "clicked" in val:
        return "added", "Added to Target cart."
    return "not_found", f"No Add-to-Cart button found on Target ({val})."


async def _add_to_heb_cart(session: aiohttp.ClientSession, item: str) -> tuple[str, str]:
    url = STORES["heb"]["search_url"].format(query=item.replace(" ", "+"))
    _log(f"  [cart] HEB: {item}")

    nav = await _bridge_post(session, "/browser/navigate", {"url": url}, timeout=40)
    if "error" in nav:
        return "error", f"Navigation failed: {nav['error']}"
    await asyncio.sleep(5)

    add_js = """
    (function() {
        var btns = Array.from(document.querySelectorAll('button'))
            .filter(function(b) {
                return /add to cart|add item/i.test(b.innerText || b.getAttribute('aria-label') || '');
            });
        if (!btns.length) return 'no_add_button';
        btns[0].click();
        return 'clicked_add_to_cart';
    })();
    """
    res = await _bridge_post(session, "/browser/js", {"script": add_js}, timeout=15)
    val = str(res.get("result", "")).lower()
    if "clicked" in val:
        return "added", "Added to HEB cart."
    return "not_found", f"No Add-to-Cart button found on HEB ({val})."


async def _add_to_store_cart(
    session: aiohttp.ClientSession, item: str, store_key: str
) -> tuple[str, str]:
    if store_key in ("amazon", "whole_foods"):
        return await _add_to_amazon_cart(session, item, store_key)
    elif store_key == "target":
        return await _add_to_target_cart(session, item)
    elif store_key == "heb":
        return await _add_to_heb_cart(session, item)
    return "error", f"No cart handler for store: {store_key}"


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
) -> str:
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
        store_lines[key].append(f"  ✓ {r.item:<35} {price_str:>7}  {r.assigned_store_name}")

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
        action = (self.params or {}).get("action", "")

        if action == "checkout":
            return await self._run_checkout()
        return await self._run_full()

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

            # ── 3. Generate grocery list ───────────────────────────────
            grocery_list = await _generate_smart_list(past_purchases, profile, targets)
            _log(f"  Grocery list ({len(grocery_list)} items): {grocery_list}")

            # ── 4. Price comparison ────────────────────────────────────
            _log(f"Step 3: Price comparison for {len(grocery_list)} items × 4 stores…")
            price_map: dict[str, list[PriceResult]] = {}
            for i, item in enumerate(grocery_list, 1):
                _log(f"  [{i}/{len(grocery_list)}] {item}")
                price_map[item] = await _compare_prices(session, item)
                await asyncio.sleep(0.3)

            priced = sum(
                1 for prices in price_map.values()
                if any(p.price is not None for p in prices)
            )
            _log(f"  ✓ {priced}/{len(grocery_list)} items priced.")

            # Close headless scraper
            await _bridge_post(session, "/scraper/close", {}, timeout=10)

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
                valid    = [p for p in prices if p.price is not None]
                best_pr  = min(valid, key=lambda p: p.price) if valid else PriceResult(assigned, STORES[assigned]["name"], item)

                if total_spend >= budget:
                    order_results.append(OrderResult(
                        item=item, assigned_store=assigned,
                        assigned_store_name=STORES[assigned]["name"],
                        best_price=best_pr.price, status="budget_exceeded",
                        note=f"Budget ${budget:.0f} reached.", all_prices=prices,
                    ))
                    continue

                status, note = await _add_to_store_cart(session, item, assigned)
                if best_pr.price and status == "added":
                    total_spend += best_pr.price

                order_results.append(OrderResult(
                    item=item, assigned_store=assigned,
                    assigned_store_name=STORES[assigned]["name"],
                    best_price=best_pr.price, status=status,
                    note=note, all_prices=prices,
                ))
                await asyncio.sleep(1)

            # ── 7. Save browser session state ──────────────────────────
            _log("Step 6: Saving browser session state…")
            save_res = await _bridge_post(session, "/browser/save-state", {}, timeout=15)
            if save_res.get("error"):
                _log(f"  ⚠ Save state failed: {save_res['error']}")
            else:
                _log(f"  ✓ Session saved.")

            # ── 8. Build report ────────────────────────────────────────
            _log("Step 7: Building report…")
            added = [r for r in order_results if r.status == "added"]

            total_savings = 0.0
            for r in order_results:
                valid_prices = [p.price for p in r.all_prices if p.price is not None]
                if len(valid_prices) >= 2 and r.best_price is not None:
                    total_savings += max(valid_prices) - r.best_price

            report = _build_report(
                profile, targets, selected_stores,
                order_results, total_spend, total_savings,
            )

            # ── 9. Store pending order in Redis ────────────────────────
            pending = {
                "status":     "pending_approval",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "stores":     selected_names,
                "store_keys": selected_stores,
                "cart_links": {s: STORES[s]["cart_url"] for s in selected_stores},
                "items":      [
                    {
                        "item":   r.item,
                        "store":  r.assigned_store_name,
                        "price":  r.best_price,
                        "status": r.status,
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
