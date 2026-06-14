"""Weather tool for JARVIS — uses Home Assistant weather integration."""

import os

import aiohttp
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")


@tool
async def get_weather(entity_id: str = "weather.home") -> str:
    """Get the current weather and forecast.

    Args:
        entity_id: Weather entity ID in Home Assistant, e.g. 'weather.home'
    """
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{HA_URL}/api/states/{entity_id}",
            headers=headers,
        ) as resp:
            if resp.status != 200:
                return f"Failed to get weather: {resp.status}"
            data = await resp.json()

    state = data.get("state", "unknown")
    attrs = data.get("attributes", {})
    temp = attrs.get("temperature", "?")
    temp_unit = attrs.get("temperature_unit", "°F")
    humidity = attrs.get("humidity", "?")
    wind_speed = attrs.get("wind_speed", "?")
    wind_unit = attrs.get("wind_speed_unit", "mph")

    result = f"Current: {state}, {temp}{temp_unit}, Humidity: {humidity}%, Wind: {wind_speed} {wind_unit}"

    # Include forecast if available
    forecast = attrs.get("forecast", [])
    if forecast:
        result += "\n\nForecast:"
        for day in forecast[:5]:
            date = day.get("datetime", "?")[:10]
            condition = day.get("condition", "?")
            high = day.get("temperature", "?")
            low = day.get("templow", "?")
            result += f"\n  {date}: {condition}, High {high}{temp_unit}, Low {low}{temp_unit}"

    return result
