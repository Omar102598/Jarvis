"""Vision tools for JARVIS — camera snapshots + GPT-4o analysis."""

import base64
import os

import aiohttp
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")


@tool
async def get_camera_snapshot(camera: str, question: str = "Describe what you see.") -> str:
    """Capture a snapshot from a camera and analyze it with vision AI.

    Args:
        camera: Camera entity ID, e.g. 'camera.front_door', 'camera.backyard'
        question: What to look for, e.g. 'Is anyone there?', 'What is happening?'
    """
    # Fetch snapshot from Home Assistant
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{HA_URL}/api/camera_proxy/{camera}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
        ) as resp:
            if resp.status != 200:
                return f"Failed to get snapshot from {camera}: {resp.status}"
            image_bytes = await resp.read()

    # Analyze with GPT-4o vision
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": question},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64_image}",
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 300,
            },
        ) as resp:
            if resp.status != 200:
                error = await resp.text()
                return f"Vision analysis failed: {resp.status} - {error}"
            data = await resp.json()
            return data["choices"][0]["message"]["content"]
