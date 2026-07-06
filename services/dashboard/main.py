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

import asyncio
import base64
import json
import os
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REDIS_HOST  = os.environ.get("REDIS_HOST", "redis")
MQTT_HOST   = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT   = int(os.environ.get("MQTT_PORT", "1883"))
USER_ID     = os.environ.get("JARVIS_USER_ID", "default")

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

_redis = redis.Redis(host=REDIS_HOST, decode_responses=True)
_mqtt = mqtt.Client()

# Widget files live in services/dashboard/widgets/ (volume-mounted into /app/widgets/)
_WIDGETS_DIR = Path(os.environ.get("WIDGETS_DIR", str(Path(__file__).parent / "widgets")))


def _on_dashboard_reload(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
        print(f"[Dashboard] Widget reload: {data.get('widget', 'unknown')}")
    except Exception:
        pass


def _mqtt_connect():
    try:
        _mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        _mqtt.subscribe("jarvis/dashboard/reload")
        _mqtt.message_callback_add("jarvis/dashboard/reload", _on_dashboard_reload)
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
                "persona_name": meta.get("persona_name", ""),
                "kind": meta.get("kind", "mechanical"),
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
        request=request,
        name="index.html",
        context={
            "agents": agents,
            "recent_reports": recent_reports,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
        # Always serve a fresh dashboard shell so template/JS changes take effect
        # without a manual hard-refresh (was serving stale index.html from cache).
        headers={"Cache-Control": "no-cache, must-revalidate"},
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


@app.get("/api/voice-state")
async def get_voice_state(room: str = "office"):
    """Return the current voice pipeline state and current thinking word (if any)."""
    state         = _redis.get(f"jarvis:voice:state:{room}") or "ready"
    thinking_word = _redis.get("jarvis:thinking:word") or "Thinking"
    return {"room": room, "state": state, "thinking_word": thinking_word}


@app.get("/api/conversation")
async def get_conversation(limit: int = 20):
    """Return recent conversation messages (shared across all surfaces)."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    raw = _redis.lrange(f"conversation:{USER_ID}", -limit, -1)
    messages = []
    for item in raw:
        try:
            messages.append(json.loads(item))
        except json.JSONDecodeError:
            pass
    return {"messages": messages}


_SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
_SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
_SPOTIFY_REDIRECT_URI  = "http://localhost:8888/spotify-callback"
_SPOTIFY_SCOPES        = (
    "user-modify-playback-state user-read-playback-state "
    "user-read-currently-playing user-library-read"
)


@app.get("/spotify-setup", response_class=HTMLResponse)
async def spotify_setup():
    """OAuth helper page — generates auth link so the user can get a refresh token."""
    if not _SPOTIFY_CLIENT_ID:
        return HTMLResponse("<h2>SPOTIFY_CLIENT_ID not set in .env</h2>", status_code=400)

    params = urllib.parse.urlencode({
        "client_id":     _SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  _SPOTIFY_REDIRECT_URI,
        "scope":         _SPOTIFY_SCOPES,
    })
    auth_url = f"https://accounts.spotify.com/authorize?{params}"

    html = f"""<!DOCTYPE html>
<html data-bs-theme="dark">
<head>
  <meta charset="UTF-8">
  <title>JARVIS — Spotify Setup</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>body{{background:#060c18;color:#cdd6e8;font-family:system-ui}}.card{{background:#0a1220;border:1px solid #1a3050}}</style>
</head>
<body class="p-4">
  <h2 style="color:#00d4ff;letter-spacing:4px">SPOTIFY SETUP</h2>
  <div class="card p-4 mt-3" style="max-width:600px">
    <p>Click the button below to authorize Jarvis with your Spotify account.
       You'll be redirected back here with your refresh token.</p>
    <p class="text-warning small">Make sure <code>http://localhost:8888/spotify-callback</code>
       is added as a Redirect URI in your <a href="https://developer.spotify.com/dashboard" target="_blank"
       class="text-info">Spotify Developer Dashboard</a> app settings.</p>
    <a href="{auth_url}" class="btn btn-success mt-2">Authorize with Spotify →</a>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/spotify-callback", response_class=HTMLResponse)
async def spotify_callback(code: str = "", error: str = ""):
    """OAuth callback — exchanges authorization code for refresh token."""
    if error:
        return HTMLResponse(f"<h2 style='color:red'>Spotify auth error: {error}</h2>")
    if not code:
        return HTMLResponse("<h2>No authorization code received.</h2>")

    # Exchange code for tokens
    try:
        auth_header = base64.b64encode(
            f"{_SPOTIFY_CLIENT_ID}:{_SPOTIFY_CLIENT_SECRET}".encode()
        ).decode()
        body = urllib.parse.urlencode({
            "grant_type":   "authorization_code",
            "code":         code,
            "redirect_uri": _SPOTIFY_REDIRECT_URI,
        }).encode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=body,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            tokens = json.loads(resp.read())
    except Exception as e:
        return HTMLResponse(f"<h2 style='color:red'>Token exchange failed: {e}</h2>")

    refresh_token = tokens.get("refresh_token", "")
    access_token  = tokens.get("access_token", "")

    html = f"""<!DOCTYPE html>
<html data-bs-theme="dark">
<head>
  <meta charset="UTF-8">
  <title>JARVIS — Spotify Connected</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>body{{background:#060c18;color:#cdd6e8;font-family:system-ui}}.card{{background:#0a1220;border:1px solid #1a3050}}code{{color:#00d4ff}}</style>
</head>
<body class="p-4">
  <h2 style="color:#00aa88;letter-spacing:4px">✓ SPOTIFY CONNECTED</h2>
  <div class="card p-4 mt-3" style="max-width:700px">
    <p>Copy the refresh token below and add it to your <code>.env</code> file:</p>
    <pre style="background:#040a14;padding:12px;border-radius:6px;color:#7dc8e8;word-break:break-all">SPOTIFY_REFRESH_TOKEN={refresh_token}</pre>
    <p class="small text-muted mt-3">After saving .env, restart the llm_agent container:
      <code>docker compose up -d llm_agent</code>
    </p>
    <p class="small text-warning">Keep this token private — it gives full playback control.</p>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/api/widgets")
async def list_widgets():
    """Return manifests for all installed dashboard widgets."""
    _WIDGETS_DIR.mkdir(parents=True, exist_ok=True)
    widgets = []
    for widget_dir in sorted(_WIDGETS_DIR.iterdir()):
        if not widget_dir.is_dir():
            continue
        manifest_path = widget_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            widgets.append(json.loads(manifest_path.read_text()))
        except Exception:
            pass
    return {"widgets": widgets}


@app.get("/widgets/{name}/html")
async def widget_html(name: str):
    """Serve the HTML partial for a widget."""
    safe_name = name.replace("/", "").replace("..", "")
    html_path = _WIDGETS_DIR / safe_name / "widget.html"
    if not html_path.exists():
        raise HTTPException(404, f"Widget '{safe_name}' not found")
    # no-cache so edited widgets always re-fetch (browsers were serving stale HTML)
    return FileResponse(
        str(html_path), media_type="text/html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/api/widgets/{name}/data")
async def widget_data(name: str):
    """Return live data for a widget (widgets may also store data in Redis at widget:<name>:data)."""
    safe_name = name.replace("/", "").replace("..", "")
    if not (_WIDGETS_DIR / safe_name).exists():
        raise HTTPException(404, f"Widget '{safe_name}' not found")
    data = {}
    try:
        raw = _redis.get(f"widget:{safe_name}:data")
        if raw:
            data = json.loads(raw)
    except Exception:
        pass
    return {"widget": safe_name, "data": data, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/tool-events")
async def get_tool_events(limit: int = 30):
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    raw = _redis.lrange("jarvis:tool_events", 0, limit - 1)
    events = []
    for item in raw:
        try:
            events.append(json.loads(item))
        except json.JSONDecodeError:
            pass
    return {"events": events}


@app.get("/api/stream")
async def event_stream():
    """Server-Sent Events stream pushing conversation + tool events + voice state in real-time."""
    async def generate():
        last_sig = None
        while True:
            try:
                conv_raw = _redis.lrange(f"conversation:{USER_ID}", -30, -1)
                tool_raw = _redis.lrange("jarvis:tool_events", 0, 19)
                voice_state = _redis.get(f"jarvis:voice:state:office") or "ready"
                thinking_word = _redis.get("jarvis:thinking:word") or ""

                # Signature must be CONTENT-based: both lists are windowed reads
                # of capped Redis lists, so their LENGTHS stop changing once the
                # window fills (30 msgs / 20 events) — comparing lengths froze
                # the stream exactly when the dashboard got busy.
                sig = (
                    conv_raw[-1][:200] if conv_raw else "",
                    len(conv_raw),
                    tool_raw[0][:200] if tool_raw else "",
                    voice_state,
                    thinking_word,
                )

                if sig != last_sig:
                    last_sig = sig

                    messages = []
                    for item in conv_raw:
                        try:
                            messages.append(json.loads(item))
                        except json.JSONDecodeError:
                            pass

                    tool_events = []
                    for item in tool_raw:
                        try:
                            tool_events.append(json.loads(item))
                        except json.JSONDecodeError:
                            pass

                    payload = json.dumps({
                        "messages": messages,
                        "tool_events": tool_events,
                        "voice_state": voice_state,
                        "thinking_word": thinking_word,
                    })
                    yield f"data: {payload}\n\n"

            except Exception:
                pass

            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Service logs — proxied from mac_bridge on the host (which can reach both
# docker logs and the native services' log files)
# ---------------------------------------------------------------------------

MAC_BRIDGE_URL = os.environ.get("MAC_BRIDGE_URL", "http://host.docker.internal:7777")


def _bridge_get(path: str) -> dict:
    req = urllib.request.Request(f"{MAC_BRIDGE_URL}{path}")
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read())


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """Service logs viewer — separate page, linked from the dashboard header."""
    return templates.TemplateResponse(request=request, name="logs.html")


@app.get("/api/logs/services")
async def api_logs_services():
    try:
        return _bridge_get("/logs/services")
    except Exception as e:
        raise HTTPException(502, f"mac_bridge unreachable: {e}")


@app.get("/api/logs/tail")
async def api_logs_tail(service: str, lines: int = 200):
    try:
        q = urllib.parse.urlencode({"service": service, "lines": min(lines, 1000)})
        return _bridge_get(f"/logs/tail?{q}")
    except urllib.error.HTTPError as e:
        raise HTTPException(e.code, e.read().decode()[:200])
    except Exception as e:
        raise HTTPException(502, f"mac_bridge unreachable: {e}")


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
