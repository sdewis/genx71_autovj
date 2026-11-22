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
import random
import logging
import subprocess
from typing import List, Dict, Any, Optional, Tuple
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

# Fact card layout (safely below EQ bars at y=1330 and above TikTok caption UI at y=1560+)
FACT_CARD_W = 920
FACT_CARD_H = 192
FACT_CARD_X = (WIDTH - FACT_CARD_W) // 2
FACT_CARD_Y = 1352

# Animated watermark lifecycle (seconds)
WM_SPIN_IN_SEC = 0.8
WM_HOLD_SEC = 2.5
WM_FADE_OUT_SEC = 3.0
WM_PAUSE_SEC = 0.7
WM_CYCLE_SEC = WM_SPIN_IN_SEC + WM_HOLD_SEC + WM_FADE_OUT_SEC + WM_PAUSE_SEC


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


def _prepare_watermark(watermark_path: str, opacity: float = 1.0, target_width: int = 170) -> Optional[Image.Image]:
    if not os.path.exists(watermark_path):
        return None
    with Image.open(watermark_path) as wm:
        wm = wm.convert("RGBA")
        ratio = target_width / wm.width
        wm = wm.resize((target_width, max(1, int(wm.height * ratio))), Image.Resampling.LANCZOS)
        if opacity < 1.0:
            r, g, b, a = wm.split()
            a = a.point(lambda p: int(p * opacity))
            wm = Image.merge("RGBA", (r, g, b, a))
        return wm


def _scale_rgba_alpha(img: Image.Image, alpha_factor: float) -> Image.Image:
    """Return a copy of an RGBA image with its alpha channel scaled by alpha_factor [0..1]."""
    if alpha_factor >= 0.999:
        return img
    if alpha_factor <= 0.001:
        return Image.new("RGBA", img.size, (0, 0, 0, 0))
    r, g, b, a = img.split()
    a = a.point(lambda p: int(p * alpha_factor))
    return Image.merge("RGBA", (r, g, b, a))


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw, max_lines: int = 2) -> List[str]:
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
    return lines[:max_lines]


def compute_fact_schedule(
    facts: Optional[List[str]],
    duration: float,
    start_margin: float = 2.0,
    end_margin: float = 2.0,
    fade_sec: float = 0.75,
    gap_sec: float = 0.30,
) -> List[Dict[str, Any]]:
    """
    Divide [start_margin, duration - end_margin] evenly across up to 3 facts,
    so fact 1 begins fading in at 2s after start and fact 3 finishes fading out 2s before end.
    """
    if not facts:
        return []
    clean_facts = [f.strip() for f in facts if isinstance(f, str) and f.strip()][:3]
    if not clean_facts:
        return []

    win_start = start_margin
    win_end = duration - end_margin
    total_win = win_end - win_start
    if total_win <= 0.6:
        return []

    n = len(clean_facts)
    eff_gap = gap_sec if total_win > (n * 2.0) else 0.0
    slot_dur = (total_win - eff_gap * (n - 1)) / n
    eff_fade = min(fade_sec, slot_dur * 0.25)

    schedule: List[Dict[str, Any]] = []
    for i, text in enumerate(clean_facts):
        s = win_start + i * (slot_dur + eff_gap)
        e = s + slot_dur
        schedule.append({
            "index": i,
            "total": n,
            "text": text,
            "start": round(s, 3),
            "end": round(e, 3),
            "fade": round(eff_fade, 3),
        })
    return schedule


def _smoothstep(u: float) -> float:
    u = max(0.0, min(1.0, u))
    return u * u * (3.0 - 2.0 * u)


def get_fact_alpha(slot: Dict[str, Any], t_sec: float) -> float:
    """Compute smooth fade-in / hold / fade-out alpha [0..1] for a fact slot at time t_sec."""
    s = slot["start"]
    e = slot["end"]
    f = slot["fade"]
    if t_sec <= s or t_sec >= e:
        return 0.0
    if f > 0.0 and t_sec < s + f:
        return _smoothstep((t_sec - s) / f)
    if f > 0.0 and t_sec > e - f:
        return _smoothstep((e - t_sec) / f)
    return 1.0


def _render_fact_card(fact_text: str, index: int, total: int, accent: Tuple[int, int, int]) -> Image.Image:
    """Pre-render a sleek glassmorphic fact card for compositing below the equalizer."""
    card = Image.new("RGBA", (FACT_CARD_W, FACT_CARD_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    # Glassmorphic dark container with subtle accent outline
    draw.rounded_rectangle(
        [0, 0, FACT_CARD_W - 1, FACT_CARD_H - 1],
        radius=24,
        fill=(14, 16, 26, 205),
        outline=(accent[0], accent[1], accent[2], 185),
        width=2,
    )

    font_label = _load_font(24, bold=True)
    font_fact = _load_font(34, bold=False)

    label_text = "FACTS"
    draw.text((FACT_CARD_W // 2, 28), label_text, fill=(accent[0], accent[1], accent[2], 245), font=font_label, anchor="mm")

    lines = _wrap_text(fact_text, font_fact, FACT_CARD_W - 60, draw, max_lines=3)
    line_h = 44
    block_h = len(lines) * line_h
    start_y = 50 + max(0, (FACT_CARD_H - 58 - block_h) // 2) + line_h // 2
    for i, line in enumerate(lines):
        draw.text((FACT_CARD_W // 2, start_y + i * line_h), line, fill=(248, 248, 255, 250), font=font_fact, anchor="mm")

    return card


def _rects_overlap(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def generate_watermark_schedule(
    duration: float,
    wm_width: int,
    wm_height: int,
    rng: Optional[random.Random] = None,
) -> List[Dict[str, Any]]:
    """
    Generate random TikTok-safe watermark placements across the clip.
    Each placement spins & fades in (0.8s), holds (2.5s), fades out over 3.0s, and pauses (0.7s).
    Avoids chart badge, fact card, right-side TikTok icons, and bottom caption UI,
    and enforces a minimum jump distance between consecutive placements.
    """
    if duration <= 0.1:
        return []
    gen = rng if rng is not None else random.Random()

    # Safe bounding box for the watermark's top-left corner
    min_x = 60
    max_x = max(min_x, min(840 - wm_width, WIDTH - wm_width - 180))
    min_y = 160
    max_y = max(min_y, FACT_CARD_Y - wm_height - 25)

    # Exclusion zones (chart badge at top center)
    badge_excl = (150, 165, 930, 265)
    min_jump_dist = 260.0

    placements: List[Dict[str, Any]] = []
    prev_center: Optional[Tuple[float, float]] = None

    t_start = 0.2 if duration > 2.0 else 0.0
    while t_start < duration:
        best_pos = (min_x, min_y)
        best_dist = -1.0

        for _ in range(60):
            x = gen.randint(min_x, max_x)
            y = gen.randint(min_y, max_y)
            rect = (x, y, x + wm_width, y + wm_height)
            if _rects_overlap(rect, badge_excl):
                continue

            cx = x + wm_width / 2.0
            cy = y + wm_height / 2.0
            if prev_center is None:
                best_pos = (x, y)
                break

            dist = math.hypot(cx - prev_center[0], cy - prev_center[1])
            if dist >= min_jump_dist:
                best_pos = (x, y)
                break
            if dist > best_dist:
                best_dist = dist
                best_pos = (x, y)

        x, y = best_pos
        prev_center = (x + wm_width / 2.0, y + wm_height / 2.0)
        placements.append({
            "x": x,
            "y": y,
            "cx": prev_center[0],
            "cy": prev_center[1],
            "start": round(t_start, 3),
        })
        t_start += WM_CYCLE_SEC

    return placements


def get_watermark_state(
    placement: Dict[str, Any],
    t_sec: float,
    peak_opacity: float = 0.40,
) -> Optional[Tuple[float, float, float]]:
    """
    Return (alpha, angle_deg, scale) for a watermark placement at t_sec, or None if hidden.
    - Spin-fade IN: 0.8s (360° eased spin, scale 0.6->1.0, alpha 0->peak_opacity)
    - Hold:         2.5s (upright 0°, scale 1.0, alpha=peak_opacity)
    - Fade OUT:     3.0s (upright 0°, scale 1.0, alpha peak_opacity->0)
    - Pause:        0.7s (hidden)
    """
    dt = t_sec - placement["start"]
    if dt < 0.0 or dt >= WM_SPIN_IN_SEC + WM_HOLD_SEC + WM_FADE_OUT_SEC:
        return None

    if dt < WM_SPIN_IN_SEC:
        u = dt / WM_SPIN_IN_SEC
        eased = 1.0 - (1.0 - u) ** 3  # Cubic ease-out
        alpha = peak_opacity * eased
        angle = (1.0 - eased) * 360.0
        scale = 0.6 + 0.4 * eased
        return (alpha, angle, scale)

    if dt < WM_SPIN_IN_SEC + WM_HOLD_SEC:
        return (peak_opacity, 0.0, 1.0)

    fade_dt = dt - (WM_SPIN_IN_SEC + WM_HOLD_SEC)
    u = max(0.0, min(1.0, fade_dt / WM_FADE_OUT_SEC))
    alpha = peak_opacity * (1.0 - u)
    return (alpha, 0.0, 1.0)


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
    template: str = "hifi",
    watermark_path: str = DEFAULT_WATERMARK_PATH,
    watermark_opacity: float = 0.65,
    facts: Optional[List[str]] = None,
    audio_features: Optional[Dict[str, Any]] = None,
    use_browser: bool = True,
    hook_overlay: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Render a 1080x1920 9:16 MP4 video using the Chromium Style Pack renderer
    (hifi, vhs, neon, riso, kinetic, cassette + legacy vinyl/cd/retro),
    with automatic fallback to the lightweight Python/Pillow renderer if
    headless Chromium is unavailable.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    hook_dur = float(hook_overlay.get("duration", 0.0)) if isinstance(hook_overlay, dict) else 0.0
    fact_start_margin = (hook_dur + 1.1) if hook_dur > 0.2 else 2.0
    fact_schedule = compute_fact_schedule(facts, duration, start_margin=fact_start_margin)

    if use_browser:
        try:
            from core.browser_renderer import render_style_video
            return render_style_video(
                artist=artist,
                title=title,
                year=year,
                chart_badge=chart_badge,
                artwork_path=artwork_path,
                audio_path=audio_path,
                rms_envelope=rms_envelope,
                duration=duration,
                output_path=output_path,
                template=template,
                watermark_path=watermark_path,
                watermark_opacity=watermark_opacity,
                fact_schedule=fact_schedule,
                audio_features=audio_features,
                hook_overlay=hook_overlay,
            )
        except Exception as e:
            logger.warning(f"Chromium Style Pack renderer failed ({e}); falling back to Pillow renderer.")

    palette = extract_palette(artwork_path)
    accent = palette["accent"]

    # Base static canvas (background + header + title/artist)
    base_bg = _create_ambient_background(artwork_path, palette)
    draw_base = ImageDraw.Draw(base_bg)

    font_badge = _load_font(30, bold=True)
    font_title = _load_font(56, bold=True)
    font_artist = _load_font(40, bold=False)

    # 1. Chart Badge Pill at top safe zone (y=185)
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

    # 3. Prepare animated random GenX watermark (full alpha base; scaled per frame)
    wm_base = _prepare_watermark(watermark_path, opacity=1.0, target_width=170)
    wm_schedule: List[Dict[str, Any]] = []
    if wm_base:
        wm_schedule = generate_watermark_schedule(duration, wm_base.width, wm_base.height)

    # 4. Pre-render 3 fading fact cards timed from 2s after start to 2s before end
    fact_schedule = compute_fact_schedule(facts, duration)
    fact_cards = [
        _render_fact_card(slot["text"], slot["index"], slot["total"], accent)
        for slot in fact_schedule
    ]

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
    eq_base_y = 1330

    try:
        for frame_idx in range(total_frames):
            frame = base_bg.copy()
            t_sec = frame_idx / FPS
            progress = frame_idx / max(1, total_frames - 1)
            energy = rms_envelope[frame_idx] if frame_idx < len(rms_envelope) else 0.3

            if template == "vinyl":
                # Rotate vinyl at ~33.3 RPM
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

            # Draw Beat-Reactive Equalizer Bars (compact height to leave room for larger fact card)
            eq_draw = ImageDraw.Draw(frame)
            for b in range(n_bars):
                # Symmetric bell curve + wave modulation
                dist_center = abs(b - (n_bars - 1) / 2.0) / (n_bars / 2.0)
                envelope_shape = max(0.25, 1.0 - dist_center * 0.7)
                wave = 0.55 + 0.45 * math.sin(t_sec * 10.0 + b * 0.7)
                bar_h = int(10 + (energy * 56 * envelope_shape * wave))
                bx0 = eq_start_x + b * (bar_width + bar_gap)
                by0 = eq_base_y - bar_h
                eq_draw.rounded_rectangle(
                    [bx0, by0, bx0 + bar_width, eq_base_y],
                    radius=6,
                    fill=(accent[0], accent[1], accent[2], 215)
                )

            # Composite active fading Track Fact card (y=1352..1544)
            for idx, slot in enumerate(fact_schedule):
                f_alpha = get_fact_alpha(slot, t_sec)
                if f_alpha > 0.005:
                    faded_card = _scale_rgba_alpha(fact_cards[idx], f_alpha)
                    frame.alpha_composite(faded_card, (FACT_CARD_X, FACT_CARD_Y))
                    break

            # Composite randomly placed spin-fading GenX Watermark
            if wm_base and wm_schedule:
                for placement in wm_schedule:
                    wm_state = get_watermark_state(placement, t_sec, peak_opacity=watermark_opacity)
                    if wm_state is None:
                        continue
                    wm_alpha, wm_angle, wm_scale = wm_state
                    if wm_alpha <= 0.005:
                        break
                    wm_frame = wm_base
                    if wm_scale < 0.999:
                        sw = max(1, int(wm_base.width * wm_scale))
                        sh = max(1, int(wm_base.height * wm_scale))
                        wm_frame = wm_frame.resize((sw, sh), Image.Resampling.BILINEAR)
                    if wm_angle > 0.1:
                        wm_frame = wm_frame.rotate(wm_angle, resample=Image.Resampling.BILINEAR, expand=True)
                    wm_frame = _scale_rgba_alpha(wm_frame, wm_alpha)
                    px = max(0, min(WIDTH - wm_frame.width, int(placement["cx"] - wm_frame.width / 2.0)))
                    py = max(0, min(HEIGHT - wm_frame.height, int(placement["cy"] - wm_frame.height / 2.0)))
                    frame.alpha_composite(wm_frame, (px, py))
                    break

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
