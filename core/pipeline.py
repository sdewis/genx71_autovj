"""
End-to-End Video Generation & Posting Pipeline for GenX71 AutoVJ.
Shared by both the 24/7 background daemon and the FastAPI Admin PWA.
"""

import os
import re
import random
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Callable

from core.db import (
    init_db, get_setting, record_post, update_post_status, get_post_by_id
)
from core.charts import select_next_track, format_chart_badge
from core.artwork import get_single_artwork
from core.audio import prepare_track_audio
from core.facts import get_or_fetch_track_facts
from core.video_renderer import render_tiktok_video, OUTPUT_DIR
from core.tiktok_poster import upload_to_tiktok, build_caption, check_cookie_health

logger = logging.getLogger(__name__)

# 1988: CD sales surpassed vinyl LPs and CDs became commonly available
CD_ERA_START_YEAR = 1988
VINYL_AUDIOPHILE_CHANCE = 0.25


def select_era_appropriate_template(
    year: Optional[int],
    rng: Optional[random.Random] = None,
) -> str:
    """
    Select a visual template matching the track's release era:
    - CD era (year >= 1988): 'cd'
    - Vinyl era (year < 1988 or unknown): 25% chance 'retro' (audiophile card), 75% 'vinyl'
    """
    if year is not None and year >= CD_ERA_START_YEAR:
        return "cd"

    r = rng if rng is not None else random
    if r.random() < VINYL_AUDIOPHILE_CHANCE:
        return "retro"
    return "vinyl"


def _safe_filename(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")[:50]


def generate_post_video(
    track_id: Optional[int] = None,
    template: Optional[str] = None,
    era_appropriate: Optional[bool] = None,
    force_wildcard: Optional[bool] = None,
    publish: bool = False,
    progress_cb: Optional[Callable[[str, int], None]] = None,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Execute the full GenX71 AutoVJ pipeline:
    1. Select non-duplicate GenX track (respecting wildcard % and blacklist)
    2. Fetch high-res single artwork & 3 track trivia facts
    3. Source & intelligently clip 35-45s audio hook (>=30s, <60s TikTok limit)
    4. Render 9:16 TikTok MP4 with fading facts & animated random GenX watermark
    5. Record in SQLite history
    6. Optionally upload/publish to TikTok via stealth Playwright
    """
    init_db(db_path)

    def _prog(msg: str, pct: int):
        logger.info(f"[{pct}%] {msg}")
        if progress_cb:
            progress_cb(msg, pct)

    _prog("Selecting GenX chart track...", 10)
    track = select_next_track(track_id=track_id, force_wildcard=force_wildcard, db_path=db_path)
    if not track:
        raise RuntimeError("No available unposted tracks left in catalog!")

    artist = track["artist"]
    title = track["title"]
    year = track["year"]
    chart_badge = format_chart_badge(track)

    if era_appropriate is True or template == "era":
        chosen_template = select_era_appropriate_template(year)
    elif era_appropriate is False:
        chosen_template = template or get_setting("default_template", "vinyl", db_path=db_path) or "vinyl"
    else:
        if template:
            chosen_template = template
        elif get_setting("era_appropriate_template", "false", db_path=db_path) == "true":
            chosen_template = select_era_appropriate_template(year)
        else:
            chosen_template = get_setting("default_template", "vinyl", db_path=db_path) or "vinyl"

    wm_opacity = float(get_setting("watermark_opacity", "0.40", db_path=db_path) or "0.40")

    _prog(f"Fetching artwork & trivia facts for {artist} - {title}...", 25)
    artwork_path = get_single_artwork(artist, title)
    facts = get_or_fetch_track_facts(track, db_path=db_path)

    _prog("Sourcing & boundary-clipping 35-45s audio hook...", 45)
    audio_info = prepare_track_audio(artist, title)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_filename = f"{timestamp}_{_safe_filename(artist)}_{_safe_filename(title)}_{chosen_template}.mp4"
    output_path = os.path.join(OUTPUT_DIR, out_filename)

    _prog(f"Rendering 9:16 '{chosen_template}' TikTok video...", 65)
    render_tiktok_video(
        artist=artist,
        title=title,
        year=year,
        chart_badge=chart_badge,
        artwork_path=artwork_path,
        audio_path=audio_info["audio_path"],
        rms_envelope=audio_info["rms_envelope"],
        duration=audio_info["duration"],
        output_path=output_path,
        template=chosen_template,
        watermark_opacity=wm_opacity,
        facts=facts
    )

    _prog("Saving post record to deduplication DB...", 85)
    post_id = record_post(
        track_id=track["id"],
        artist=artist,
        title=title,
        year=year,
        chart_info=chart_badge,
        template_used=chosen_template,
        video_path=output_path,
        audio_url=str(audio_info["source_url"]),
        artwork_path=artwork_path,
        status="rendered",
        db_path=db_path
    )

    if publish:
        health = check_cookie_health(db_path=db_path)
        if not health["healthy"]:
            update_post_status(post_id, "rendered", error_message=health["reason"], db_path=db_path)
            _prog(f"Rendered video saved, skipped TikTok upload: {health['reason']}", 100)
        else:
            auto_pub = get_setting("auto_publish", "false", db_path=db_path) == "true"
            _prog("Uploading to TikTok via stealth browser...", 92)
            try:
                caption = build_caption(artist, title, year, chart_badge)
                res = upload_to_tiktok(output_path, caption, auto_publish=auto_pub, db_path=db_path)
                update_post_status(post_id, res["status"], db_path=db_path)
                _prog(res["message"], 100)
            except Exception as e:
                logger.error(f"TikTok upload failed: {e}")
                update_post_status(post_id, "failed", error_message=str(e), db_path=db_path)
                _prog(f"Upload failed (video saved): {e}", 100)
    else:
        _prog("Video rendered and ready for preview!", 100)

    return get_post_by_id(post_id, db_path=db_path)
