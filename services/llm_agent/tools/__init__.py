"""JARVIS LLM Agent tools."""

from .calendar import get_calendar_events, set_reminder
from .smart_home import control_device, get_device_states, get_presence, set_scene
from .vision import get_camera_snapshot
from .web_search import web_search

__all__ = [
    "control_device",
    "get_calendar_events",
    "get_camera_snapshot",
    "get_device_states",
    "get_presence",
    "set_reminder",
    "set_scene",
    "web_search",
]
