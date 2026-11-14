"""
Modern 9:16 (1080x1920) Multi-Template TikTok Video Renderer for GenX71 AutoVJ.
Templates:
  - 'vinyl': Spinning 12" vinyl record with concentric grooves + sleeve cover
  - 'cd': High-gloss CD jewel case with transparent spine & specular reflection
  - 'retro': Glassmorphic audiophile card with dynamic progress bar & waveform
All templates feature:
  - Ambient color-matched blurred artwork background
  - Beat-reactive spectrum equalizer bars driven by RMS audio envelope
  - Official chart peak pill badge & sleek typography within TikTok safe zones
  - Subtle Seany GenX watermark (genxlogofinal.png) positioned in safe margins
"""

import os
import math
import logging
import subprocess
from typing import List, Dict, Any, Optional
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

from core.artwork import extract_palette

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WATERMARK_PATH = os.path.join(PROJECT_ROOT, "genxlogofinal.png")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")

WIDTH = 1080
HEIGHT = 1920
FPS = 30


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _create_ambient_background(artwork_path: str, palette: Dict[str, Any]) -> Image.Image:
    """Create a 1080x1920 blurred, darkened ambient backdrop from the single artwork."""
    with Image.open(artwork_path) as art:
        art = art.convert("RGB")
        # Scale up to cover 1080x1920
        bg = art.resize((HEIGHT, HEIGHT), Image.Resampling.BILINEAR)
        left = (HEIGHT - WIDTH) // 2
        bg = bg.crop((left, 0, left + WIDTH, HEIGHT))
        bg = bg.filter(ImageFilter.GaussianBlur(radius=65))
        bg = ImageEnhance.Brightness(bg).enhance(0.35)

    # Subtle dark vignette & tint
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    bg_dark = palette["bg_dark"]
    draw.rectangle([0, 0, WIDTH, HEIGHT], fill=(bg_dark[0], bg_dark[1], bg_dark[2], 95))

    # Top and bottom gradient darkening for text readability
    for y in range(0, 400):
        alpha = int(140 * (1.0 - y / 400.0))
        draw.line([(0, y), (WIDTH, y)], fill=(8, 8, 14, alpha))
    for y in range(1200, HEIGHT):
        alpha = int(190 * ((y - 1200) / (HEIGHT - 1200)))
        draw.line([(0, y), (WIDTH, y)], fill=(8, 8, 14, alpha))

    return Image.alpha_composite(bg.convert("RGBA"), overlay)


def _create_vinyl_disc(artwork_path: str, size: int = 620) -> Image.Image:
    """Pre-render a crisp vinyl disc with grooves and center artwork label."""
    disc = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(disc)
    cx, cy = size // 2, size // 2
    r_outer = size // 2 - 4

    # Outer black vinyl body
    draw.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], fill=(16, 16, 20, 255), outline=(45, 45, 55, 255), width=3)

    # Concentric vinyl grooves
    for r in range(int(r_outer * 0.42), r_outer - 10, 5):
        shade = 28 if (r % 15 == 0) else 20
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(shade, shade, shade + 6, 180), width=1)

    # Subtle specular sheen arcs
    draw.arc([cx - r_outer + 20, cy - r_outer + 20, cx + r_outer - 20, cy + r_outer - 20],
             start=205, end=255, fill=(255, 255, 255, 35), width=18)
    draw.arc([cx - r_outer + 20, cy - r_outer + 20, cx + r_outer - 20, cy + r_outer - 20],
             start=25, end=75, fill=(255, 255, 255, 35), width=18)

    # Center artwork label
    label_r = int(size * 0.19)
    with Image.open(artwork_path) as art:
        art_label = art.convert("RGBA").resize((label_r * 2, label_r * 2), Image.Resampling.LANCZOS)
        mask = Image.new("L", (label_r * 2, label_r * 2), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, label_r * 2, label_r * 2], fill=255)
        disc.paste(art_label, (cx - label_r, cy - label_r), mask)

    # Center spindle hole
    hole_r = 12
    draw.ellipse([cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r], fill=(12, 12, 16, 255), outline=(180, 180, 190, 200), width=2)
    return disc


def _create_cd_jewel_case(artwork_path: str, size: int = 660) -> Image.Image:
    """Pre-render a 3D-style CD jewel case mockup with plastic spine and glossy reflection."""
    spine_w = 56
    total_w = size + spine_w
    case = Image.new("RGBA", (total_w, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(case)

    # Translucent dark plastic spine on left
    draw.rounded_rectangle([0, 0, spine_w + 12, size], radius=8, fill=(24, 24, 32, 235), outline=(120, 130, 150, 130), width=2)
    for y in range(24, size - 24, 14):
        draw.line([(12, y), (spine_w - 8, y)], fill=(90, 95, 115, 140), width=3)

    # Main square artwork
    with Image.open(artwork_path) as art:
        cover = art.convert("RGBA").resize((size - 16, size - 16), Image.Resampling.LANCZOS)
        case.paste(cover, (spine_w + 8, 8))

    # Glossy acrylic reflection diagonal
    gloss = Image.new("RGBA", (total_w, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(gloss)
    gdraw.polygon([
        (spine_w, 0),
        (spine_w + int(size * 0.55), 0),
        (spine_w + int(size * 0.15), size),
        (spine_w, size)
    ], fill=(255, 255, 255, 32))
    gdraw.rounded_rectangle([0, 0, total_w - 1, size - 1], radius=10, outline=(255, 255, 255, 110), width=3)

    return Image.alpha_composite(case, gloss)


def _prepare_watermark(watermark_path: str, opacity: float = 0.40, target_width: int = 190) -> Optional[Image.Image]:
    if not os.path.exists(watermark_path):
        return None
    with Image.open(watermark_path) as wm:
        wm = wm.convert("RGBA")
        ratio = target_width / wm.width
        wm = wm.resize((target_width, int(wm.height * ratio)), Image.Resampling.LANCZOS)
        # Apply opacity to alpha channel
        r, g, b, a = wm.split()
        a = a.point(lambda p: int(p * opacity))
        wm = Image.merge("RGBA", (r, g, b, a))
        return wm


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> List[str]:
    words = text.split()
    lines = []
    current = ""
    for w in words:
        test = f"{current} {w}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines[:2]


def render_tiktok_video(
    artist: str,
    title: str,
    year: int,
    chart_badge: str,
    artwork_path: str,
    audio_path: str,
    rms_envelope: List[float],
    duration: float,
    output_path: str,
    template: str = "vinyl",
    watermark_path: str = DEFAULT_WATERMARK_PATH,
    watermark_opacity: float = 0.40,
) -> str:
    """
    Render a sleek 1080x1920 9:16 MP4 video with the selected template,
    beat-reactive visualizer, chart badge, and Seany GenX safe-zone watermark.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    palette = extract_palette(artwork_path)
    accent = palette["accent"]

    # Base static canvas (background + header + text + watermark)
    base_bg = _create_ambient_background(artwork_path, palette)
    draw_base = ImageDraw.Draw(base_bg)

    font_badge = _load_font(30, bold=True)
    font_title = _load_font(56, bold=True)
    font_artist = _load_font(40, bold=False)
    font_sub = _load_font(26, bold=True)

    # 1. Chart Badge Pill at top safe zone (y=190)
    badge_text = chart_badge.upper()
    b_bbox = draw_base.textbbox((0, 0), badge_text, font=font_badge)
    bw = b_bbox[2] - b_bbox[0] + 64
    bh = 60
    bx = (WIDTH - bw) // 2
    by = 185
    draw_base.rounded_rectangle([bx, by, bx + bw, by + bh], radius=30,
                                fill=(20, 22, 32, 200), outline=(accent[0], accent[1], accent[2], 220), width=3)
    draw_base.text((WIDTH // 2, by + bh // 2), badge_text, fill=(255, 255, 255, 245), font=font_badge, anchor="mm")

    # 2. Track Title & Artist (y=1080..1260, well above TikTok bottom UI)
    title_lines = _wrap_text(title, font_title, WIDTH - 180, draw_base)
    ty = 1095
    for line in title_lines:
        draw_base.text((WIDTH // 2, ty), line, fill=(255, 255, 255, 255), font=font_title, anchor="mm")
        ty += 68

    draw_base.text((WIDTH // 2, ty + 14), artist.upper(), fill=(accent[0], accent[1], accent[2], 255), font=font_artist, anchor="mm")

    # 3. Subtle Seany GenX Watermark in TikTok Safe Zone (upper-left / top-safe or bottom-center above UI)
    wm = _prepare_watermark(watermark_path, opacity=watermark_opacity, target_width=170)
    if wm:
        # Place cleanly at top-left safe margin (x=64, y=130) or bottom-center safe (y=1470)
        # Bottom-center above TikTok caption box is super clean and hard to crop out without ruining the video
        base_bg.alpha_composite(wm, ((WIDTH - wm.width) // 2, 1415))

    # Prepare Template-specific pre-rendered assets
    vinyl_disc = None
    sleeve_img = None
    cd_case = None
    retro_art = None

    if template == "vinyl":
        vinyl_disc = _create_vinyl_disc(artwork_path, size=540)
        with Image.open(artwork_path) as art:
            sleeve_img = art.convert("RGBA").resize((540, 540), Image.Resampling.LANCZOS)
            # Add thin border to sleeve
            s_draw = ImageDraw.Draw(sleeve_img)
            s_draw.rectangle([0, 0, 539, 539], outline=(255, 255, 255, 90), width=3)
    elif template == "cd":
        cd_case = _create_cd_jewel_case(artwork_path, size=620)
    else:  # 'retro'
        with Image.open(artwork_path) as art:
            retro_art = art.convert("RGBA").resize((580, 580), Image.Resampling.LANCZOS)
            mask = Image.new("L", (580, 580), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, 580, 580], radius=36, fill=255)
            retro_art.putalpha(mask)

    total_frames = max(1, int(duration * FPS))

    # Launch FFmpeg process receiving raw RGBA frames via stdin
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgba",
        "-s", f"{WIDTH}x{HEIGHT}",
        "-r", str(FPS),
        "-i", "-",
        "-i", audio_path,
        "-c:v", "libx264",
        "-preset", "faster",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        output_path
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    n_bars = 28
    bar_width = 16
    bar_gap = 12
    eq_total_w = n_bars * bar_width + (n_bars - 1) * bar_gap
    eq_start_x = (WIDTH - eq_total_w) // 2
    eq_base_y = 1365

    try:
        for frame_idx in range(total_frames):
            frame = base_bg.copy()
            t_sec = frame_idx / FPS
            progress = frame_idx / max(1, total_frames - 1)
            energy = rms_envelope[frame_idx] if frame_idx < len(rms_envelope) else 0.3

            if template == "vinyl":
                # Rotate vinyl at ~33.3 RPM (200 deg/sec)
                angle = -(t_sec * 160.0) % 360.0
                rot_disc = vinyl_disc.rotate(angle, resample=Image.Resampling.BILINEAR)
                # Vinyl peeks out to the right of sleeve
                frame.alpha_composite(rot_disc, (440, 390))
                frame.alpha_composite(sleeve_img, (130, 390))

            elif template == "cd":
                # Subtle floating hover animation
                dy = int(math.sin(t_sec * 1.8) * 12)
                cx = (WIDTH - cd_case.width) // 2
                frame.alpha_composite(cd_case, (cx, 350 + dy))

            else:  # 'retro' audiophile card
                card_draw = ImageDraw.Draw(frame)
                card_x, card_y, card_w, card_h = 170, 320, 740, 710
                card_draw.rounded_rectangle(
                    [card_x, card_y, card_x + card_w, card_y + card_h],
                    radius=44, fill=(18, 20, 30, 185), outline=(255, 255, 255, 50), width=2
                )
                frame.alpha_composite(retro_art, ((WIDTH - 580) // 2, 360))
                # Progress bar inside card
                pb_x, pb_y, pb_w, pb_h = 250, 975, 580, 10
                card_draw.rounded_rectangle([pb_x, pb_y, pb_x + pb_w, pb_y + pb_h], radius=5, fill=(255, 255, 255, 50))
                fill_w = max(10, int(pb_w * progress))
                card_draw.rounded_rectangle([pb_x, pb_y, pb_x + fill_w, pb_y + pb_h], radius=5, fill=(accent[0], accent[1], accent[2], 255))

            # Draw Beat-Reactive Equalizer Bars
            eq_draw = ImageDraw.Draw(frame)
            for b in range(n_bars):
                # Symmetric bell curve + wave modulation
                dist_center = abs(b - (n_bars - 1) / 2.0) / (n_bars / 2.0)
                envelope_shape = max(0.25, 1.0 - dist_center * 0.7)
                wave = 0.55 + 0.45 * math.sin(t_sec * 10.0 + b * 0.7)
                bar_h = int(14 + (energy * 95 * envelope_shape * wave))
                bx0 = eq_start_x + b * (bar_width + bar_gap)
                by0 = eq_base_y - bar_h
                eq_draw.rounded_rectangle(
                    [bx0, by0, bx0 + bar_width, eq_base_y],
                    radius=6,
                    fill=(accent[0], accent[1], accent[2], 215)
                )

            proc.stdin.write(frame.tobytes())

        proc.stdin.close()
        stderr = proc.stderr.read().decode("utf-8", errors="ignore")
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f"FFmpeg failed (code {ret}): {stderr[-500:]}")
    except Exception:
        proc.kill()
        raise

    return output_path
