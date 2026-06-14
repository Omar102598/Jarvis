"""JARVIS LLM Agent tools."""

from .agents import get_agent_report, trigger_agent
from .calendar import get_calendar_events, set_reminder
from .code_exec import run_python
from .comms import send_email, send_sms
from .dispatch import ask_subagent, spawn_task
from .files import list_files, read_file, write_file
from .github_tool import github_tool
from .memory import forget, recall, remember
from .notes import manage_notes
from .notifications import send_notification
from .shell import run_shell
from .smart_home import control_device, get_device_states, get_presence, set_scene
from .spotify import spotify_control
from .vision import get_camera_snapshot
from .weather import get_weather
from .web import fetch_url
from .web_search import web_search

__all__ = [
    "ask_subagent",
    "control_device",
    "fetch_url",
    "forget",
    "get_agent_report",
    "get_calendar_events",
    "get_camera_snapshot",
    "get_device_states",
    "get_presence",
    "get_weather",
    "github_tool",
    "list_files",
    "manage_notes",
    "read_file",
    "recall",
    "remember",
    "run_python",
    "run_shell",
    "send_email",
    "send_notification",
    "send_sms",
    "set_reminder",
    "set_scene",
    "spawn_task",
    "spotify_control",
    "trigger_agent",
    "web_search",
    "write_file",
]
