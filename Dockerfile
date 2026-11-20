FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8710

WORKDIR /app

# Install ffmpeg (audio/video processing) and fonts required by Pillow renderer
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser and its OS dependencies
RUN playwright install --with-deps chromium

# Copy application source
COPY . .

# Ensure persistent runtime directories exist
RUN mkdir -p /app/data /app/output /app/cache/audio /app/cache/artwork

EXPOSE 8710

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8710\")}/api/status', timeout=5)" || exit 1

CMD ["sh", "-c", "exec uvicorn api.app:app --host ${HOST} --port ${PORT}"]
