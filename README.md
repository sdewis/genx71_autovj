# GenX71 AutoVJ v2.0

Automated 1978–1996 GenX Chart Hit TikTok Video Studio, 24/7 Background Scheduler, and Samsung Galaxy S24 Ultra PWA.

## Features
- **GenX Era Chart Engine (`core/charts.py`)**: Curated 1978–1996 UK Official Singles & US Billboard catalog with **✨ Load New Items** hybrid batch loading (fetches live 1978–1996 UK/US chart singles from Wikipedia with an expanded curated offline fallback pool, pushing previous pages back from Page 1 → Page 2 → Page 3…), configurable **Wildcard (Top 21–40)** rarity weighting (default 25%), and permanent SQLite deduplication (`core/db.py`).
- **Intelligent Audio Clipper & FFT Analyzer (`core/audio.py`)**: Prioritizes full tracks via `yt-dlp` (with Deezer/iTunes preview fallback), boundary-clips clean 35–45s hooks (`>=30s`, `<60s` TikTok limit), and extracts 24-band log-spaced FFT spectrum + bass/kick beat onset impulses.
- **High-Res Single Artwork (`core/artwork.py`)**: Fetches authentic Vinyl/CD single sleeves and extracts dominant/accent color palettes to drive each style's lighting, gradients, and color blocks.
- **9:16 Chromium Style Pack Renderer (`core/browser_renderer.py`, `core/studio_template.html`, `core/styles.py`)**: Deterministic, zero-AI-token 1080×1920 MP4 renderer with **Era-Weighted Auto-Rotate** (never repeating the last 2 styles) and automatic Pillow fallback (`core/video_renderer.py`):
  - `hifi`: **Hi-Fi Audiophile** — 3D spinning vinyl + tonearm (pre-1988) or 3D glossy CD jewel case (1988+)
  - `vhs`: **VHS / MTV Broadcast** — CRT scanlines, tracking lock intro, chromatic kick hits & MTV lower-third
  - `neon`: **Neon Tubes (80s Synth)** — Synthwave perspective grid & glowing dual neon frame
  - `riso`: **Risograph Zine Poster** — Halftone paper texture, taped print & rubber-stamp badge
  - `kinetic`: **Kinetic Color-Block** — Bold palette fields & giant cropped parallax typography
  - `cassette`: **Cassette / Boombox Deck** — Dual spinning reels & backlit amber analog VU meters
  - All styles feature real 24-band FFT visualizers, 3 fading track trivia cards, and your transparent glowing **Seany GenX** neon station bug (`assets/genx_neon_logo.png`).
- **Regional Voice Hook TTS (`core/tts.py`, `core/hooks.py`, `tts_worker/`)**: CPU-only zero-shot voice cloning sidecar (Coqui XTTS-v2) supporting **UK** & **US** voice profiles (auto-routed by track chart region, with optional per-upload background noise cleanup), 30+ era-specific upbeat GenX DJ hook templates, **"talk up the post"** radio-ramp audio mixing (targeting ~58s total), live `ON AIR` captions, and async `TTS pending` queue statuses in the Web GUI.
- **Stealth TikTok Poster (`core/tiktok_poster.py`)**: Playwright + `playwright-stealth` using your browser `sessionid` cookies, human-like typing/delays, and Drafts or Direct Publish support.
- **24/7 Daemon with Jitter (`daemon.py`)**: `APScheduler` service with randomized Gaussian time fluctuation (`+/-` configurable minutes) to avoid bot-like posting intervals.
- **Samsung S24 Ultra PWA & Linux UI (`web/`, `api/app.py`)**: Mobile-first AMOLED dark interface with Studio preview, drag-and-drop Scheduled Queue, History management (including **Delete Render** to remove an MP4 and unlock a track for re-rendering), paginated Charts catalog with **✨ Load New Items**, and Admin settings.

## Quick Start

### Option 1: Docker (Recommended)

```bash
./start.sh            # Checks whether genx71-autovj or tts-worker need rebuilding, builds only what changed, and starts/restarts the stack
./start.sh --check    # Dry-run check only (no build or restart)
./start.sh --pull     # git pull, rebuild if needed, and restart
```

Or directly via Compose:

```bash
docker compose up -d --build
```

### Option 2: Local Python Environment

```bash
./run.sh
```

Open `http://localhost:8710` on Linux or `http://<your-linux-ip>:8710` on your Samsung Galaxy S24 Ultra (tap **Add to Home Screen / Install App** for standalone mode).

For full installation, Docker, Tailscale, TikTok cookie setup, and usage instructions, see the **[GenX71 AutoVJ Wiki](https://github.com/sdewis/genx71_autovj/wiki)**.

## Running Tests

```bash
source .venv/bin/activate
PYTHONPATH=. pytest -v
```
