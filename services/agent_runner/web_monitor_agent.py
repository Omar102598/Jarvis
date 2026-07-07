"""Web monitor agent — "Scout".

Two watch modes, both configured under params in config/agents.yml:

1. ``queries``    — Tavily search queries; reports newly published results
                    (3-day dedup via Redis, unchanged from v1).
2. ``watch_urls`` — specific pages watched for NEW AVAILABLE APARTMENT UNITS
                    (or any listing-style content). Each run the page is
                    rendered via the Mac Bridge headless scraper (falls back
                    to a plain GET), the visible text is hashed, and ONLY when
                    the page actually changed is an LLM asked to extract the
                    current unit list and diff it against the last snapshot
                    stored in Redis. New units trigger an iOS push + iMessage,
                    with priority emphasis for units matching
                    ``priority_keywords`` (e.g. "1st floor", "patio", "yard").

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

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_SYSTEM_PROMPT = (
    "You are JARVIS. Summarise these newly detected web results for your user. "
    "Group by topic, highlight what is most interesting or actionable, and keep "
    "it under 300 words. Use bullet points."
)

_EXTRACT_SYSTEM = (
    "You are Scout, JARVIS's web monitor. You extract apartment/unit "
    "availability from rendered page text and diff it against the previous "
    "snapshot. Reply with STRICT JSON only — no prose, no markdown fences."
)

_EXTRACT_USER = """Watch: {name}
URL: {url}
Priority keywords: {keywords}

Previously seen available units (JSON): {previous}

Current page text:
<<<
{text}
>>>

Return JSON exactly in this shape:
{{"units": [{{"id": "...", "plan": "...", "floor": null, "price": "...", "available": "...", "priority": false, "why_priority": ""}}],
  "new_unit_ids": [], "note": ""}}

Rules:
- "units": EVERY currently listed available unit (or per-floor-plan availability
  entry) on the page. Use the apartment/unit number as "id" when shown;
  otherwise use the floor plan name plus a distinguishing detail.
- "floor": integer floor number if determinable (unit numbers often encode it,
  e.g. unit 1104 on a garden property is likely floor 1), else null.
- priority=true when the unit matches ANY priority keyword, is on floor 1 /
  ground floor, or mentions a private patio/yard. Explain in "why_priority".
- "new_unit_ids": ids from "units" NOT present in the previous snapshot
  (match loosely on unit number / plan name). Empty list if none.
- If the page shows no availability info (blocked, error page, empty), return
  empty "units" and explain in "note"."""

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
        watches: list[dict] = self.params.get("watch_urls", [])
        if not queries and not watches:
            return (
                "Web monitor agent: nothing configured. Add params.queries "
                "and/or params.watch_urls in config/agents.yml."
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
                    record(f"• {name}: unchanged ({len(units)} unit(s) available)")
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
                text = await self._fetch_page(session, url)
                if not text or len(text) < 200 or _BLOCK_RE.search(text):
                    record(f"• {name}: page fetch failed or bot-blocked — will retry next run")
                    continue

                normalized = " ".join(text.split())
                page_hash = hashlib.md5(normalized.encode()).hexdigest()
                if self.r.get(hash_key) == page_hash and prev_units_raw is not None:
                    count = len(json.loads(prev_units_raw))
                    record(f"• {name}: unchanged ({count} unit(s) available)")
                    continue

                extracted = await self._extract_units(
                    name, url, keywords, prev_units_raw, normalized[:11000]
                )
                if extracted is None:
                    record(f"• {name}: page changed but unit extraction failed — will retry next run")
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
                    f"• {name}: baseline recorded — {len(units)} unit(s) available"
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
                record(f"• {name}: page changed, no new units ({len(units)} available)"
                             + (f" [{note}]" if note else ""))
                continue

            record(f"• {name}: 🚨 {len(fresh)} NEW unit(s)!")
            for u in fresh:
                desc = self._unit_line(name, u)
                if u.get("priority"):
                    priority_alerts.append("⭐ PRIORITY " + desc)
                else:
                    alerts.append(desc)

        all_alerts = priority_alerts + alerts
        if all_alerts and self.params.get("notify", True):
            await self._notify(
                "🏠 Scout — new apartment availability:\n" + "\n".join(all_alerts)
            )

        report = "Scout — URL watches\n\n" + "\n".join(lines)
        if all_alerts:
            report += "\n\nNew units:\n" + "\n".join(all_alerts)
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

    async def _extract_units(self, name, url, keywords, prev_raw, text) -> dict | None:
        previous = prev_raw if prev_raw else "NONE (first scan)"
        try:
            reply = await complete(
                system=_EXTRACT_SYSTEM,
                user=_EXTRACT_USER.format(
                    name=name, url=url, keywords=", ".join(keywords),
                    previous=previous, text=text,
                ),
                max_tokens=1400,
                temperature=0.0,
            )
            start, end = reply.find("{"), reply.rfind("}") + 1
            if start < 0:
                return None
            data = json.loads(reply[start:end])
            return data if isinstance(data, dict) else None
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
