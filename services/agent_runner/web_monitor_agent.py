"""Web monitor agent — "Scout".

Two watch modes, both configured under params in config/agents.yml:

1. ``queries``    — Tavily search queries; reports newly published results
                    (3-day dedup via Redis, unchanged from v1).
2. ``watch_urls`` — specific pages watched for NEW ITEMS matching a per-watch
                    goal. GENERIC: each watch can set ``goal`` (what to
                    extract — apartment units, product restocks, job postings,
                    concert tickets, price changes…), ``noun`` (word used in
                    reports), ``alert_title``, and ``priority_keywords``.
                    Defaults reproduce the original apartment behavior. Each
                    run the page is rendered via the Mac Bridge scraper (falls
                    back to a plain GET), the text is hashed, and ONLY when it
                    changed is an LLM asked to extract the current item list and
                    diff it against the last snapshot in Redis. New items
                    trigger an iOS push + iMessage, priority items first.

                    Watches with ``type: sightmap`` (or a sightmap.com/app/api
                    URL) skip scraping + LLM entirely: SightMap embeds expose a
                    public JSON API with exact unit numbers, floors, prices and
                    availability dates, so those are diffed mechanically.

                    Bot-blocked responses (Cloudflare "verifying", Akamai
                    "Access Denied", …) are retried once and never written as
                    a baseline — otherwise the first successful fetch would
                    flag every existing unit as "new".

Token cost is near-zero when nothing changes: the hash gate means no LLM call
on an unchanged page.

Redis keys:
    agent:web_monitor:seen:{hash}            search-result dedup (3-day TTL)
    agent:web_monitor:watch:{id}:hash        last page-text hash
    agent:web_monitor:watch:{id}:units       last extracted unit list (JSON)
    agent:web_monitor:watch:{id}:notified:*  per-unit re-notify guard (7-day TTL)
"""

import asyncio
import hashlib
import json
import os
import re

import aiohttp

from base_agent import BaseAgent
from llm_helper import complete

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
MAC_BRIDGE_URL = os.environ.get("MAC_BRIDGE_URL", "http://host.docker.internal:7777")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
# Firecrawl (optional) — a managed scrape+structured-extract API for URL watches.
# When FIRECRAWL_API_KEY is set, URL watches scrape + extract via Firecrawl's
# schema extraction (structured data back, no fragile LLM-JSON parsing, better
# anti-bot/JS handling). Unset → falls back to the local scraper + LLM path.
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_URL = os.environ.get("FIRECRAWL_URL", "https://api.firecrawl.dev").rstrip("/")


def _repair_json(s: str):
    """Best-effort JSON parse that survives truncation (the main cause of Scout's
    'Expecting delimiter' errors when a big page overflows max_tokens): parse as-is,
    else trim to the last complete object and balance open brackets."""
    try:
        return json.loads(s)
    except Exception:
        pass
    cut = s.rfind("}")
    if cut < 0:
        return None
    frag = s[: cut + 1]
    # Balance any brackets left open by the truncation (ignoring those in strings).
    close = {"{": "}", "[": "]"}
    stack, in_str, esc = [], False, False
    for ch in frag:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(close[ch])
        elif ch in "}]" and stack:
            stack.pop()
    frag += "".join(reversed(stack))
    try:
        return json.loads(frag)
    except Exception:
        return None

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_SYSTEM_PROMPT = (
    "You are JARVIS. Summarise these newly detected web results for your user. "
    "Group by topic, highlight what is most interesting or actionable, and keep "
    "it under 300 words. Use bullet points."
)

# Default goal keeps the existing apartment watches working unchanged; any
# watch can override with its own `goal` for a totally different use case
# (product restocks, job postings, ticket drops, price changes, etc.).
_DEFAULT_GOAL = (
    "available apartment rental units or per-floor-plan availability. Use the "
    "unit number as id when shown; infer floor from the unit number when a "
    "garden/low-rise property (e.g. unit 1104 → floor 1)"
)

_EXTRACT_SYSTEM = (
    "You are Scout, JARVIS's web monitor. You extract a list of items matching "
    "the user's watch goal from rendered page text and diff it against the "
    "previous snapshot. Reply with STRICT JSON only — no prose, no markdown."
)

_EXTRACT_USER = """Watch: {name}
URL: {url}
WHAT TO WATCH FOR (extract these items): {goal}
Priority keywords (flag matching items): {keywords}

Previously seen items (JSON): {previous}

Current page text:
<<<
{text}
>>>

Return JSON exactly in this shape:
{{"items": [{{"id": "...", "label": "...", "detail": "", "price": "", "priority": false, "why_priority": ""}}],
  "new_item_ids": [], "note": ""}}

Rules:
- "items": EVERY item on the page matching the watch goal above.
- "id": a STABLE identifier for the item (unit/product/listing number, or a
  distinctive name) — used to detect what's genuinely new across runs.
- "label": short human name; "detail": any useful extras (floor, size, dates);
  "price": price/rate if shown.
- priority=true when the item matches ANY priority keyword; explain in
  "why_priority".
- "new_item_ids": ids from "items" NOT present in the previous snapshot (match
  loosely). Empty list if none.
- If the page shows no matching items (blocked, error page, empty), return
  empty "items" and explain in "note"."""

# Text that means the fetch was bot-blocked, not a real empty page.
_BLOCK_RE = re.compile(
    r"verifying you are human|just a moment|access denied"
    r"|enable javascript and cookies|checking your browser"
    r"|security verification|reference #1[08]\.",
    re.IGNORECASE,
)


class WebMonitorAgent(BaseAgent):
    """Monitors search queries and specific URLs for notable new content."""

    async def run(self) -> str:
        queries: list[str] = self.params.get("queries", [])
        watches: list[dict] = list(self.params.get("watch_urls", []))
        # DYNAMIC watches added at runtime via the manage_watches tool (stored in
        # Redis scout:watches) are merged with the static agents.yml ones, so you
        # can say "watch this URL for X" without editing config or restarting.
        try:
            dynamic = json.loads(self.r.get("scout:watches") or "[]")
            if isinstance(dynamic, list):
                watches += [w for w in dynamic if isinstance(w, dict) and w.get("url")]
        except Exception:
            pass
        if not queries and not watches:
            return (
                "Web monitor agent: nothing configured. Add a watch via "
                "\"watch this URL for …\" (manage_watches) or params in config/agents.yml."
            )

        sections: list[str] = []
        async with aiohttp.ClientSession() as session:
            if watches:
                sections.append(await self._run_url_watches(session, watches))
            if queries:
                sections.append(await self._run_query_watches(session, queries))

        return "\n\n".join(s for s in sections if s)

    # ------------------------------------------------------------------
    # URL watches — new-unit detection on specific pages
    # ------------------------------------------------------------------

    async def _run_url_watches(self, session, watches: list[dict]) -> str:
        default_keywords = self.params.get(
            "priority_keywords",
            ["1st floor", "first floor", "ground floor", "patio", "yard"],
        )
        lines: list[str] = []
        alerts: list[str] = []          # new-unit alert lines (priority first)
        priority_alerts: list[str] = []

        def record(line: str) -> None:
            """Report line + unified observability event in one place."""
            lines.append(line)
            self.log_event("finding", line.lstrip("• "))

        for watch in watches:
            name = watch.get("name", watch.get("url", "watch"))
            url = watch.get("url", "")
            if not url:
                continue
            self.log_event("tool", f"Checking watch '{name}': {url}")
            keywords = watch.get("priority_keywords", default_keywords)
            goal = watch.get("goal", _DEFAULT_GOAL)   # what to extract (per-watch)
            noun = watch.get("noun", "item")          # word used in reports/alerts
            wid = hashlib.md5(url.encode()).hexdigest()[:12]
            hash_key = f"agent:web_monitor:watch:{wid}:hash"
            units_key = f"agent:web_monitor:watch:{wid}:units"
            prev_units_raw = self.r.get(units_key)

            if watch.get("type") == "sightmap" or "sightmap.com/app/api" in url:
                # Structured JSON API — no scraping, no LLM.
                units = await self._fetch_sightmap_units(session, url, keywords)
                if units is None:
                    record(f"• {name}: SightMap API fetch failed — will retry next run")
                    continue
                note = ""
                page_hash = hashlib.md5(
                    json.dumps(units, sort_keys=True).encode()
                ).hexdigest()
                if self.r.get(hash_key) == page_hash and prev_units_raw is not None:
                    record(f"• {name}: unchanged ({len(units)} {noun}(s) available)")
                    continue
                prev_ids = {
                    str(u.get("id", "")).strip().lower()
                    for u in json.loads(prev_units_raw or "[]")
                }
                new_ids = {
                    str(u.get("id", "")).strip().lower()
                    for u in units
                    if str(u.get("id", "")).strip().lower() not in prev_ids
                }
            else:
                # Prefer Firecrawl (managed scrape + schema extraction) when
                # configured; fall back to the local scraper + LLM path otherwise.
                fc = await self._firecrawl_extract(session, url, goal, prev_units_raw)
                if fc is not None:
                    units = fc["units"]
                    note = fc.get("note", "")
                    page_hash = hashlib.md5(
                        json.dumps(units, sort_keys=True).encode()).hexdigest()
                    if self.r.get(hash_key) == page_hash and prev_units_raw is not None:
                        record(f"• {name}: unchanged ({len(units)} {noun}(s) available)")
                        continue
                    if not units and note:
                        record(f"• {name}: no availability data [{note}] — will retry next run")
                        continue
                    for u in units:
                        blob = json.dumps(u).lower()
                        if any(kw.lower() in blob for kw in keywords) or u.get("floor") in (1, "1"):
                            u["priority"] = True
                    new_ids = {str(i).strip().lower() for i in fc.get("new_unit_ids", [])}
                else:
                    text = await self._fetch_page(session, url)
                    if not text or len(text) < 200 or _BLOCK_RE.search(text):
                        record(f"• {name}: page fetch failed or bot-blocked — will retry next run")
                        continue

                    normalized = " ".join(text.split())
                    page_hash = hashlib.md5(normalized.encode()).hexdigest()
                    if self.r.get(hash_key) == page_hash and prev_units_raw is not None:
                        count = len(json.loads(prev_units_raw))
                        record(f"• {name}: unchanged ({count} {noun}(s) available)")
                        continue

                    extracted = await self._extract_units(
                        name, url, goal, keywords, prev_units_raw, normalized[:11000]
                    )
                    if extracted is None:
                        record(f"• {name}: page changed but extraction failed — will retry next run")
                        continue

                    units = extracted.get("units", [])
                    note = extracted.get("note", "")
                    if not units and note:
                        # Rendered fine but no availability data (blocked widget,
                        # error page) — do NOT store this as a baseline.
                        record(f"• {name}: no availability data [{note}] — will retry next run")
                        continue
                    # Belt-and-braces priority check on top of the LLM's flag
                    for u in units:
                        blob = json.dumps(u).lower()
                        if any(kw.lower() in blob for kw in keywords) or u.get("floor") in (1, "1"):
                            u["priority"] = True
                    new_ids = {str(i).strip().lower() for i in extracted.get("new_unit_ids", [])}

            self.r.set(units_key, json.dumps(units))
            self.r.set(hash_key, page_hash)

            if prev_units_raw is None:
                pri = sum(1 for u in units if u.get("priority"))
                record(
                    f"• {name}: baseline recorded — {len(units)} {noun}(s) available"
                    + (f", {pri} priority (1st floor/patio/yard)" if pri else "")
                    + (f" [{note}]" if note else "")
                )
                continue
            new_units = [
                u for u in units
                if str(u.get("id", "")).strip().lower() in new_ids
            ]
            # Re-notify guard: don't alert the same unit twice within a week
            # even if the page flaps (unit disappears and reappears).
            fresh = []
            for u in new_units:
                uid = hashlib.md5(f"{wid}:{str(u.get('id','')).lower()}".encode()).hexdigest()
                if self.r.set(f"agent:web_monitor:watch:{wid}:notified:{uid}",
                              "1", nx=True, ex=7 * 24 * 3600):
                    fresh.append(u)

            if not fresh:
                record(f"• {name}: page changed, no new {noun}s ({len(units)} available)"
                             + (f" [{note}]" if note else ""))
                continue

            record(f"• {name}: 🚨 {len(fresh)} NEW {noun}(s)!")
            for u in fresh:
                desc = self._unit_line(name, u)
                if u.get("priority"):
                    priority_alerts.append("⭐ PRIORITY " + desc)
                else:
                    alerts.append(desc)

        all_alerts = priority_alerts + alerts
        if all_alerts and self.params.get("notify", True):
            title = self.params.get("alert_title", "🔔 Scout — new matches")
            await self._notify(title + ":\n" + "\n".join(all_alerts))

        report = "Scout — URL watches\n\n" + "\n".join(lines)
        if all_alerts:
            report += "\n\nNew matches:\n" + "\n".join(all_alerts)
        return report

    @staticmethod
    def _unit_line(watch_name: str, u: dict) -> str:
        bits = [str(u.get("id", "?"))]
        if u.get("plan"):
            bits.append(str(u["plan"]))
        if u.get("floor") is not None:
            bits.append(f"floor {u['floor']}")
        if u.get("price"):
            bits.append(str(u["price"]))
        if u.get("available"):
            bits.append(f"avail {u['available']}")
        if u.get("why_priority"):
            bits.append(str(u["why_priority"]))
        return f"[{watch_name}] " + " — ".join(bits)

    async def _fetch_sightmap_units(self, session, url: str,
                                    keywords: list[str]) -> list[dict] | None:
        """Fetch a SightMap embed API payload and normalise its unit list."""
        try:
            async with session.get(
                url,
                headers={"User-Agent": _UA, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:
            print(f"[Scout] sightmap fetch failed for {url}: {exc}")
            return None

        d = data.get("data", data)
        floors = {
            f.get("id"): str(f.get("filter_short_label")
                             or f.get("filter_label") or "").replace("Floor", "").strip()
            for f in d.get("floors", [])
        }
        plans = {}
        for p in d.get("floor_plans", []):
            pname = p.get("name")
            if isinstance(pname, str) and pname.startswith("{"):
                try:
                    pname = json.loads(pname).get("name", pname)
                except (ValueError, AttributeError):
                    pass
            elif isinstance(pname, dict):
                pname = pname.get("name", "")
            plans[p.get("id")] = pname or ""

        units = []
        for u in d.get("units", []):
            floor_label = floors.get(u.get("floor_id"), "")
            try:
                floor = int(floor_label)
            except (ValueError, TypeError):
                floor = None
            unit = {
                "id": u.get("display_unit_number") or u.get("unit_number") or u.get("id"),
                "plan": plans.get(u.get("floor_plan_id"), ""),
                "floor": floor,
                "price": u.get("display_price") or u.get("price"),
                "available": u.get("display_available_on") or u.get("available_on"),
                "priority": False,
                "why_priority": "",
            }
            # Ground (0) and 1st floor both count as best-yard-odds units.
            if floor is not None and floor <= 1:
                unit["priority"] = True
                unit["why_priority"] = f"floor {floor} (ground/1st — patio/yard potential)"
            elif any(kw.lower() in json.dumps(u).lower() for kw in keywords):
                unit["priority"] = True
                unit["why_priority"] = "matches priority keyword"
            units.append(unit)
        return units

    async def _fetch_page(self, session, url: str) -> str:
        """Rendered page text, trying progressively heavier fetchers:

        1. Mac Bridge headless scraper (stealth context).
        2. Mac Bridge VISIBLE browser — Cloudflare parks the headless
           scraper on its challenge page indefinitely, but the headed
           browser (real fingerprint + persisted cookies) passes; this is
           the same browser the grocery/ClassPass agents already drive.
        3. Plain GET + tag-strip (last resort; listing sites usually 403 it).
        """
        text = await self._bridge_read(session, "/scraper/navigate", "/scraper/read", url)
        if text and not _BLOCK_RE.search(text) and len(text) > 200:
            return text

        text = await self._bridge_read(session, "/browser/navigate", "/browser/read", url)
        if text:
            return text

        # Fallback: direct fetch (many listing sites 403 non-browser clients,
        # in which case this raises/returns empty and the run reports it).
        try:
            async with session.get(
                url,
                headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status != 200:
                    return ""
                html = await resp.text()
                html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
                return re.sub(r"(?s)<[^>]+>", " ", html)
        except Exception as exc:
            print(f"[Scout] direct fetch failed for {url}: {exc}")
            return ""

    async def _bridge_read(self, session, nav_path: str, read_path: str, url: str) -> str:
        """Navigate a mac_bridge browser to url, wait, and return its text.

        Used for both the headless scraper (/scraper/*) and the warmed VISIBLE
        browser (/browser/*) — the latter carries the user's Cloudflare
        clearance cookies, so it passes challenges the scraper can't.
        Returns "" on any failure (caller falls through to the next fetcher).
        """
        try:
            async with session.post(
                f"{MAC_BRIDGE_URL}{nav_path}",
                json={"url": url, "wait_until": "domcontentloaded", "timeout_ms": 25000},
                timeout=aiohttp.ClientTimeout(total=40),
            ) as resp:
                if resp.status != 200:
                    return ""
                body = await resp.json()
                if isinstance(body, dict) and body.get("error"):
                    return ""
        except Exception as exc:
            print(f"[Scout] {nav_path} failed for {url}: {exc}")
            return ""

        await asyncio.sleep(5)  # let the SPA / challenge settle before reading

        try:
            async with session.get(
                f"{MAC_BRIDGE_URL}{read_path}",
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                return str(data.get("text") or "")
        except Exception as exc:
            print(f"[Scout] {read_path} failed for {url}: {exc}")
            return ""

    async def _firecrawl_extract(self, session, url, goal, prev_raw) -> dict | None:
        """Scrape + STRUCTURED-extract a watch URL via Firecrawl (schema extraction).

        Returns {units, new_unit_ids, note} or None. None means "not configured or
        failed" → the caller falls back to the local scraper + LLM path. Because
        Firecrawl returns typed data against a schema, there's no fragile JSON to
        parse (fixes the 'Expecting delimiter' errors), and its managed renderer
        clears JS/anti-bot pages the local scraper gets parked on.
        """
        if not FIRECRAWL_API_KEY:
            return None
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string",
                                   "description": "unit number / listing id / SKU"},
                            "plan": {"type": "string", "description": "plan/label/name"},
                            "floor": {"type": "string"},
                            "price": {"type": "string"},
                            "available": {"type": "string",
                                          "description": "availability date/status"},
                        },
                        "required": ["id"],
                    },
                },
                "note": {"type": "string",
                         "description": "short note if no availability / error page"},
            },
            "required": ["items"],
        }
        prompt = (
            f"Extract EVERY currently available {goal} listed on this page. For each, "
            "capture id (unit number / listing id), plan/label, floor, price, and "
            "availability date. If the page shows no availability, or is an error / "
            "bot-challenge page, return items: [] with a short note explaining."
        )
        body = {
            "url": url,
            "formats": ["json"],
            "onlyMainContent": True,
            "jsonOptions": {"schema": schema, "prompt": prompt},
        }
        try:
            async with session.post(
                f"{FIRECRAWL_URL}/v1/scrape",
                headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                         "Content-Type": "application/json"},
                json=body,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    txt = (await resp.text())[:160]
                    print(f"[Scout] firecrawl {resp.status} for {url}: {txt}")
                    return None
                payload = await resp.json()
        except Exception as exc:
            print(f"[Scout] firecrawl error for {url}: {exc}")
            return None

        if not payload.get("success", True):
            return None
        j = ((payload.get("data") or {}).get("json")) or {}
        items = j.get("items")
        if not isinstance(items, list):
            return None
        for it in items:
            if isinstance(it, dict):
                it.setdefault("plan", it.get("label", ""))
                it.setdefault("available", it.get("detail", ""))
        # Diff against the previous snapshot ourselves (reliable — same as the
        # SightMap path — rather than trusting a model to compute the delta).
        try:
            prev_ids = {str(u.get("id", "")).strip().lower()
                        for u in json.loads(prev_raw or "[]")}
        except Exception:
            prev_ids = set()
        new_ids = [str(it.get("id", "")).strip() for it in items
                   if str(it.get("id", "")).strip().lower() not in prev_ids]
        return {"units": items, "new_unit_ids": new_ids, "note": j.get("note", "")}

    async def _extract_units(self, name, url, goal, keywords, prev_raw, text) -> dict | None:
        previous = prev_raw if prev_raw else "NONE (first scan)"
        try:
            reply = await complete(
                system=_EXTRACT_SYSTEM,
                user=_EXTRACT_USER.format(
                    name=name, url=url, goal=goal, keywords=", ".join(keywords),
                    previous=previous, text=text,
                ),
                # Raised from 1400: a page with many units overflowed and produced
                # truncated/invalid JSON ("Expecting ',' delimiter"). More headroom
                # + _repair_json (truncation-tolerant) fixes that class of failure.
                max_tokens=4000,
                temperature=0.0,
            )
            start, end = reply.find("{"), reply.rfind("}") + 1
            if start < 0:
                return None
            data = _repair_json(reply[start:end])
            if not isinstance(data, dict):
                return None
            # Normalize the generic item shape → the internal keys the rest of
            # the agent consumes (keeps sightmap + LLM paths on one shape).
            items = data.get("items", data.get("units", []))
            for it in items:
                it.setdefault("plan", it.get("label", ""))
                it.setdefault("available", it.get("detail", ""))
            return {"units": items,
                    "new_unit_ids": data.get("new_item_ids",
                                             data.get("new_unit_ids", [])),
                    "note": data.get("note", "")}
        except Exception as exc:
            print(f"[Scout] extraction failed for {name}: {exc}")
            return None

    async def _notify(self, text: str) -> None:
        """iOS app push + iMessage — same path Sentry uses."""
        self.log_event("tool", f"notify (push + iMessage): {text}")
        try:
            import paho.mqtt.publish as mqtt_pub
            mqtt_pub.single(
                "jarvis/surfaces/iphone/push",
                json.dumps({"text": text, "title": "Scout"}),
                hostname=MQTT_HOST, port=MQTT_PORT,
            )
        except Exception as exc:
            print(f"[Scout] surface push failed: {exc}")

        try:
            profile = json.loads(self.r.get("user:profile") or "{}")
            phone = profile.get("imessage_to", "")
            if phone:
                script = (
                    f'tell application "Messages" to send {json.dumps(text)} '
                    f'to buddy "{phone}" of (service 1 whose service type is iMessage)'
                )
                async with aiohttp.ClientSession() as s:
                    await s.post(f"{MAC_BRIDGE_URL}/applescript",
                                 json={"script": script, "timeout": 20},
                                 timeout=aiohttp.ClientTimeout(total=25))
        except Exception as exc:
            print(f"[Scout] iMessage failed: {exc}")

    # ------------------------------------------------------------------
    # Search-query watches (v1 behaviour, unchanged)
    # ------------------------------------------------------------------

    async def _run_query_watches(self, session, queries: list[str]) -> str:
        if not TAVILY_API_KEY:
            return "Web monitor: TAVILY_API_KEY is not configured (query watches skipped)."

        new_results: list[dict] = []
        for query in queries:
            self.log_event("tool", f"Tavily search: {query}")
            try:
                async with session.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": TAVILY_API_KEY,
                        "query": query,
                        "search_depth": "basic",
                        "max_results": 3,
                    },
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    for result in data.get("results", []):
                        url = result.get("url", "")
                        url_hash = hashlib.md5(url.encode()).hexdigest()
                        seen_key = f"agent:web_monitor:seen:{url_hash}"
                        if not self.r.exists(seen_key):
                            self.r.set(seen_key, "1", ex=3 * 24 * 3600)
                            new_results.append(
                                {
                                    "query": query,
                                    "title": result.get("title", ""),
                                    "url": url,
                                    "snippet": result.get("content", "")[:250],
                                }
                            )
            except Exception as exc:
                new_results.append(
                    {
                        "query": query,
                        "title": f"Search error: {exc}",
                        "url": "",
                        "snippet": "",
                    }
                )

        self.log_event(
            "finding",
            f"Query watches: {len(new_results)} new result(s) across "
            f"{len(queries)} quer(ies)",
        )
        if not new_results:
            return "Web monitor: No new content detected for any monitored query."

        results_text = "\n\n".join(
            f"[{r['query']}] **{r['title']}**\n{r['snippet']}\n{r['url']}"
            for r in new_results
        )

        return await complete(
            system=_SYSTEM_PROMPT,
            user=f"Newly detected results:\n\n{results_text}",
            max_tokens=500,
        )
