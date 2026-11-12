#!/usr/bin/env python3
"""Screenshot the dashboard from THIS checkout — Forge's visual verify loop.

Starts the dashboard (services/dashboard) on a local port against a local
Redis seeded with sample data, then captures each page at desktop and phone
widths with headless Chrome. Forge reads the PNGs, fixes what looks wrong,
and runs it again. The Forge workflow also runs it once at the end and puts
the final shots in the PR, so the Ship card shows the change, not a summary.

    python3 scripts/forge_dash_shots.py                    # all pages
    python3 scripts/forge_dash_shots.py --pages /chat      # just one
    python3 scripts/forge_dash_shots.py --out /tmp/shots --no-seed

Needs: a Redis on REDIS_HOST:6379 (CI starts one: docker run -p 6379:6379
redis:7), the dashboard requirements, and Playwright
(pip install playwright && python -m playwright install --with-deps chromium).
Never point it at the live Redis with seeding on: --no-seed is for that.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DASH = REPO / "services" / "dashboard"
SIZES = {"desktop": (1280, 900), "phone": (390, 844)}
DEFAULT_PAGES = ["/", "/chat", "/voice"]


def seed(host: str, port: int) -> None:
    """Sample data that exercises every chat renderer: markdown, tool calls,
    cards of each type, a photo turn, plus a pending approval."""
    import redis
    r = redis.Redis(host=host, port=port, decode_responses=True)
    now = datetime.now(timezone.utc)
    ts = lambda m: (now - timedelta(minutes=m)).isoformat()
    conv = [
        {"role": "user", "content": "Turn on the bedroom lamp.", "turn_id": "office-aaaaaaaaaaaa", "ts": ts(30)},
        {"role": "assistant", "content": "The bedroom lamp is on, sir.", "turn_id": "office-aaaaaaaaaaaa", "ts": ts(30)},
        {"role": "user", "content": "[camera image]\nHow many calories is this?", "turn_id": "app-bbbbbbbbbbbb", "ts": ts(20)},
        {"role": "assistant", "turn_id": "app-bbbbbbbbbbbb", "ts": ts(20),
         "content": "## Estimate\n- **Beef**, ~7 oz: `480 kcal`\n- Sweet potato: 110 kcal\n\n"
                    "| item | kcal |\n|---|---|\n| beef | 480 |\n| potato | 110 |\n\n"
                    "Logged it, sir. You have **650 kcal** left today.",
         "cards": [
             {"type": "macros", "title": "Today", "calories": {"value": 1650, "target": 2300},
              "bars": [{"label": "Protein", "value": 140, "target": 190, "unit": "g"},
                       {"label": "Carbs", "value": 150, "target": 220, "unit": "g", "color": "#f0a020"},
                       {"label": "Fat", "value": 48, "target": 70, "unit": "g", "color": "#3ddc97"}],
              "meals": ["Eggs + toast · 420 kcal", "Beef + potato · 590 kcal"], "footer": "650 kcal left"},
             {"type": "chart", "title": "Weight", "kind": "line", "unit": "lb", "target": 179,
              "points": [{"x": d, "y": y} for d, y in
                         zip(["Mon", "Tue", "Wed", "Thu", "Fri"], [181.2, 180.8, 180.9, 180.1, 179.8])]},
             {"type": "stats", "title": "Readiness", "items": [
                 {"label": "Sleep", "value": "7h 40m"}, {"label": "HRV", "value": "62", "sub": "+4"},
                 {"label": "Resting HR", "value": "54"}]},
             {"type": "table", "title": "This week", "columns": ["Day", "Workout"],
              "rows": [["Mon", "A — chest/back"], ["Wed", "B — shoulders/arms"], ["Fri", "C — legs"]]},
             {"type": "action", "title": "Order groceries", "subtitle": "HEB · 14 items · $86",
              "status": "pending"},
         ]},
        {"role": "user", "content": "What time is it?", "turn_id": "office-cccccccccccc", "ts": ts(2)},
        {"role": "assistant", "content": "It's 9:59 in the evening, sir.", "turn_id": "office-cccccccccccc", "ts": ts(2)},
    ]
    pipe = r.pipeline()
    pipe.delete("conversation:default", "jarvis:tool_events")
    for m in conv:
        pipe.rpush("conversation:default", json.dumps(m))
    for ev in [
        {"tool": "control_device", "status": "done", "turn_id": "office-aaaaaaaaaaaa", "call_id": "t1",
         "args_preview": '{"entity_id": "light.bedroom_lamp", "action": "turn_on"}',
         "result_preview": "Turned on light.bedroom_lamp", "timestamp": ts(30)},
        {"tool": "log_food", "status": "done", "turn_id": "app-bbbbbbbbbbbb", "call_id": "t2",
         "args_preview": '{"description": "beef + sweet potato"}', "result_preview": "Logged 590 kcal",
         "timestamp": ts(20)},
    ]:
        pipe.lpush("jarvis:tool_events", json.dumps(ev))
    pipe.hset("jarvis:approvals:pending", "sample000001", json.dumps({
        "id": "sample000001", "source": "Forge", "title": "Ship: sample change",
        "text": "PR #0: sample\n✅ Golden tasks passed", "created": ts(5),
        "expires": time.time() + 3600}))
    pipe.execute()


def port_open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_dashboard(port: int, redis_host: str, redis_port: int) -> subprocess.Popen:
    env = {**os.environ, "REDIS_HOST": redis_host, "REDIS_PORT": str(redis_port), "MQTT_HOST": "127.0.0.1",
           "DASHBOARD_PASSWORD": "", "WIDGETS_DIR": str(DASH / "widgets"),
           "MAC_BRIDGE_HOST": "127.0.0.1", "GATEWAY_INTERNAL_URL": "http://127.0.0.1:1"}
    log = open(f"/tmp/forge_dash_{port}.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=DASH, env=env, stdout=log, stderr=log)
    for _ in range(40):
        if proc.poll() is not None:
            sys.exit(f"Dashboard exited early — see /tmp/forge_dash_{port}.log:\n"
                     + Path(f"/tmp/forge_dash_{port}.log").read_text()[-2000:])
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/chat", timeout=2)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    sys.exit("Dashboard didn't answer within 20s.")


def shoot_all(base: str, pages: list[str], out: Path) -> list[Path]:
    """Playwright, not bare `chrome --screenshot`: /chat holds an SSE stream
    open, so idle-waiting never finishes, and a fixed --timeout captured black
    frames before first paint."""
    from playwright.sync_api import sync_playwright
    shots = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for label, (w, h) in SIZES.items():
            ctx = browser.new_context(viewport={"width": w, "height": h},
                                      device_scale_factor=1 if label == "desktop" else 2,
                                      is_mobile=label == "phone", has_touch=label == "phone")
            # Skip the once-per-session boot splash, or the first page shot
            # is the splash instead of the page.
            ctx.add_init_script("sessionStorage.setItem('jarvis_booted', '1')")
            page = ctx.new_page()
            for p in pages:
                slug = p.strip("/").replace("/", "_") or "home"
                path = out / f"{slug}-{label}.png"
                try:
                    page.goto(base + p, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(2500)   # SSE first frame + CDN scripts
                    page.screenshot(path=str(path), full_page=True)
                    shots.append(path)
                except Exception as exc:
                    print(f"  ✗ {path.name}: {exc}")
            ctx.close()
        browser.close()
    return shots


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", nargs="*", default=DEFAULT_PAGES)
    ap.add_argument("--out", default="/tmp/forge-shots")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--redis-host", default=os.environ.get("SHOTS_REDIS_HOST", "127.0.0.1"))
    ap.add_argument("--redis-port", type=int, default=int(os.environ.get("SHOTS_REDIS_PORT", "6379")))
    ap.add_argument("--no-seed", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if not args.no_seed:
        seed(args.redis_host, args.redis_port)
    proc = None if port_open(args.port) else start_dashboard(args.port, args.redis_host, args.redis_port)
    try:
        for path in shoot_all(f"http://127.0.0.1:{args.port}", args.pages, out):
            print(path)
    finally:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    main()
