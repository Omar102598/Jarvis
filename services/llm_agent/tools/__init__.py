"""JARVIS LLM Agent tools."""

from .agents import get_agent_report, trigger_agent
from .calendar import get_calendar_events, set_reminder
from .code_exec import run_python
from .comms import send_email, send_sms
from .dispatch import ask_subagent, spawn_task
from .files import list_files, read_file, write_file
from .github_tool import github_tool
from .mac import (
    mac_applescript,
    mac_browser_click,
    mac_browser_fill,
    mac_browser_navigate,
    mac_browser_read,
    mac_browser_screenshot,
    mac_clipboard,
    mac_notify,
    mac_open,
    mac_shell,
    mac_screenshot,
    mac_spotlight,
    mac_system_info,
    mac_type,
)
from .memory import forget, recall, remember, consolidate_memory
from .chronicle import recall_journal
from .finance_insights import get_spending_insights
from .tasks import manage_tasks
from .health import get_readiness
from .nutrition import log_meal, get_nutrition_today
from .visual_memory import log_sighting, recall_visual
from .capture import quick_capture
from .packages import get_packages
from .actions import get_recent_actions, undo_last_action
from .firecrawl import scrape_page
from .people import manage_people
from .routines import detect_routines
from .travel import plan_trip, get_trips
from .calls import make_call
from .watches import manage_watches
from .presence import arrive_home, leave_home
from .notes import manage_notes
from .notifications import send_notification
from .petkit import (
    feed_pet,
    get_feeder_status,
    get_feeding_schedule,
    get_fountain_status,
    list_petkit_devices,
    toggle_feeding_plan,
)
from .shell import run_shell
from .smart_home import control_device, get_device_states, get_presence, set_scene
from .scenes import manage_scenes
from .email_drafts import get_email_drafts
from .focus import focus_mode
from .spotify import spotify_control
from .spotify_desktop import spotify_desktop
from .self_modify import (
    jarvis_find_file, jarvis_grep_code,
    jarvis_list_files, jarvis_read_code, jarvis_rebuild, jarvis_write_code,
)
from .developer import dev_list_dir, dev_read_file, dev_search_code, dev_shell, dev_write_file
from .vision import get_camera_snapshot
from .weather import get_weather
from .web import fetch_url
from .web_search import web_search

__all__ = [
    "ask_subagent",
    "control_device",
    "feed_pet",
    "fetch_url",
    "forget",
    "get_agent_report",
    "get_calendar_events",
    "get_camera_snapshot",
    "get_device_states",
    "get_feeder_status",
    "get_feeding_schedule",
    "get_fountain_status",
    "get_presence",
    "get_weather",
    "github_tool",
    "list_files",
    "list_petkit_devices",
    "dev_list_dir",
    "dev_read_file",
    "dev_search_code",
    "dev_shell",
    "dev_write_file",
    "jarvis_find_file",
    "jarvis_grep_code",
    "jarvis_list_files",
    "jarvis_read_code",
    "jarvis_rebuild",
    "jarvis_write_code",
    "mac_applescript",
    "mac_chrome_js",
    "mac_chrome_navigate",
    "mac_chrome_read",
    "mac_browser_click",
    "mac_browser_fill",
    "mac_browser_navigate",
    "mac_browser_read",
    "mac_browser_screenshot",
    "mac_clipboard",
    "mac_notify",
    "mac_open",
    "mac_shell",
    "mac_screenshot",
    "mac_spotlight",
    "mac_system_info",
    "mac_type",
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
    "spotify_desktop",
    "toggle_feeding_plan",
    "trigger_agent",
    "web_search",
    "write_file",
]
