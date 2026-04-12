"""JARVIS Vision service.

Processes camera feeds for object detection (YOLO) and
face recognition (InsightFace). Publishes events via MQTT.
"""

import json
import os
import time

import cv2
import numpy as np
import paho.mqtt.client as mqtt
import redis
from insightface.app import FaceAnalysis
from ultralytics import YOLO

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))

# Camera sources — RTSP URLs or device indices
CAMERAS = json.loads(os.environ.get("CAMERAS", '{"front_door": "rtsp://cameras.local/front"}'))

DETECTION_INTERVAL = float(os.environ.get("DETECTION_INTERVAL", "2.0"))  # seconds
FACE_SIMILARITY_THRESHOLD = float(os.environ.get("FACE_SIMILARITY_THRESHOLD", "0.45"))


class VisionProcessor:
    def __init__(self):
        print("[Vision] Loading YOLO model...")
        self.yolo = YOLO("yolo11n.pt")  # swap to yolo26m when available

        print("[Vision] Loading InsightFace...")
        self.face_app = FaceAnalysis(
            name="buffalo_l",
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        self.face_app.prepare(ctx_id=0, det_size=(640, 640))

        self.redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
        self.known_faces = self._load_known_faces()

        self.mqtt = mqtt.Client()
        self.mqtt.connect(MQTT_HOST, MQTT_PORT, 60)
        self.mqtt.loop_start()

        print(f"[Vision] Loaded {len(self.known_faces)} known faces")

    def _load_known_faces(self) -> dict:
        """Load enrolled face embeddings from Redis."""
        faces = {}
        for key in self.redis.scan_iter("face:*"):
            name = key.decode().split(":", 1)[1]
            embedding = np.frombuffer(self.redis.get(key), dtype=np.float32)
            faces[name] = embedding
        return faces

    def _identify_face(self, embedding: np.ndarray) -> tuple[str, float]:
        """Compare face embedding against known faces."""
        best_name = "unknown"
        best_score = 0.0

        for name, known_emb in self.known_faces.items():
            score = np.dot(embedding, known_emb) / (
                np.linalg.norm(embedding) * np.linalg.norm(known_emb)
            )
            if score > best_score:
                best_score = score
                best_name = name

        if best_score < FACE_SIMILARITY_THRESHOLD:
            return "unknown", best_score
        return best_name, best_score

    def process_frame(self, camera_name: str, frame: np.ndarray):
        """Run detection + recognition on a single frame."""
        # Object detection
        results = self.yolo(frame, verbose=False)
        detections = []
        person_crops = []

        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                cls_name = r.names[cls_id]
                conf = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])

                detections.append({
                    "class": cls_name,
                    "confidence": round(conf, 2),
                    "bbox": [x1, y1, x2, y2],
                })

                if cls_name == "person" and conf > 0.5:
                    person_crops.append(frame[y1:y2, x1:x2])

        # Publish object detections
        if detections:
            self.mqtt.publish(
                f"jarvis/vision/{camera_name}/detections",
                json.dumps({"camera": camera_name, "objects": detections}),
            )

        # Face recognition on person crops
        for crop in person_crops:
            if crop.size == 0:
                continue
            faces = self.face_app.get(crop)
            for face in faces:
                name, score = self._identify_face(face.embedding)
                self.mqtt.publish(
                    f"jarvis/vision/{camera_name}/face",
                    json.dumps({
                        "camera": camera_name,
                        "person": name,
                        "confidence": round(float(score), 3),
                    }),
                )
                if name != "unknown":
                    print(f"[Vision] {camera_name}: Recognized {name} ({score:.2f})")

    def run(self):
        """Main loop — read from all cameras and process."""
        caps = {}
        for name, source in CAMERAS.items():
            cap = cv2.VideoCapture(source)
            if cap.isOpened():
                caps[name] = cap
                print(f"[Vision] Opened camera: {name} ({source})")
            else:
                print(f"[Vision] Failed to open: {name} ({source})")

        if not caps:
            print("[Vision] No cameras available. Waiting for MQTT commands...")
            self.mqtt.subscribe("jarvis/vision/analyze")
            self.mqtt.on_message = self._on_mqtt_analyze
            self.mqtt.loop_forever()
            return

        print(f"[Vision] Processing {len(caps)} camera(s) every {DETECTION_INTERVAL}s")

        while True:
            for name, cap in caps.items():
                ret, frame = cap.read()
                if not ret:
                    print(f"[Vision] Failed to read from {name}, reconnecting...")
                    cap.release()
                    caps[name] = cv2.VideoCapture(CAMERAS[name])
                    continue

                self.process_frame(name, frame)

            time.sleep(DETECTION_INTERVAL)

    def _on_mqtt_analyze(self, client, userdata, msg):
        """Handle on-demand frame analysis via MQTT."""
        try:
            payload = json.loads(msg.payload.decode())
            # Expect base64 or file path
            print(f"[Vision] On-demand analysis request: {payload.get('source', 'unknown')}")
        except Exception as e:
            print(f"[Vision] MQTT analysis error: {e}")


def main():
    print("[Vision] Starting JARVIS Vision service...")
    processor = VisionProcessor()
    processor.run()


if __name__ == "__main__":
    main()
