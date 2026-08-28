"""JARVIS Vision service.

Face recognition (InsightFace) over Ring camera snapshots — the deterministic
"is that the resident?" signal Sentry's LLM vision can't provide on its own:

  ring/+/camera/+/snapshot/image (raw JPEG from ring-mqtt)
      → detect + identify faces against enrolled embeddings (Redis face:{name})
      → Redis ring:camera:{device}:face_id  {name, score, faces, ts}
      → MQTT jarvis/vision/ring/{device}/face

Enrollment over MQTT:
  jarvis/vision/enroll_image {"name": "omar", "image_b64": "..."}
      → embed the largest face in the provided image (PRIMARY path — the
        iOS app's selfie flow posts these via the gateway /face/enroll)
  jarvis/vision/enroll {"name": "omar", "device": "<id>"}
      → same, from that Ring camera's CURRENT cached snapshot
        (bonus path for camera owners — scripts/enroll_face_ring.sh)
  jarvis/vision/enroll_finalize {"name": "omar"}
      → average samples → face:{name} (what identification compares against)

Privacy mode (sentry:privacy) drops ALL processing, same as the Ring pipeline.

Legacy mode: if CAMERAS (JSON name→RTSP/device) is set, also runs the original
YOLO object-detection loop over those feeds (YOLO loads lazily, only here).
"""

import base64
import json
import os
import time
from datetime import datetime, timezone

import cv2
import numpy as np
import paho.mqtt.client as mqtt
import redis
from insightface.app import FaceAnalysis

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))

# Legacy camera sources — RTSP URLs or device indices (empty = Ring-only mode)
CAMERAS = json.loads(os.environ.get("CAMERAS", "{}"))

DETECTION_INTERVAL = float(os.environ.get("DETECTION_INTERVAL", "2.0"))  # seconds
FACE_SIMILARITY_THRESHOLD = float(os.environ.get("FACE_SIMILARITY_THRESHOLD", "0.45"))


class VisionProcessor:
    def __init__(self):
        self.yolo = None
        if CAMERAS:
            print("[Vision] Loading YOLO model (legacy camera mode)...")
            from ultralytics import YOLO
            self.yolo = YOLO("yolo11n.pt")

        print("[Vision] Loading InsightFace...")
        self.face_app = FaceAnalysis(
            name="buffalo_l",
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        self.face_app.prepare(ctx_id=0, det_size=(640, 640))

        self.redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)
        self.known_faces = self._load_known_faces()

        self.mqtt = mqtt.Client()
        self.mqtt.on_connect = self._on_connect          # resubscribe on reconnect
        self.mqtt.message_callback_add("ring/+/camera/+/snapshot/image",
                                       self._on_ring_snapshot)
        self.mqtt.message_callback_add("jarvis/vision/enroll", self._on_enroll)
        self.mqtt.message_callback_add("jarvis/vision/enroll_image",
                                       self._on_enroll_image)
        self.mqtt.message_callback_add("jarvis/vision/enroll_finalize",
                                       self._on_enroll_finalize)
        self.mqtt.connect(MQTT_HOST, MQTT_PORT, 60)
        self.mqtt.loop_start()

        print(f"[Vision] Loaded {len(self.known_faces)} known faces")

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe("ring/+/camera/+/snapshot/image")
        client.subscribe("jarvis/vision/enroll")
        client.subscribe("jarvis/vision/enroll_image")
        client.subscribe("jarvis/vision/enroll_finalize")
        print("[Vision] MQTT connected — watching Ring snapshots")

    # ------------------------------------------------------------------
    # Ring snapshot → face identification (the Sentry accuracy signal)
    # ------------------------------------------------------------------

    def _on_ring_snapshot(self, client, userdata, msg):
        try:
            if self.redis.get("sentry:privacy"):
                return                      # privacy mode drops everything
            device = msg.topic.split("/")[3]
            frame = cv2.imdecode(np.frombuffer(msg.payload, np.uint8),
                                 cv2.IMREAD_COLOR)
            if frame is None:
                return
            faces = self.face_app.get(frame)
            best_name, best_score = "unknown", 0.0
            for face in faces:
                name, score = self._identify_face(face.embedding)
                if score > best_score:
                    best_name, best_score = name, score
            result = {
                "name": best_name if best_score >= FACE_SIMILARITY_THRESHOLD
                        else "unknown",
                "score": round(float(best_score), 3),
                "faces": len(faces),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            self.redis.set(f"ring:camera:{device}:face_id",
                           json.dumps(result), ex=600)
            self.mqtt.publish(f"jarvis/vision/ring/{device}/face",
                              json.dumps(result))
            if faces:
                print(f"[Vision] ring {device}: {result['name']} "
                      f"({result['score']}) — {len(faces)} face(s)")
        except Exception as e:
            print(f"[Vision] ring snapshot error: {e}")

    # ------------------------------------------------------------------
    # Enrollment over MQTT — each 'enroll' embeds the camera's CURRENT
    # cached snapshot; 'enroll_finalize' averages the samples into the
    # face:{name} embedding identification compares against.
    # ------------------------------------------------------------------

    def _on_enroll(self, client, userdata, msg):
        try:
            req = json.loads(msg.payload.decode())
            name = req["name"].strip().lower()
            device = req["device"]
            snap = self.redis.get(f"ring:camera:{device}:snapshot")
            if not snap:
                self._enroll_result(name, False, reason="no cached snapshot")
                return
            self._enroll_sample(name, base64.b64decode(snap))
        except Exception as e:
            print(f"[Vision] enroll error: {e}")

    def _on_enroll_image(self, client, userdata, msg):
        """Enroll from an image carried IN the message (app selfie flow)."""
        try:
            req = json.loads(msg.payload.decode())
            name = req["name"].strip().lower()
            self._enroll_sample(name, base64.b64decode(req["image_b64"]))
        except Exception as e:
            print(f"[Vision] enroll_image error: {e}")

    def _enroll_sample(self, name: str, jpeg: bytes) -> None:
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        faces = self.face_app.get(frame) if frame is not None else []
        if not faces:
            self._enroll_result(name, False, reason="no face in frame")
            return
        face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) *
                                        (f.bbox[3] - f.bbox[1]))
        self.redis.rpush(f"face_samples:{name}",
                         face.embedding.astype(np.float32).tobytes())
        n = int(self.redis.llen(f"face_samples:{name}"))
        self._enroll_result(name, True, samples=n)
        print(f"[Vision] enroll: sample {n} for '{name}'")

    def _on_enroll_finalize(self, client, userdata, msg):
        try:
            name = json.loads(msg.payload.decode())["name"].strip().lower()
            raw = self.redis.lrange(f"face_samples:{name}", 0, -1)
            if not raw:
                self._enroll_result(name, False, reason="no samples collected")
                return
            embs = [np.frombuffer(b, dtype=np.float32) for b in raw]
            avg = np.mean(embs, axis=0).astype(np.float32)
            self.redis.set(f"face:{name}", avg.tobytes())
            self.redis.delete(f"face_samples:{name}")
            self.known_faces = self._load_known_faces()
            self._enroll_result(name, True, finalized=True, samples=len(embs))
            print(f"[Vision] enrolled '{name}' from {len(embs)} samples; "
                  f"known faces: {list(self.known_faces)}")
        except Exception as e:
            print(f"[Vision] finalize error: {e}")

    def _enroll_result(self, name: str, ok: bool, **extra) -> None:
        print(f"[Vision] enroll_result: {name} ok={ok} {extra}")
        self.mqtt.publish("jarvis/vision/enroll_result",
                          json.dumps({"name": name, "ok": ok, **extra}))

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
