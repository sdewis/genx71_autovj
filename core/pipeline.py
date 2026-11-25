"""
End-to-End Video Generation & Posting Pipeline for GenX71 AutoVJ.
Shared by both the 24/7 background daemon and the FastAPI Admin PWA.

Supports two execution modes:
  1. Direct / synchronous generation (`generate_post_video`)
  2. Async TTS-queued generation (`enqueue_tts_post` -> `TTSJobWorker` -> `process_pending_post`),
     which immediately records a post with status='tts_pending', synthesizes the regional
     cloned DJ voice hook sequentially on CPU, mixes the 'talk up the post' radio ramp,
     and renders the final Chromium Style Pack MP4.
"""

import os
import re
import time
import random
import logging
import threading
from datetime import datetime
from typing import Optional, Dict, Any, Callable

from core.db import (
    init_db, get_setting, record_post, update_post_status, update_post_fields,
    get_post_by_id, get_track_by_id, queue_post, get_recent_templates,
    get_recent_hook_templates, get_pending_tts_posts,
    claim_next_pending_tts_post, claim_post_for_processing, claim_post_for_publish,
)
from core.charts import select_next_track, format_chart_badge
from core.artwork import get_single_artwork
from core.audio import prepare_track_audio
from core.facts import get_or_fetch_track_facts
from core.hooks import build_dj_hook_script
from core.styles import select_style_pack
from core.tts import (
    TTS_CACHE_DIR, resolve_voice_region, synthesize_hook_audio, extract_voice_rms,
)
from core.video_renderer import render_tiktok_video, OUTPUT_DIR
from core.tiktok_poster import upload_to_tiktok, build_caption, check_cookie_health

logger = logging.getLogger(__name__)

# In-process guard preventing two threads from rendering/publishing the same post_id concurrently
_ACTIVE_POST_LOCK = threading.Lock()
_ACTIVE_POST_IDS: set = set()

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


def select_next_style(
    year: Optional[int],
    db_path: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> str:
    """
    Select the next visual style pack using Era-Weighted Auto-Rotate,
    avoiding the 2 most recently used styles in SQLite history.
    """
    recent = get_recent_templates(limit=2, db_path=db_path)
    return select_style_pack(year=year, recent_styles=recent, rng=rng)


def _safe_filename(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")[:50]


def _resolve_template(
    year: Optional[int],
    template: Optional[str],
    era_appropriate: Optional[bool],
    db_path: Optional[str] = None,
) -> str:
    if era_appropriate is True or template == "era":
        return select_era_appropriate_template(year)
    if era_appropriate is False:
        raw_tpl = template or get_setting("default_template", "auto", db_path=db_path) or "auto"
        return select_next_style(year, db_path=db_path) if raw_tpl == "auto" else raw_tpl
    if template:
        return select_next_style(year, db_path=db_path) if template == "auto" else template
    if get_setting("era_appropriate_template", "false", db_path=db_path) == "true":
        return select_era_appropriate_template(year)
    raw_tpl = get_setting("default_template", "auto", db_path=db_path) or "auto"
    return select_next_style(year, db_path=db_path) if raw_tpl == "auto" else raw_tpl


def _finalize_rendered_post(
    post_id: int,
    artist: str,
    title: str,
    year: int,
    chart_badge: str,
    output_path: str,
    publish: bool,
    auto_queue: Optional[bool],
    prog_cb: Callable[[str, int], None],
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    if publish:
        health = check_cookie_health(db_path=db_path)
        if not health["healthy"]:
            update_post_status(post_id, "rendered", error_message=health["reason"], db_path=db_path)
            prog_cb(f"Rendered video saved, skipped TikTok upload: {health['reason']}", 100)
        else:
            claimed = claim_post_for_publish(post_id, allow_republish=False, db_path=db_path)
            if not claimed:
                logger.warning(
                    "Skipping duplicate TikTok upload for post #%s (already publishing or posted).",
                    post_id,
                )
                prog_cb("Post already publishing or published; skipped duplicate upload.", 100)
                return get_post_by_id(post_id, db_path=db_path)
            auto_pub = get_setting("auto_publish", "false", db_path=db_path) == "true"
            prog_cb("Uploading to TikTok via stealth browser...", 92)
            try:
                caption = build_caption(artist, title, year, chart_badge)
                res = upload_to_tiktok(output_path, caption, auto_publish=auto_pub, db_path=db_path)
                update_post_status(post_id, res["status"], db_path=db_path)
                prog_cb(res["message"], 100)
            except Exception as e:
                logger.error(f"TikTok upload failed: {e}")
                update_post_status(post_id, "failed", error_message=str(e), db_path=db_path)
                prog_cb(f"Upload failed (video saved): {e}", 100)
    else:
        should_queue = (
            auto_queue
            if auto_queue is not None
            else get_setting("auto_queue_on_generate", "false", db_path=db_path) == "true"
        )
        if should_queue:
            pos = queue_post(post_id, db_path=db_path)
            prog_cb(f"Video rendered and queued (#{pos}) for next scheduled post!", 100)
        else:
            prog_cb("Video rendered and ready for preview!", 100)

    return get_post_by_id(post_id, db_path=db_path)


def enqueue_tts_post(
    track_id: Optional[int] = None,
    template: Optional[str] = None,
    era_appropriate: Optional[bool] = None,
    force_wildcard: Optional[bool] = None,
    publish: bool = False,
    auto_queue: Optional[bool] = None,
    voice_region: Optional[str] = None,
    custom_hook_text: Optional[str] = None,
    initial_status: str = "tts_pending",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Immediately reserve a track, prepare its DJ hook script & regional voice routing,
    and insert a post row with status=initial_status (default 'tts_pending') so the
    Web GUI displays it right away.
    """
    init_db(db_path)

    track = select_next_track(track_id=track_id, force_wildcard=force_wildcard, db_path=db_path)
    if not track:
        raise RuntimeError("No available unposted tracks left in catalog!")

    artist = track["artist"]
    title = track["title"]
    year = track["year"]
    chart_badge = format_chart_badge(track)
    chosen_template = _resolve_template(year, template, era_appropriate, db_path=db_path)

    artwork_path = get_single_artwork(artist, title)
    facts = get_or_fetch_track_facts(track, db_path=db_path)
    recent_hooks = get_recent_hook_templates(limit=12, db_path=db_path)
    hook_info = build_dj_hook_script(
        track,
        facts=facts,
        recent_hook_ids=recent_hooks,
        custom_text=custom_hook_text,
    )

    resolved_region = resolve_voice_region(
        track_chart_region=track.get("chart_region"),
        requested_region=voice_region,
    )
    if not resolved_region:
        raise RuntimeError("No voice reference clips uploaded yet. Upload a UK or US voice clip in Admin first.")

    should_queue = (
        auto_queue
        if auto_queue is not None
        else get_setting("auto_queue_on_generate", "false", db_path=db_path) == "true"
    )

    post_id = record_post(
        track_id=track["id"],
        artist=artist,
        title=title,
        year=year,
        chart_info=chart_badge,
        template_used=chosen_template,
        video_path=None,
        audio_url=None,
        artwork_path=artwork_path,
        status=initial_status,
        hook_text=hook_info["text"],
        hook_template_id=hook_info["hook_id"],
        voice_region=resolved_region,
        tts_enabled=1,
        auto_queue=1 if should_queue else 0,
        publish_after_render=1 if publish else 0,
        db_path=db_path,
    )
    return get_post_by_id(post_id, db_path=db_path)


def process_pending_post(
    post_id: int,
    render_without_tts: bool = False,
    progress_cb: Optional[Callable[[str, int], None]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Complete TTS synthesis (unless render_without_tts=True), radio-ramp audio mixing,
    and Chromium Style Pack rendering for a pending/failed post.
    Protected against concurrent execution on the same post_id.
    """
    init_db(db_path)
    with _ACTIVE_POST_LOCK:
        if post_id in _ACTIVE_POST_IDS:
            logger.warning("Post #%s is already being processed; skipping duplicate run.", post_id)
            existing = get_post_by_id(post_id, db_path=db_path)
            if not existing:
                raise RuntimeError(f"Post #{post_id} not found.")
            return existing
        _ACTIVE_POST_IDS.add(post_id)

    try:
        post = get_post_by_id(post_id, db_path=db_path)
        if not post:
            raise RuntimeError(f"Post #{post_id} not found.")

        status = post.get("status")
        if status in ("rendered", "scheduled", "publishing", "posted", "draft"):
            logger.warning(
                "Post #%s already in terminal/ready status '%s'; skipping duplicate processing.",
                post_id, status,
            )
            return post

        if status in ("tts_pending", "tts_failed", "failed"):
            claimed = claim_post_for_processing(
                post_id,
                allowed_statuses=(status,),
                target_status="rendering" if render_without_tts else "tts_processing",
                db_path=db_path,
            )
            if not claimed:
                logger.warning("Post #%s was already claimed by another worker; skipping.", post_id)
                return get_post_by_id(post_id, db_path=db_path)
            post = claimed

        return _process_pending_post_impl(
            post=post,
            post_id=post_id,
            render_without_tts=render_without_tts,
            progress_cb=progress_cb,
            db_path=db_path,
        )
    finally:
        with _ACTIVE_POST_LOCK:
            _ACTIVE_POST_IDS.discard(post_id)


def _process_pending_post_impl(
    post: Dict[str, Any],
    post_id: int,
    render_without_tts: bool = False,
    progress_cb: Optional[Callable[[str, int], None]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    def _prog(msg: str, pct: int):
        logger.info(f"[Post #{post_id} | {pct}%] {msg}")
        if progress_cb:
            progress_cb(msg, pct)

    artist = post["artist"]
    title = post["title"]
    year = post["year"]
    chart_badge = post.get("chart_info") or ""
    chosen_template = post.get("template_used") or "hifi"
    artwork_path = post.get("artwork_path") or get_single_artwork(artist, title)

    track = get_track_by_id(post["track_id"], db_path=db_path) if post.get("track_id") else {
        "artist": artist, "title": title, "year": year, "chart_region": "UK"
    }
    facts = get_or_fetch_track_facts(track, db_path=db_path)
    wm_opacity = float(get_setting("watermark_opacity", "0.65", db_path=db_path) or "0.65")

    tts_audio_path: Optional[str] = None
    hook_overlay: Optional[Dict[str, Any]] = None

    use_tts = bool(post.get("tts_enabled")) and not render_without_tts
    if use_tts:
        hook_text = (post.get("hook_text") or "").strip()
        if not hook_text:
            recent_hooks = get_recent_hook_templates(limit=12, db_path=db_path)
            hook_info = build_dj_hook_script(track, facts=facts, recent_hook_ids=recent_hooks)
            hook_text = hook_info["text"]
            update_post_fields(
                post_id,
                hook_text=hook_text,
                hook_template_id=hook_info["hook_id"],
                db_path=db_path,
            )

        region = post.get("voice_region") or resolve_voice_region(track.get("chart_region"))
        if not region:
            err = "No voice reference clips uploaded for TTS."
            update_post_status(post_id, "tts_failed", error_message=err, db_path=db_path)
            _prog(err, 100)
            return get_post_by_id(post_id, db_path=db_path)

        existing_tts = post.get("tts_audio_path")
        if existing_tts and os.path.exists(existing_tts) and os.path.getsize(existing_tts) > 4000:
            _prog(f"Reusing synthesized {region.upper()} DJ hook audio...", 35)
            tts_audio_path = existing_tts
            voice_rms = extract_voice_rms(tts_audio_path, fps=30)
            tts_dur = round(len(voice_rms) / 30.0, 2) if voice_rms else 6.0
        else:
            _prog(f"Synthesizing {region.upper()} DJ voice hook on CPU...", 20)
            tts_speed = float(get_setting("tts_speed", "1.05", db_path=db_path) or "1.05")
            os.makedirs(TTS_CACHE_DIR, exist_ok=True)
            tts_out = os.path.join(
                TTS_CACHE_DIR,
                f"post_{post_id}_{_safe_filename(artist)}_{_safe_filename(title)}_{region}.wav",
            )
            try:
                tts_res = synthesize_hook_audio(
                    text=hook_text,
                    region=region,
                    output_path=tts_out,
                    speed=tts_speed,
                    db_path=db_path,
                )
                tts_audio_path = tts_res["audio_path"]
                tts_dur = tts_res["duration"]
                voice_rms = tts_res["voice_rms"]
                update_post_fields(post_id, tts_audio_path=tts_audio_path, voice_region=region, db_path=db_path)
            except Exception as exc:
                logger.error("TTS synthesis failed for post #%s: %s", post_id, exc)
                update_post_status(post_id, "tts_failed", error_message=str(exc), db_path=db_path)
                _prog(f"TTS failed: {exc}", 100)
                return get_post_by_id(post_id, db_path=db_path)

        hook_overlay = {
            "text": hook_text,
            "duration": tts_dur,
            "voice_rms": voice_rms,
            "region": region,
        }

    update_post_status(post_id, "rendering", error_message=None, db_path=db_path)
    _prog("Sourcing & mixing audio track...", 50)
    try:
        audio_info = prepare_track_audio(artist, title, tts_audio_path=tts_audio_path)
        if hook_overlay:
            if audio_info.get("tts_duration"):
                hook_overlay["duration"] = float(audio_info["tts_duration"])
            if audio_info.get("ramp_start"):
                hook_overlay["ramp_start"] = float(audio_info["ramp_start"])
            if audio_info.get("ramp_end"):
                hook_overlay["ramp_end"] = float(audio_info["ramp_end"])

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_filename = f"{timestamp}_{_safe_filename(artist)}_{_safe_filename(title)}_{chosen_template}.mp4"
        output_path = os.path.join(OUTPUT_DIR, out_filename)

        _prog(f"Rendering 9:16 '{chosen_template}' TikTok video...", 70)
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
            facts=facts,
            audio_features=audio_info.get("audio_features"),
            hook_overlay=hook_overlay,
        )

        update_post_fields(
            post_id,
            video_path=output_path,
            audio_url=str(audio_info["source_url"]),
            artwork_path=artwork_path,
            status="rendered",
            error_message=None,
            db_path=db_path,
        )
    except Exception as exc:
        logger.exception("Render failed for post #%s", post_id)
        update_post_status(post_id, "failed", error_message=str(exc), db_path=db_path)
        _prog(f"Render failed: {exc}", 100)
        return get_post_by_id(post_id, db_path=db_path)

    return _finalize_rendered_post(
        post_id=post_id,
        artist=artist,
        title=title,
        year=year,
        chart_badge=chart_badge,
        output_path=output_path,
        publish=bool(post.get("publish_after_render")),
        auto_queue=bool(post.get("auto_queue")),
        prog_cb=_prog,
        db_path=db_path,
    )


def generate_post_video(
    track_id: Optional[int] = None,
    template: Optional[str] = None,
    era_appropriate: Optional[bool] = None,
    force_wildcard: Optional[bool] = None,
    publish: bool = False,
    auto_queue: Optional[bool] = None,
    tts_enabled: Optional[bool] = None,
    voice_region: Optional[str] = None,
    custom_hook_text: Optional[str] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Execute the full GenX71 AutoVJ pipeline synchronously:
    1. Select non-duplicate GenX track (respecting wildcard % and blacklist)
    2. Fetch high-res single artwork & 3 track trivia facts
    3. Optionally synthesize cloned regional DJ hook TTS & mix ~58s radio-ramp audio
    4. Render 9:16 TikTok MP4 with selected style pack, FFT visualizer, ON AIR captions & FACTS
    5. Record in SQLite history
    6. Optionally upload/publish to TikTok via stealth Playwright
    """
    init_db(db_path)

    def _prog(msg: str, pct: int):
        logger.info(f"[{pct}%] {msg}")
        if progress_cb:
            progress_cb(msg, pct)

    use_tts = (
        bool(tts_enabled)
        if tts_enabled is not None
        else get_setting("tts_enabled", "false", db_path=db_path) == "true"
    )
    if use_tts:
        resolved = resolve_voice_region(requested_region=voice_region)
        if resolved:
            pending = enqueue_tts_post(
                track_id=track_id,
                template=template,
                era_appropriate=era_appropriate,
                force_wildcard=force_wildcard,
                publish=publish,
                auto_queue=auto_queue,
                voice_region=voice_region,
                custom_hook_text=custom_hook_text,
                initial_status="tts_processing",
                db_path=db_path,
            )
            return process_pending_post(pending["id"], progress_cb=progress_cb, db_path=db_path)
        logger.warning("TTS enabled but no voice samples uploaded; falling back to non-TTS render.")

    _prog("Selecting GenX chart track...", 10)
    track = select_next_track(track_id=track_id, force_wildcard=force_wildcard, db_path=db_path)
    if not track:
        raise RuntimeError("No available unposted tracks left in catalog!")

    artist = track["artist"]
    title = track["title"]
    year = track["year"]
    chart_badge = format_chart_badge(track)
    chosen_template = _resolve_template(year, template, era_appropriate, db_path=db_path)
    wm_opacity = float(get_setting("watermark_opacity", "0.65", db_path=db_path) or "0.65")

    _prog(f"Fetching artwork & trivia facts for {artist} - {title}...", 25)
    artwork_path = get_single_artwork(artist, title)
    facts = get_or_fetch_track_facts(track, db_path=db_path)

    _prog("Sourcing & boundary-clipping audio hook...", 45)
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
        facts=facts,
        audio_features=audio_info.get("audio_features"),
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

    return _finalize_rendered_post(
        post_id=post_id,
        artist=artist,
        title=title,
        year=year,
        chart_badge=chart_badge,
        output_path=output_path,
        publish=publish,
        auto_queue=auto_queue,
        prog_cb=_prog,
        db_path=db_path,
    )


class TTSJobWorker:
    """
    Sequential background worker that drains 'tts_pending' posts one at a time
    so CPU voice synthesis + Chromium rendering never thrash the host machine.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.active_post_id: Optional[int] = None
        self.active_message: str = "Idle"
        self.active_progress: int = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="genx71-tts-queue", daemon=True)
        self._thread.start()
        self.wake()
        logger.info("GenX71 TTS background queue worker started.")

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def wake(self) -> None:
        self._wake_event.set()

    def status(self) -> Dict[str, Any]:
        pending = get_pending_tts_posts(db_path=self.db_path)
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "active_post_id": self.active_post_id,
            "message": self.active_message,
            "progress": self.active_progress,
            "pending_count": len(pending),
        }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = claim_next_pending_tts_post(db_path=self.db_path)
                if job:
                    pid = job["id"]
                    self.active_post_id = pid
                    self.active_progress = 10
                    self.active_message = f"Processing {job['artist']} - {job['title']}..."

                    def _cb(msg: str, pct: int):
                        self.active_message = msg
                        self.active_progress = pct

                    try:
                        process_pending_post(pid, progress_cb=_cb, db_path=self.db_path)
                    except Exception as exc:
                        logger.exception("Unhandled error in TTSJobWorker for post #%s", pid)
                        update_post_status(pid, "tts_failed", error_message=str(exc), db_path=self.db_path)
                    finally:
                        self.active_post_id = None
                        self.active_progress = 0
                        self.active_message = "Idle"
                    continue
            except Exception:
                logger.exception("TTSJobWorker poll error")

            self._wake_event.wait(timeout=4.0)
            self._wake_event.clear()
