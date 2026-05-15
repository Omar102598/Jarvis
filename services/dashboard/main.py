"""JARVIS Agent Dashboard.

Provides a web UI for monitoring and manually triggering background agents.
Reads agent status and reports from Redis; publishes manual-run triggers to MQTT.

Endpoints
---------
GET  /                              — HTML dashboard
GET  /api/agents                    — JSON list of all agents with current status
GET  /api/reports/{name}?limit=N   — JSON list of recent reports for one agent
POST /api/agents/{name}/run         — Trigger an agent immediately (publishes to MQTT)
GET  /health                        — Health check
"""

import json
import os
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

_redis = redis.Redis(host=REDIS_HOST, decode_responses=True)
_mqtt = mqtt.Client()


def _mqtt_connect():
    try:
        _mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        _mqtt.loop_start()
    except OSError as exc:
        print(f"[Dashboard] MQTT connect warning: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _mqtt_connect()
    yield
    _mqtt.loop_stop()
    _mqtt.disconnect()


# ---------------------------------------------------------------------------
# FastAPI + templates
# ---------------------------------------------------------------------------

app = FastAPI(
    title="JARVIS Dashboard",
    description="Background agent monitoring and control",
    version="1.0.0",
    lifespan=lifespan,
)
templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KNOWN_AGENTS = ["newsletter", "job_monitor", "web_monitor"]


def _get_all_agents() -> list[dict]:
    """Return status + metadata for every known agent."""
    agents = []

    # Discover agents from Redis metadata keys
    meta_keys = _redis.keys("agent:*:meta")
    seen_names = set()

    for key in meta_keys:
        raw = _redis.get(key)
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            continue

        name = meta.get("name", "")
        if not name or name in seen_names:
            continue
        seen_names.add(name)

        status = _redis.get(f"agent:{name}:status") or "unknown"
        last_run = _redis.get(f"agent:{name}:last_run")
        last_error = _redis.get(f"agent:{name}:last_error")

        # Get the most recent report (preview)
        latest_report = ""
        reports_raw = _redis.lrange(f"agent:{name}:reports", 0, 0)
        if reports_raw:
            try:
                entry = json.loads(reports_raw[0])
                latest_report = entry.get("report", "")[:200]
            except json.JSONDecodeError:
                pass

        agents.append(
            {
                "name": name,
                "display_name": meta.get("display_name", name),
                "description": meta.get("description", ""),
                "schedule": meta.get("schedule", ""),
                "enabled": meta.get("enabled", False),
                "status": status,
                "last_run": last_run,
                "last_error": last_error,
                "latest_report_preview": latest_report,
            }
        )

    # Sort by display_name
    agents.sort(key=lambda a: a["display_name"])
    return agents


def _get_reports(name: str, limit: int = 10) -> list[dict]:
    raw_list = _redis.lrange(f"agent:{name}:reports", 0, limit - 1)
    reports = []
    for raw in raw_list:
        try:
            reports.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return reports


def _get_all_recent_reports(limit: int = 20) -> list[dict]:
    """Return the most recent reports across all agents, newest first."""
    all_reports = []
    meta_keys = _redis.keys("agent:*:meta")
    for key in meta_keys:
        raw = _redis.get(key)
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            continue
        name = meta.get("name", "")
        if not name:
            continue
        all_reports.extend(_get_reports(name, limit=5))

    all_reports.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return all_reports[:limit]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    agents = _get_all_agents()
    recent_reports = _get_all_recent_reports(limit=20)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "agents": agents,
            "recent_reports": recent_reports,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
    )


@app.get("/api/agents")
async def list_agents():
    return _get_all_agents()


@app.get("/api/reports/{name}")
async def get_agent_reports(name: str, limit: int = 10):
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 50")
    reports = _get_reports(name, limit=limit)
    return {"agent": name, "reports": reports}


@app.get("/api/reports")
async def get_all_recent_reports(limit: int = 20):
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    return {"reports": _get_all_recent_reports(limit=limit)}


@app.post("/api/agents/{name}/run")
async def trigger_agent(name: str):
    """Publish a manual-run trigger to the agent_runner via MQTT."""
    _mqtt.publish(
        f"jarvis/agents/{name}/trigger",
        json.dumps(
            {
                "triggered_by": "dashboard",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ),
    )
    return {"status": "triggered", "agent": name}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "jarvis-dashboard"}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("DASHBOARD_PORT", "8888"))
    uvicorn.run(app, host="0.0.0.0", port=port)
