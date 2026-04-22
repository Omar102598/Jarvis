FROM python:3.11-slim

WORKDIR /app

# System deps:
#   ffmpeg        — audio format conversion (M4A → WAV, etc.) for faster-whisper
#   espeak-ng     — phoneme backend required by piper-tts
#   libsndfile1   — soundfile I/O used by piper-tts
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        espeak-ng \
        libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8080

CMD ["python", "-u", "main.py"]
