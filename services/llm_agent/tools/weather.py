"""Weather tool for JARVIS — uses web search (Chrome) as primary source.

Falls back to Home Assistant only if HA_TOKEN is configured.
"""

import os
import aiohttp
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")


@tool
async def get_weather(entity_id: str = "weather.home") -> str:
    """Get the current weather and forecast.

    Fetches weather via web search (wttr.in) by default.
    Falls back to Home Assistant if HA_TOKEN is configured.

    Args:
        entity_id: Weather entity ID in Home Assistant, e.g. 'weather.home'
                   (only used when HA_TOKEN is set)
    """
    # --- Primary: use wttr.in JSON API (no auth needed) ---
    try:
        location = os.environ.get("WEATHER_LOCATION", "")
        url = f"https://wttr.in/{location}?format=j1"

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "JARVIS/1.0"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    current = data["current_condition"][0]
                    area = data.get("nearest_area", [{}])[0]
                    city = area.get("areaName", [{}])[0].get("value", "")
                    country = area.get("country", [{}])[0].get("value", "")
                    location_str = f"{city}, {country}" if city else "your location"

                    temp_f = current.get("temp_F", "?")
                    temp_c = current.get("temp_C", "?")
                    feels_f = current.get("FeelsLikeF", "?")
                    humidity = current.get("humidity", "?")
                    wind_mph = current.get("windspeedMiles", "?")
                    desc = current.get("weatherDesc", [{}])[0].get("value", "?")
                    visibility = current.get("visibility", "?")

                    result = (
                        f"Weather for {location_str}:\n"
                        f"  Condition: {desc}\n"
                        f"  Temperature: {temp_f}°F ({temp_c}°C), feels like {feels_f}°F\n"
                        f"  Humidity: {humidity}%\n"
                        f"  Wind: {wind_mph} mph\n"
                        f"  Visibility: {visibility} km"
                    )

                    # 3-day forecast
                    weather_days = data.get("weather", [])
                    if weather_days:
                        result += "\n\nForecast:"
                        for day in weather_days[:3]:
                            date = day.get("date", "?")
                            max_f = day.get("maxtempF", "?")
                            min_f = day.get("mintempF", "?")
                            day_desc = day.get("hourly", [{}])[4].get(
                                "weatherDesc", [{}]
                            )[0].get("value", "?")
                            result += f"\n  {date}: {day_desc}, High {max_f}°F / Low {min_f}°F"

                    return result

    except Exception as e:
        # Fall through to HA fallback
        ha_error = str(e)

    # --- Fallback: Home Assistant (only if token is set) ---
    if not HA_TOKEN:
        return (
            "Unable to fetch weather from web service. "
            "Home Assistant is also not configured (HA_TOKEN missing). "
            "Please set WEATHER_LOCATION in your .env for accurate results."
        )

    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HA_URL}/api/states/{entity_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    return "Error: Unauthorized — check your HA_TOKEN."
                if resp.status == 404:
                    return (
                        f"Error: Entity '{entity_id}' not found in Home Assistant."
                    )
                if resp.status != 200:
                    text = await resp.text()
                    return f"Error: Home Assistant returned status {resp.status}: {text}"
                data = await resp.json()

    except Exception as e:
        return f"Weather unavailable: web service failed ({ha_error}), HA also failed ({e})."

    state = data.get("state", "unknown")
    attrs = data.get("attributes", {})
    temp = attrs.get("temperature", "?")
    temp_unit = attrs.get("temperature_unit", "°F")
    humidity = attrs.get("humidity", "?")
    wind_speed = attrs.get("wind_speed", "?")
    wind_unit = attrs.get("wind_speed_unit", "mph")

    result = (
        f"Current: {state}, {temp}{temp_unit}, "
        f"Humidity: {humidity}%, Wind: {wind_speed} {wind_unit}"
    )

    forecast = attrs.get("forecast", [])
    if forecast:
        result += "\n\nForecast:"
        for day in forecast[:5]:
            date = day.get("datetime", "?")[:10]
            condition = day.get("condition", "?")
            high = day.get("temperature", "?")
            low = day.get("templow", "?")
            result += (
                f"\n  {date}: {condition}, High {high}{temp_unit}, Low {low}{temp_unit}"
            )

    return result
