"""Unit tests for LLM Agent tools.

These tests mock external dependencies (Home Assistant, Tavily, OpenAI)
and verify the tool logic works correctly.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add the llm_agent service to the path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "llm_agent"))


class MockResponse:
    """Mock aiohttp response that supports async context manager."""

    def __init__(self, status, json_data=None, text_data=""):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def read(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockSession:
    """Mock aiohttp.ClientSession that supports async context manager."""

    def __init__(self, response):
        self._response = response

    def post(self, *args, **kwargs):
        return self._response

    def get(self, *args, **kwargs):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# --- Smart Home Tools ---


@pytest.mark.asyncio
async def test_control_device_success():
    """Test successful device control via Home Assistant."""
    from tools.smart_home import control_device

    resp = MockResponse(200)
    session = MockSession(resp)

    with patch("tools.smart_home.aiohttp.ClientSession", return_value=session):
        result = await control_device.ainvoke(
            {"entity_id": "light.bedroom_govee", "action": "turn_on"}
        )
    assert "Done" in result
    assert "light.bedroom_govee" in result


@pytest.mark.asyncio
async def test_control_device_error():
    """Test error handling for device control."""
    from tools.smart_home import control_device

    resp = MockResponse(404, text_data="Not found")
    session = MockSession(resp)

    with patch("tools.smart_home.aiohttp.ClientSession", return_value=session):
        result = await control_device.ainvoke(
            {"entity_id": "light.nonexistent", "action": "turn_on"}
        )
    assert "Error" in result


# --- Web Search Tool ---


@pytest.mark.asyncio
async def test_web_search_no_api_key():
    """Test web search without API key configured."""
    from tools.web_search import web_search

    with patch("tools.web_search.TAVILY_API_KEY", None):
        result = await web_search.ainvoke({"query": "test query"})
    assert "not configured" in result


@pytest.mark.asyncio
async def test_web_search_success():
    """Test successful web search."""
    from tools.web_search import web_search

    mock_data = {
        "results": [
            {
                "title": "Test Result",
                "content": "This is a test result about the weather.",
                "url": "https://example.com",
            }
        ]
    }

    resp = MockResponse(200, json_data=mock_data)
    session = MockSession(resp)

    with patch("tools.web_search.TAVILY_API_KEY", "tvly-test"), \
         patch("tools.web_search.aiohttp.ClientSession", return_value=session):
        result = await web_search.ainvoke({"query": "weather today"})
    assert "Test Result" in result
    assert "example.com" in result


# --- Calendar Tool ---


@pytest.mark.asyncio
async def test_calendar_events_empty():
    """Test calendar with no events."""
    from tools.calendar import get_calendar_events

    resp = MockResponse(200, json_data=[])
    session = MockSession(resp)

    with patch("tools.calendar.aiohttp.ClientSession", return_value=session):
        result = await get_calendar_events.ainvoke({})
    assert "No events" in result


@pytest.mark.asyncio
async def test_calendar_date_calculation():
    """Test that calendar date calculation handles month boundaries."""
    from tools.calendar import get_calendar_events

    resp = MockResponse(200, json_data=[])
    session = MockSession(resp)

    with patch("tools.calendar.aiohttp.ClientSession", return_value=session):
        # Should not raise ValueError for days=30 near end of month
        result = await get_calendar_events.ainvoke({"days": 30})
    assert isinstance(result, str)


# --- Weather Tool ---


@pytest.mark.asyncio
async def test_weather_success():
    """Test successful weather fetch."""
    from tools.weather import get_weather

    mock_data = {
        "state": "sunny",
        "attributes": {
            "temperature": 72,
            "temperature_unit": "°F",
            "humidity": 45,
            "wind_speed": 8,
            "wind_speed_unit": "mph",
            "forecast": [],
        },
    }

    resp = MockResponse(200, json_data=mock_data)
    session = MockSession(resp)

    with patch("tools.weather.aiohttp.ClientSession", return_value=session):
        result = await get_weather.ainvoke({})
    assert "sunny" in result
    assert "72" in result


# --- Notification Tool ---


@pytest.mark.asyncio
async def test_send_notification_success():
    """Test successful notification send."""
    from tools.notifications import send_notification

    resp = MockResponse(200)
    session = MockSession(resp)

    with patch("tools.notifications.aiohttp.ClientSession", return_value=session):
        result = await send_notification.ainvoke({"message": "Test notification"})
    assert "Notification sent" in result
