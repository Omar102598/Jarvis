"""JARVIS Glasses Bridge — Brilliant Labs Frame integration.

Connects to the Frame smart glasses via Bluetooth and bridges
camera capture, display output, and microphone data to JARVIS
via MQTT.
"""

import asyncio
import base64
import json
import os

import paho.mqtt.client as mqtt
from frame_sdk import Frame
from frame_sdk.display import Alignment

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
FRAME_AUTO_CONNECT = os.environ.get("FRAME_AUTO_CONNECT", "true").lower() == "true"


class GlassesBridge:
    def __init__(self):
        self.frame = None
        self.mqtt = mqtt.Client()
        self.mqtt.on_connect = self._on_mqtt_connect
        self.mqtt.on_message = self._on_mqtt_message
        self.mqtt.connect(MQTT_HOST, MQTT_PORT, 60)
        self.mqtt.loop_start()

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        print(f"[Glasses] MQTT connected (rc={rc})")
        client.subscribe("jarvis/glasses/display")
        client.subscribe("jarvis/glasses/capture")
        client.subscribe("jarvis/glasses/command")

    def _on_mqtt_message(self, client, userdata, msg):
        """Handle MQTT commands for the glasses."""
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}

        if topic == "jarvis/glasses/display":
            text = payload.get("text", "")
            if text and self.frame:
                asyncio.run_coroutine_threadsafe(
                    self._display_text(text), self._loop
                )

        elif topic == "jarvis/glasses/capture":
            if self.frame:
                asyncio.run_coroutine_threadsafe(
                    self._capture_photo(), self._loop
                )

    async def _display_text(self, text: str):
        """Display text on the Frame's OLED."""
        try:
            await self.frame.display.show_text(
                text,
                align=Alignment.MIDDLE_CENTER,
            )
            print(f"[Glasses] Displayed: {text[:50]}...")
        except Exception as e:
            print(f"[Glasses] Display error: {e}")

    async def _capture_photo(self):
        """Capture a photo from the Frame's camera and publish via MQTT."""
        try:
            photo = await self.frame.camera.take_photo()
            b64 = base64.b64encode(photo).decode("utf-8")
            self.mqtt.publish(
                "jarvis/glasses/photo",
                json.dumps({
                    "source": "frame_glasses",
                    "image_b64": b64,
                }),
            )
            print(f"[Glasses] Captured photo ({len(photo)} bytes)")
        except Exception as e:
            print(f"[Glasses] Capture error: {e}")

    async def connect_and_run(self):
        """Connect to Frame glasses and maintain connection."""
        self._loop = asyncio.get_event_loop()

        while True:
            try:
                print("[Glasses] Searching for Frame glasses...")
                async with Frame() as frame:
                    self.frame = frame

                    battery = await frame.get_battery_level()
                    print(f"[Glasses] Connected! Battery: {battery}%")

                    self.mqtt.publish(
                        "jarvis/glasses/status",
                        json.dumps({
                            "connected": True,
                            "battery": battery,
                        }),
                    )

                    # Show connection confirmation
                    await frame.display.show_text(
                        "JARVIS Online",
                        align=Alignment.MIDDLE_CENTER,
                    )

                    # Keep connection alive
                    while True:
                        await asyncio.sleep(30)
                        try:
                            bat = await frame.get_battery_level()
                            self.mqtt.publish(
                                "jarvis/glasses/status",
                                json.dumps({"connected": True, "battery": bat}),
                            )
                        except Exception:
                            print("[Glasses] Connection lost")
                            break

            except Exception as e:
                print(f"[Glasses] Connection failed: {e}")
                self.frame = None
                self.mqtt.publish(
                    "jarvis/glasses/status",
                    json.dumps({"connected": False}),
                )
                print("[Glasses] Retrying in 10 seconds...")
                await asyncio.sleep(10)


def main():
    print("[Glasses] Starting JARVIS Glasses Bridge...")
    bridge = GlassesBridge()
    asyncio.run(bridge.connect_and_run())


if __name__ == "__main__":
    main()
