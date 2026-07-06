"""Vision tools for JARVIS — camera snapshots analyzed by the configured LLM
(Claude via llm_factory — same vision path as mac_screenshot; no OpenAI
dependency)."""

import base64
import os

import aiohttp
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN")


@tool
async def get_camera_snapshot(camera: str, question: str = "Describe what you see.") -> str:
    """Capture a snapshot from a camera and analyze it with vision AI.

    Args:
        camera: Camera entity ID, e.g. 'camera.front_door', 'camera.backyard'
        question: What to look for, e.g. 'Is anyone there?', 'What is happening?'
    """
    if not HA_TOKEN:
        return ("No Home Assistant configured (HA_URL/HA_TOKEN missing) — "
                "camera snapshots need a running Home Assistant instance.")

    # Fetch snapshot from Home Assistant
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{HA_URL}/api/camera_proxy/{camera}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
        ) as resp:
            if resp.status != 200:
                return f"Failed to get snapshot from {camera}: {resp.status}"
            image_bytes = await resp.read()

    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    from llm_factory import build_llm
    llm = build_llm(temperature=0)
    response = await llm.ainvoke([
        SystemMessage(content="You are analysing a security-camera snapshot for the user."),
        HumanMessage(content=[
            {"type": "text", "text": question},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
        ]),
    ])
    return response.content if isinstance(response.content, str) else str(response.content)
