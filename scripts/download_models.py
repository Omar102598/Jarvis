#!/usr/bin/env python3
"""Download ML models required by JARVIS services.

This script pre-downloads models to the local `models/` directory so that
Docker containers don't need to download them on first start.

Usage:
    python scripts/download_models.py [--all | --stt | --speaker | --tts | --vision]
"""

import argparse
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(PROJECT_DIR, "models")


def download_stt_model():
    """Download faster-whisper turbo model."""
    print("\n=== Downloading STT Model (faster-whisper turbo) ===")
    try:
        from faster_whisper import WhisperModel

        model_dir = os.path.join(MODELS_DIR, "faster-whisper-turbo")
        os.makedirs(model_dir, exist_ok=True)
        print("  Downloading 'turbo' model (this may take a few minutes)...")
        WhisperModel("turbo", device="cpu", compute_type="int8",
                      download_root=model_dir)
        print("  ✓ STT model downloaded")
    except ImportError:
        print("  ✗ faster-whisper not installed. Install with: pip install faster-whisper")
    except Exception as e:
        print(f"  ✗ Error downloading STT model: {e}")


def download_speaker_model():
    """Download SpeechBrain ECAPA-TDNN speaker verification model."""
    print("\n=== Downloading Speaker Verification Model (SpeechBrain ECAPA-TDNN) ===")
    try:
        from speechbrain.inference.speaker import EncoderClassifier

        model_dir = os.path.join(MODELS_DIR, "speaker_model")
        os.makedirs(model_dir, exist_ok=True)
        print("  Downloading ECAPA-TDNN model...")
        EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=model_dir,
        )
        print("  ✓ Speaker verification model downloaded")
    except ImportError:
        print("  ✗ SpeechBrain not installed. Install with: pip install speechbrain")
    except Exception as e:
        print(f"  ✗ Error downloading speaker model: {e}")


def download_tts_model():
    """Download Piper TTS model."""
    print("\n=== Downloading TTS Model (Piper) ===")
    model_name = "en_GB-alan-medium"
    model_dir = os.path.join(MODELS_DIR, "piper")
    os.makedirs(model_dir, exist_ok=True)

    try:
        import urllib.request

        base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
        lang = model_name.split("-")[0]

        onnx_url = f"{base_url}/{lang}/{model_name}/{model_name}.onnx"
        json_url = f"{base_url}/{lang}/{model_name}/{model_name}.onnx.json"

        onnx_path = os.path.join(model_dir, f"{model_name}.onnx")
        json_path = os.path.join(model_dir, f"{model_name}.onnx.json")

        if not os.path.exists(onnx_path):
            print(f"  Downloading {model_name}.onnx...")
            urllib.request.urlretrieve(onnx_url, onnx_path)
        else:
            print(f"  • {model_name}.onnx already exists")

        if not os.path.exists(json_path):
            print(f"  Downloading {model_name}.onnx.json...")
            urllib.request.urlretrieve(json_url, json_path)
        else:
            print(f"  • {model_name}.onnx.json already exists")

        print("  ✓ Piper TTS model downloaded")
    except Exception as e:
        print(f"  ✗ Error downloading TTS model: {e}")


def download_vision_models():
    """Download YOLO and InsightFace models."""
    print("\n=== Downloading Vision Models ===")

    # YOLO
    try:
        from ultralytics import YOLO

        model_path = os.path.join(MODELS_DIR, "yolo11n.pt")
        if not os.path.exists(model_path):
            print("  Downloading YOLO11n model...")
            model = YOLO("yolo11n.pt")
            # Move to models dir
            src = "yolo11n.pt"
            if os.path.exists(src):
                os.rename(src, model_path)
            print("  ✓ YOLO model downloaded")
        else:
            print("  • YOLO model already exists")
    except ImportError:
        print("  ✗ Ultralytics not installed. Install with: pip install ultralytics")
    except Exception as e:
        print(f"  ✗ Error downloading YOLO model: {e}")

    # InsightFace
    try:
        from insightface.app import FaceAnalysis

        print("  Downloading InsightFace Buffalo_L model...")
        app = FaceAnalysis(
            name="buffalo_l",
            root=os.path.join(MODELS_DIR, "insightface"),
            providers=["CPUExecutionProvider"],
        )
        app.prepare(ctx_id=-1, det_size=(640, 640))
        print("  ✓ InsightFace model downloaded")
    except ImportError:
        print("  ✗ InsightFace not installed. Install with: pip install insightface onnxruntime")
    except Exception as e:
        print(f"  ✗ Error downloading InsightFace model: {e}")


def download_wake_word_model():
    """Download OpenWakeWord model."""
    print("\n=== Downloading Wake Word Model (OpenWakeWord) ===")
    try:
        from openwakeword.model import Model

        print("  Downloading 'hey jarvis' model...")
        Model(wakeword_models=["hey jarvis"], inference_framework="onnx")
        print("  ✓ Wake word model downloaded")
    except ImportError:
        print("  ✗ OpenWakeWord not installed. Install with: pip install openwakeword")
    except Exception as e:
        print(f"  ✗ Error downloading wake word model: {e}")


def main():
    parser = argparse.ArgumentParser(description="Download JARVIS ML models")
    parser.add_argument("--all", action="store_true", help="Download all models")
    parser.add_argument("--stt", action="store_true", help="Download STT model only")
    parser.add_argument("--speaker", action="store_true", help="Download speaker model only")
    parser.add_argument("--tts", action="store_true", help="Download TTS model only")
    parser.add_argument("--vision", action="store_true", help="Download vision models only")
    parser.add_argument("--wake", action="store_true", help="Download wake word model only")
    args = parser.parse_args()

    os.makedirs(MODELS_DIR, exist_ok=True)

    # If no flags, download all
    download_all = args.all or not any([args.stt, args.speaker, args.tts, args.vision, args.wake])

    print("=== JARVIS Model Download ===")
    print(f"Models directory: {MODELS_DIR}")

    if download_all or args.wake:
        download_wake_word_model()
    if download_all or args.stt:
        download_stt_model()
    if download_all or args.speaker:
        download_speaker_model()
    if download_all or args.tts:
        download_tts_model()
    if download_all or args.vision:
        download_vision_models()

    print("\n=== Download Complete ===")


if __name__ == "__main__":
    main()
