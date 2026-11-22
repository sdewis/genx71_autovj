"""
Headless Chromium Style Pack Video Renderer for GenX71 AutoVJ.

Uses the deterministic `window.render(t)` browser capture pattern (inspired by
lemo-opuscar & bang-motion) on top of Playwright Chromium + FFmpeg:
  - 100% deterministic, zero AI tokens at runtime
  - Captures 1080x1920 frames via CDP Page.captureScreenshot (JPEG quality=94)
    piped directly into FFmpeg H.264/AAC for fast CPU rendering
  - Automatically uses Playwright's bundled Chromium (Docker) or system Chrome
"""

import os
import base64
import shutil
import logging
import mimetypes
import subprocess
from typing import Any, Dict, List, Optional

from core.artwork import extract_palette
from core.styles import resolve_style_and_variant

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIO_HTML_PATH = os.path.join(PROJECT_ROOT, "core", "studio_template.html")
DEFAULT_NEON_LOGO_PATH = os.path.join(PROJECT_ROOT, "assets", "genx_neon_logo.png")
FALLBACK_WATERMARK_PATH = os.path.join(PROJECT_ROOT, "genxlogofinal.png")

WIDTH = 1080
HEIGHT = 1920
FPS = 30


def _file_to_data_url(path: Optional[str]) -> Optional[str]:
    if not path or not os.path.exists(path):
        return None
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _resolve_logo_path(watermark_path: Optional[str]) -> Optional[str]:
    # Prefer the transparent neon sign cutout when using the default logo
    if watermark_path and os.path.exists(watermark_path):
        if os.path.abspath(watermark_path) == os.path.abspath(FALLBACK_WATERMARK_PATH) and os.path.exists(DEFAULT_NEON_LOGO_PATH):
            return DEFAULT_NEON_LOGO_PATH
        return watermark_path
    if os.path.exists(DEFAULT_NEON_LOGO_PATH):
        return DEFAULT_NEON_LOGO_PATH
    if os.path.exists(FALLBACK_WATERMARK_PATH):
        return FALLBACK_WATERMARK_PATH
    return None


def _launch_browser(p):
    """Launch headless Chromium, using Playwright's bundled binary or system Chrome fallback."""
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu-sandbox",
        "--headless=new",
    ]
    try:
        return p.chromium.launch(headless=True, args=launch_args)
    except Exception as first_err:
        for candidate in ("google-chrome", "chromium", "chromium-browser"):
            exe = shutil.which(candidate)
            if exe:
                logger.info(f"Using system browser executable for rendering: {exe}")
                return p.chromium.launch(headless=True, executable_path=exe, args=launch_args)
        raise first_err


def _build_scene_payload(
    artist: str,
    title: str,
    year: int,
    chart_badge: str,
    artwork_path: str,
    duration: float,
    template: str,
    rms_envelope: List[float],
    audio_features: Optional[Dict[str, Any]],
    fact_schedule: List[Dict[str, Any]],
    watermark_path: Optional[str],
    watermark_opacity: float,
    hook_overlay: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    style_id, media_variant = resolve_style_and_variant(template, year)
    palette = extract_palette(artwork_path)

    if audio_features and isinstance(audio_features, dict) and audio_features.get("bands"):
        audio_payload = audio_features
    else:
        n = max(1, len(rms_envelope))
        audio_payload = {
            "fps": FPS,
            "n_bands": 24,
            "rms": rms_envelope or [0.3] * max(1, int(duration * FPS)),
            "bass": rms_envelope or [0.3] * max(1, int(duration * FPS)),
            "onset": [0.0] * n,
            "bands": [[v] * 24 for v in (rms_envelope or [0.3] * n)],
        }

    logo_path = _resolve_logo_path(watermark_path)

    return {
        "style": style_id,
        "media_variant": media_variant,
        "artist": artist,
        "title": title,
        "year": int(year),
        "chart_badge": chart_badge,
        "duration": float(duration),
        "palette": palette,
        "artwork_data_url": _file_to_data_url(artwork_path),
        "logo_data_url": _file_to_data_url(logo_path),
        "watermark_opacity": float(watermark_opacity),
        "fact_schedule": fact_schedule,
        "audio": audio_payload,
        "hook": hook_overlay if isinstance(hook_overlay, dict) and hook_overlay.get("text") else None,
    }


def render_style_video(
    artist: str,
    title: str,
    year: int,
    chart_badge: str,
    artwork_path: str,
    audio_path: str,
    rms_envelope: List[float],
    duration: float,
    output_path: str,
    template: str = "hifi",
    watermark_path: Optional[str] = None,
    watermark_opacity: float = 0.65,
    fact_schedule: Optional[List[Dict[str, Any]]] = None,
    audio_features: Optional[Dict[str, Any]] = None,
    hook_overlay: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Render a 1080x1920 9:16 MP4 video using Headless Chromium + studio_template.html + FFmpeg.
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    payload = _build_scene_payload(
        artist=artist,
        title=title,
        year=year,
        chart_badge=chart_badge,
        artwork_path=artwork_path,
        duration=duration,
        template=template,
        rms_envelope=rms_envelope,
        audio_features=audio_features,
        fact_schedule=fact_schedule or [],
        watermark_path=watermark_path,
        watermark_opacity=watermark_opacity,
        hook_overlay=hook_overlay,
    )

    total_frames = max(1, int(duration * FPS))

    cmd = [
        "ffmpeg", "-y",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-r", str(FPS),
        "-i", "-",
        "-i", audio_path,
        "-c:v", "libx264",
        "-preset", "faster",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
        page = context.new_page()
        cdp = context.new_cdp_session(page)

        page.goto(f"file://{STUDIO_HTML_PATH}", wait_until="load")
        page.evaluate("async (data) => { await window.initScene(data); }", payload)

        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for frame_idx in range(total_frames):
                t_sec = frame_idx / FPS
                page.evaluate("(t) => window.render(t)", t_sec)
                shot = cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 93})
                proc.stdin.write(base64.b64decode(shot["data"]))

            proc.stdin.close()
            stderr = proc.stderr.read().decode("utf-8", errors="ignore")
            ret = proc.wait()
            if ret != 0:
                raise RuntimeError(f"FFmpeg failed (code {ret}): {stderr[-500:]}")
        except Exception:
            proc.kill()
            raise
        finally:
            browser.close()

    return output_path


def render_style_preview_frame(
    artist: str,
    title: str,
    year: int,
    chart_badge: str,
    artwork_path: str,
    duration: float,
    output_png_path: str,
    t_sec: float = 5.0,
    template: str = "hifi",
    rms_envelope: Optional[List[float]] = None,
    audio_features: Optional[Dict[str, Any]] = None,
    fact_schedule: Optional[List[Dict[str, Any]]] = None,
    watermark_path: Optional[str] = None,
    watermark_opacity: float = 0.65,
) -> str:
    """Render a single 1080x1920 PNG still frame at time t_sec for visual QC."""
    from playwright.sync_api import sync_playwright

    os.makedirs(os.path.dirname(os.path.abspath(output_png_path)), exist_ok=True)
    payload = _build_scene_payload(
        artist=artist,
        title=title,
        year=year,
        chart_badge=chart_badge,
        artwork_path=artwork_path,
        duration=duration,
        template=template,
        rms_envelope=rms_envelope or [0.65] * max(1, int(duration * FPS)),
        audio_features=audio_features,
        fact_schedule=fact_schedule or [],
        watermark_path=watermark_path,
        watermark_opacity=watermark_opacity,
    )

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
        page = context.new_page()
        page.goto(f"file://{STUDIO_HTML_PATH}", wait_until="load")
        page.evaluate("async (data) => { await window.initScene(data); }", payload)
        page.evaluate("(t) => window.render(t)", float(t_sec))
        page.screenshot(path=output_png_path, type="png")
        browser.close()

    return output_png_path
