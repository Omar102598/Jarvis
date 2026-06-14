"""JARVIS LLM Agent tools."""

from .agents import get_agent_report
from .calendar import get_calendar_events, set_reminder
from .notifications import send_notification
from .smart_home import control_device, get_device_states, get_presence, set_scene
from .vision import get_camera_snapshot
from .weather import get_weather
from .web_search import web_search

__all__ = [
    "control_device",
    "get_agent_report",
    "get_calendar_events",
    "get_camera_snapshot",
    "get_device_states",
    "get_presence",
    "get_weather",
    "send_notification",
    "set_reminder",
    "set_scene",
    "web_search",
]
