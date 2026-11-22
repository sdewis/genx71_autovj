FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COQUI_TOS_AGREED=1 \
    TTS_HOST=0.0.0.0 \
    TTS_PORT=8711 \
    OMP_NUM_THREADS=4

WORKDIR /tts

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    espeak-ng \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install CPU-only PyTorch first to keep image size reasonable without CUDA bloat
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
       torch torchaudio

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN mkdir -p /app/data/voices/uk /app/data/voices/us /app/cache/tts

EXPOSE 8711

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"TTS_PORT\", \"8711\")}/health', timeout=5)" || exit 1

CMD ["sh", "-c", "exec uvicorn server:app --host ${TTS_HOST} --port ${TTS_PORT}"]
