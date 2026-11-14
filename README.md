# GenX71 AutoVJ v2.0

Automated 1978–1996 GenX Chart Hit TikTok Video Studio, 24/7 Background Scheduler, and Samsung Galaxy S24 Ultra PWA.

## Features
- **GenX Era Chart Engine (`core/charts.py`)**: Curated 1978–1996 UK Official Singles & US Billboard catalog with configurable **Wildcard (Top 21–40)** rarity weighting (default 25%) and permanent SQLite deduplication (`core/db.py`).
- **Intelligent Audio Clipper (`core/audio.py`)**: Sources 30s studio previews (Deezer / iTunes, with `yt-dlp` fallback) and snaps boundaries using RMS energy & onset analysis so clips start cleanly and fade out smoothly.
- **High-Res Single Artwork (`core/artwork.py`)**: Fetches authentic Vinyl/CD single sleeves and extracts dominant/accent color palettes for ambient backdrops.
- **9:16 Multi-Template Renderer (`core/video_renderer.py`)**: Renders 1080x1920 MP4s with:
  - `vinyl`: Spinning 12" vinyl record + sleeve cover
  - `cd`: 3D CD Jewel Case with glossy reflection
  - `retro`: Glassmorphic Audiophile Card with progress bar
  - Beat-reactive spectrum visualizer, chart peak badge, and TikTok safe-zone **Seany GenX** watermark (`genxlogofinal.png`).
- **Stealth TikTok Poster (`core/tiktok_poster.py`)**: Playwright + `playwright-stealth` using your browser `sessionid` cookies, human-like typing/delays, and Drafts or Direct Publish support.
- **24/7 Daemon with Jitter (`daemon.py`)**: `APScheduler` service with randomized Gaussian time fluctuation (`+/-` configurable minutes) to avoid bot-like posting intervals.
- **Samsung S24 Ultra PWA & Linux UI (`web/`, `api/app.py`)**: Mobile-first AMOLED dark interface installable as a standalone app on your S24 Ultra.

## Quick Start

```bash
./run.sh
```

Open `http://localhost:8710` on Linux or `http://<your-linux-ip>:8710` on your Samsung Galaxy S24 Ultra (tap **Add to Home Screen / Install App** for standalone mode).

## Running Tests

```bash
source .venv/bin/activate
PYTHONPATH=. pytest -v
```
