"""
FastAPI Backend Server & PWA Host for GenX71 AutoVJ.
Provides full admin, 24/7 jittered scheduling, Chromium Style Pack & Regional TTS
video generation, video preview, History render deletion/re-rendering, cookie
management, and deduplication history for both Samsung S24 Ultra PWA and Linux
desktop browsers.
"""

import os
import json
import threading
import logging
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.db import (
    init_db, count_tracks, get_available_tracks, get_recent_posts,
    get_all_tracks, get_track_by_id, add_track, add_to_blacklist, remove_from_blacklist,
    get_blacklist, get_all_settings, set_setting, get_setting, get_post_by_id,
    update_post_status, update_post_fields, is_track_posted, is_blacklisted,
    queue_post, get_queued_posts, remove_from_queue, reorder_queue, clear_queue,
    delete_post, get_track_batches, get_tracks_by_batch,
    upsert_post_analytics, get_analytics_reports, get_latest_analytics_report,
    get_active_feedback_weights, claim_post_for_publish,
    claim_post_for_processing, recover_interrupted_posts,
)
from core.analytics import (
    build_analytics_summary, sync_tiktok_analytics, import_analytics_file,
    run_analytics_report, run_daily_analytics_report, apply_report_feedback,
    build_learning_summary,
)
from core.llm_analyst import get_llm_health, ensure_model_pulled
from core.charts import seed_charts_if_empty, load_new_chart_batch
from core.pipeline import (
    generate_post_video, enqueue_tts_post, process_pending_post, TTSJobWorker
)
from core.tts import (
    TTS_CACHE_DIR, list_voice_profiles, save_voice_sample, delete_voice_sample,
    get_tts_worker_health, synthesize_hook_audio, resolve_voice_region,
)
from core.tiktok_poster import (
    check_cookie_health, upload_to_tiktok, build_caption,
    parse_cookies, parse_cookies_json_secure, CookieParseError
)
from core.video_renderer import OUTPUT_DIR
from daemon import AutoVJDaemon

logger = logging.getLogger("genx71.api")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(PROJECT_ROOT, "web")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(WEB_DIR, exist_ok=True)
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

daemon = AutoVJDaemon()
tts_worker = TTSJobWorker()

# Live generation job state
gen_state: Dict[str, Any] = {
    "running": False,
    "progress": 0,
    "message": "Idle",
    "last_post": None,
    "error": None
}
gen_lock = threading.Lock()

# Live TikTok publish job state
publish_state: Dict[str, Any] = {
    "running": False,
    "post_id": None,
    "label": "",
    "progress": 0,
    "message": "Idle",
    "status": None,
    "error": None
}
publish_lock = threading.Lock()

# Live Analytics sync / LLM report job state
analytics_state: Dict[str, Any] = {
    "running": False,
    "task": None,
    "progress": 0,
    "message": "Idle",
    "last_report_id": None,
    "error": None,
}
analytics_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    recovered = recover_interrupted_posts()
    if any(recovered.values()):
        logger.warning("Recovered interrupted posts on startup: %s", recovered)
    seed_charts_if_empty()
    daemon.start()
    tts_worker.start()
    yield
    tts_worker.stop()
    daemon.stop()


app = FastAPI(title="GenX71 AutoVJ", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Request Models ---

class GenerateRequest(BaseModel):
    track_id: Optional[int] = None
    template: Optional[str] = None
    era_appropriate: Optional[bool] = None
    force_wildcard: Optional[bool] = None
    publish: bool = False
    auto_queue: Optional[bool] = None
    tts_enabled: Optional[bool] = None
    voice_region: Optional[str] = None
    custom_hook_text: Optional[str] = None


class TTSTestRequest(BaseModel):
    region: str = "uk"
    text: Optional[str] = None


class TrackCreateRequest(BaseModel):
    artist: str
    title: str
    year: int
    chart_peak: Optional[int] = None
    chart_region: str = "UK"
    is_wildcard: int = 0
    genre: str = "Pop/Rock"


class BlacklistRequest(BaseModel):
    artist: str
    title: Optional[str] = None
    reason: str = ""


class SettingsUpdateRequest(BaseModel):
    settings: Dict[str, str]


class CookiesUpdateRequest(BaseModel):
    cookies_json: str


class CookieFileImportRequest(BaseModel):
    filename: str
    content: str


class QueueReorderRequest(BaseModel):
    post_ids: List[int]


class PostAnalyticsUpdateRequest(BaseModel):
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    avg_watch_time_sec: float = 0.0
    completion_rate: float = 0.0
    tiktok_url: Optional[str] = None


class AnalyticsImportRequest(BaseModel):
    filename: str
    content: str


class AnalyticsRunRequest(BaseModel):
    period: str = "daily"
    sync_first: Optional[bool] = None
    auto_apply_feedback: Optional[bool] = None


class ApplyFeedbackRequest(BaseModel):
    apply_wildcard: bool = True


class LLMPullRequest(BaseModel):
    model: Optional[str] = None


# --- API Endpoints ---

@app.get("/api/status")
def api_status():
    total_tracks = count_tracks()
    avail_all = len(get_available_tracks())
    avail_wild = len(get_available_tracks(is_wildcard=True))
    posts = get_recent_posts(limit=500)
    posted_count = sum(1 for p in posts if p["status"] in ("posted", "draft"))
    queued_count = len(get_queued_posts())
    tts_pending_count = sum(1 for p in posts if p["status"] in ("tts_pending", "tts_processing", "rendering"))
    cookie_health = check_cookie_health()

    return {
        "daemon_running": daemon.running,
        "next_scheduled_run": daemon.next_run_info,
        "next_analytics_run": daemon.next_analytics_run_info,
        "cookie_health": cookie_health,
        "stats": {
            "total_tracks": total_tracks,
            "available_tracks": avail_all,
            "available_wildcards": avail_wild,
            "total_posts": len(posts),
            "published_count": posted_count,
            "queued_count": queued_count,
            "tts_pending_count": tts_pending_count,
        },
        "generation": gen_state,
        "publish": publish_state,
        "analytics_job": analytics_state,
        "tts_queue": tts_worker.status(),
    }


@app.get("/api/styles")
def api_styles():
    from core.styles import get_style_metadata
    return {"styles": get_style_metadata()}


def _start_generation_worker(
    *,
    track_id: Optional[int],
    template: Optional[str],
    era_appropriate: Optional[bool],
    force_wildcard: Optional[bool],
    publish: bool,
    auto_queue: Optional[bool],
) -> None:
    def _worker():
        def _cb(msg: str, pct: int):
            gen_state["message"] = msg
            gen_state["progress"] = pct

        try:
            post = generate_post_video(
                track_id=track_id,
                template=template,
                era_appropriate=era_appropriate,
                force_wildcard=force_wildcard,
                publish=publish,
                auto_queue=auto_queue,
                tts_enabled=False,
                progress_cb=_cb
            )
            gen_state["last_post"] = post
            gen_state["progress"] = 100
        except Exception as e:
            logger.exception("Generation worker error")
            gen_state["error"] = str(e)
            gen_state["message"] = f"Error: {e}"
        finally:
            gen_state["running"] = False

    threading.Thread(target=_worker, daemon=True).start()


@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    use_tts = (
        bool(req.tts_enabled)
        if req.tts_enabled is not None
        else get_setting("tts_enabled", "false") == "true"
    )

    if use_tts:
        if not resolve_voice_region(requested_region=req.voice_region):
            raise HTTPException(
                status_code=400,
                detail="Voice Hook TTS is enabled, but no UK or US voice clip has been uploaded yet. Upload a sample in Admin first.",
            )
        try:
            pending_post = enqueue_tts_post(
                track_id=req.track_id,
                template=req.template,
                era_appropriate=req.era_appropriate,
                force_wildcard=req.force_wildcard,
                publish=req.publish,
                auto_queue=req.auto_queue,
                voice_region=req.voice_region,
                custom_hook_text=req.custom_hook_text,
            )
            tts_worker.start()
            tts_worker.wake()
            return {"started": True, "queued_tts": True, "post": pending_post}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    with gen_lock:
        if gen_state["running"]:
            raise HTTPException(status_code=409, detail="A generation job is already running.")
        gen_state["running"] = True
        gen_state["progress"] = 5
        gen_state["message"] = "Starting pipeline..."
        gen_state["error"] = None
        gen_state["last_post"] = None

    _start_generation_worker(
        track_id=req.track_id,
        template=req.template,
        era_appropriate=req.era_appropriate,
        force_wildcard=req.force_wildcard,
        publish=req.publish,
        auto_queue=req.auto_queue,
    )
    return {"started": True, "queued_tts": False}


@app.get("/api/generate/status")
def api_generate_status():
    return gen_state


@app.get("/api/posts")
def api_posts(limit: int = Query(50, le=200)):
    posts = get_recent_posts(limit=limit)
    for p in posts:
        if p.get("video_path"):
            p["video_url"] = f"/output/{os.path.basename(p['video_path'])}"
        else:
            p["video_url"] = None
    return {"posts": posts}


def _safe_remove_file_under(file_path: Optional[str], allowed_dir: str) -> bool:
    """Safely remove file_path only if it exists and resolves inside allowed_dir."""
    if not file_path:
        return False
    try:
        abs_file = os.path.realpath(file_path)
        abs_dir = os.path.realpath(allowed_dir)
        if os.path.commonpath([abs_file, abs_dir]) == abs_dir and os.path.isfile(abs_file):
            os.remove(abs_file)
            return True
    except OSError as e:
        logger.warning(f"Could not remove file {file_path}: {e}")
    return False


@app.delete("/api/posts/{post_id}")
def api_delete_post(post_id: int):
    """
    Delete a render/post record from History, remove its rendered MP4 (and any
    per-post cached TTS clip) from disk, and unlock the track so it can be
    re-rendered.
    """
    post = get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    if post.get("status") in ("rendering", "tts_processing"):
        raise HTTPException(status_code=409, detail="Cannot delete a post while it is actively rendering.")
    if post.get("status") == "publishing" or (publish_state["running"] and publish_state["post_id"] == post_id):
        raise HTTPException(status_code=409, detail="Cannot delete a post while it is uploading to TikTok.")

    deleted = delete_post(post_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Post not found.")

    video_removed = _safe_remove_file_under(deleted.get("video_path"), OUTPUT_DIR)
    tts_removed = _safe_remove_file_under(deleted.get("tts_audio_path"), TTS_CACHE_DIR)

    if gen_state.get("last_post") and gen_state["last_post"].get("id") == post_id:
        gen_state["last_post"] = None

    return {
        "ok": True,
        "deleted_id": post_id,
        "file_removed": video_removed,
        "tts_removed": tts_removed,
        "track_unlocked": True,
    }


@app.post("/api/posts/{post_id}/rerender")
def api_rerender_post(post_id: int, req: Optional[GenerateRequest] = None):
    """
    Atomically delete an existing render from History (removing its MP4 and cached
    TTS audio) and immediately start a fresh render for the same track using the
    requested/current Studio settings.
    """
    req = req or GenerateRequest()

    post = get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    if post.get("status") in ("rendering", "tts_processing"):
        raise HTTPException(status_code=409, detail="Cannot re-render a post while it is actively rendering.")
    if post.get("status") == "publishing" or (publish_state["running"] and publish_state["post_id"] == post_id):
        raise HTTPException(status_code=409, detail="Cannot re-render a post while it is uploading to TikTok.")

    track_id = post.get("track_id")
    if not track_id or not get_track_by_id(track_id):
        track_id = add_track(
            artist=post["artist"],
            title=post["title"],
            year=post["year"],
        )
    if not track_id:
        raise HTTPException(status_code=404, detail="Associated track not found in catalog.")

    use_tts = (
        bool(req.tts_enabled)
        if req.tts_enabled is not None
        else get_setting("tts_enabled", "false") == "true"
    )

    if use_tts:
        if not resolve_voice_region(requested_region=req.voice_region):
            raise HTTPException(
                status_code=400,
                detail="Voice Hook TTS is enabled, but no UK or US voice clip has been uploaded yet. Upload a sample in Admin first.",
            )

        deleted = delete_post(post_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Post not found.")

        video_removed = _safe_remove_file_under(deleted.get("video_path"), OUTPUT_DIR)
        tts_removed = _safe_remove_file_under(deleted.get("tts_audio_path"), TTS_CACHE_DIR)
        if gen_state.get("last_post") and gen_state["last_post"].get("id") == post_id:
            gen_state["last_post"] = None

        try:
            pending_post = enqueue_tts_post(
                track_id=track_id,
                template=req.template,
                era_appropriate=req.era_appropriate,
                force_wildcard=False,
                publish=req.publish,
                auto_queue=req.auto_queue,
                voice_region=req.voice_region,
                custom_hook_text=req.custom_hook_text,
            )
            tts_worker.start()
            tts_worker.wake()
            return {
                "started": True,
                "queued_tts": True,
                "deleted_id": post_id,
                "track_id": track_id,
                "file_removed": video_removed,
                "tts_removed": tts_removed,
                "post": pending_post,
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    with gen_lock:
        if gen_state["running"]:
            raise HTTPException(status_code=409, detail="A generation job is already running.")
        gen_state["running"] = True
        gen_state["progress"] = 5
        gen_state["message"] = "Starting re-render pipeline..."
        gen_state["error"] = None
        gen_state["last_post"] = None

    try:
        deleted = delete_post(post_id)
        if not deleted:
            gen_state["running"] = False
            raise HTTPException(status_code=404, detail="Post not found.")

        video_removed = _safe_remove_file_under(deleted.get("video_path"), OUTPUT_DIR)
        tts_removed = _safe_remove_file_under(deleted.get("tts_audio_path"), TTS_CACHE_DIR)

        _start_generation_worker(
            track_id=track_id,
            template=req.template,
            era_appropriate=req.era_appropriate,
            force_wildcard=False,
            publish=req.publish,
            auto_queue=req.auto_queue,
        )
    except HTTPException:
        raise
    except Exception as exc:
        gen_state["running"] = False
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "started": True,
        "queued_tts": False,
        "deleted_id": post_id,
        "track_id": track_id,
        "file_removed": video_removed,
        "tts_removed": tts_removed,
    }


@app.post("/api/posts/{post_id}/publish")
def api_publish_post(post_id: int, auto_publish: Optional[bool] = None):
    post = get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    if not post.get("video_path") or not os.path.exists(post["video_path"]):
        raise HTTPException(status_code=400, detail="Video file missing on disk.")

    if post.get("status") == "publishing":
        raise HTTPException(status_code=409, detail="This post is already being uploaded to TikTok.")
    if post.get("status") == "posted":
        raise HTTPException(status_code=409, detail="This post has already been published to TikTok.")

    health = check_cookie_health()
    if not health["healthy"]:
        raise HTTPException(status_code=400, detail=health["reason"])

    if auto_publish is None:
        settings = get_all_settings()
        auto_publish = settings.get("auto_publish", "false") == "true"

    label = f"{post['artist']} - {post['title']}"
    with publish_lock:
        if publish_state["running"]:
            raise HTTPException(status_code=409, detail="A TikTok upload is already in progress.")
        claimed = claim_post_for_publish(post_id, allow_republish=False)
        if not claimed:
            raise HTTPException(
                status_code=409,
                detail="Post is already uploading or has already been published to TikTok.",
            )
        publish_state["running"] = True
        publish_state["post_id"] = post_id
        publish_state["label"] = label
        publish_state["progress"] = 5
        publish_state["message"] = f"Starting TikTok upload for {label}..."
        publish_state["status"] = None
        publish_state["error"] = None

    caption = build_caption(post["artist"], post["title"], post["year"], post.get("chart_info", ""))
    video_path = post["video_path"]

    def _worker():
        def _cb(msg: str, pct: int):
            publish_state["message"] = msg
            publish_state["progress"] = pct

        try:
            res = upload_to_tiktok(
                video_path,
                caption,
                auto_publish=auto_publish,
                progress_cb=_cb,
            )
            update_post_status(post_id, res["status"])
            publish_state["status"] = res["status"]
            publish_state["message"] = res["message"]
            publish_state["progress"] = 100
        except Exception as e:
            logger.exception("TikTok publish worker error")
            update_post_status(post_id, "failed", error_message=str(e))
            publish_state["status"] = "failed"
            publish_state["error"] = str(e)
            publish_state["message"] = f"Error: {e}"
        finally:
            publish_state["running"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True, "post_id": post_id}


@app.get("/api/publish/status")
def api_publish_status():
    return publish_state


def _attach_video_urls(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for item in items:
        if item.get("video_path"):
            item["video_url"] = f"/output/{os.path.basename(item['video_path'])}"
        else:
            item["video_url"] = None
    return items


@app.get("/api/queue")
def api_get_queue():
    items = _attach_video_urls(get_queued_posts())
    return {
        "queue": items,
        "next_scheduled_run": daemon.next_run_info,
    }


@app.post("/api/posts/{post_id}/queue")
def api_queue_post(post_id: int, front: bool = False):
    post = get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    if not post.get("video_path") or not os.path.exists(post["video_path"]):
        raise HTTPException(status_code=400, detail="Video file missing on disk.")

    pos = queue_post(post_id, front=front)
    if pos is None:
        raise HTTPException(status_code=404, detail="Post not found.")
    items = _attach_video_urls(get_queued_posts())
    return {
        "ok": True,
        "post_id": post_id,
        "queue_position": pos,
        "queue_length": len(items),
        "queue": items,
    }


@app.post("/api/queue/reorder")
def api_reorder_queue(req: QueueReorderRequest):
    items = _attach_video_urls(reorder_queue(req.post_ids))
    return {"ok": True, "queue": items}


@app.delete("/api/queue/{post_id}")
def api_remove_from_queue(post_id: int):
    ok = remove_from_queue(post_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Post is not currently in the queue.")
    items = _attach_video_urls(get_queued_posts())
    return {"ok": True, "queue": items}


@app.delete("/api/queue")
def api_clear_queue():
    cleared = clear_queue()
    return {"ok": True, "cleared": cleared, "queue": []}


@app.get("/api/tracks")
def api_tracks(q: str = "", page: int = 1, limit: int = 200):
    q_lower = q.strip().lower()
    if q_lower:
        tracks = get_all_tracks(limit=2000)
        results = []
        for t in tracks:
            if (
                q_lower not in t["artist"].lower()
                and q_lower not in t["title"].lower()
                and q_lower not in str(t["year"])
            ):
                continue
            t["posted"] = is_track_posted(t["artist"], t["title"])
            t["blacklisted"] = is_blacklisted(t["artist"], t["title"])
            results.append(t)
            if len(results) >= limit:
                break
        return {
            "tracks": results,
            "page": 1,
            "total_pages": 1,
            "total_tracks": len(results),
            "batch_num": None,
            "search_active": True,
        }

    batches = get_track_batches()
    total_pages = max(1, len(batches))
    safe_page = max(1, min(int(page), total_pages))
    target_batch = batches[safe_page - 1] if batches else 1
    tracks = get_tracks_by_batch(target_batch) if batches else []

    results = []
    for t in tracks:
        t["posted"] = is_track_posted(t["artist"], t["title"])
        t["blacklisted"] = is_blacklisted(t["artist"], t["title"])
        results.append(t)

    return {
        "tracks": results,
        "page": safe_page,
        "total_pages": total_pages,
        "total_tracks": count_tracks(),
        "batch_num": target_batch,
        "search_active": False,
    }


@app.post("/api/tracks/load-new")
def api_load_new_tracks(count: int = Query(default=25, ge=1, le=50)):
    result = load_new_chart_batch(count=count)
    return result


@app.post("/api/tracks")
def api_add_track(req: TrackCreateRequest):
    tid = add_track(
        artist=req.artist,
        title=req.title,
        year=req.year,
        chart_peak=req.chart_peak,
        chart_region=req.chart_region,
        is_wildcard=req.is_wildcard,
        genre=req.genre
    )
    return {"ok": True, "track_id": tid}


@app.get("/api/blacklist")
def api_get_blacklist():
    return {"blacklist": get_blacklist()}


@app.post("/api/blacklist")
def api_add_blacklist(req: BlacklistRequest):
    bid = add_to_blacklist(req.artist, req.title, req.reason)
    return {"ok": True, "id": bid}


@app.delete("/api/blacklist/{blacklist_id}")
def api_delete_blacklist(blacklist_id: int):
    ok = remove_from_blacklist(blacklist_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Blacklist entry not found.")
    return {"ok": True}


@app.get("/api/settings")
def api_get_settings():
    settings = get_all_settings()
    # Mask cookies for display security
    raw_cookies = settings.get("cookies_json", "")
    settings["has_cookies"] = "true" if raw_cookies else "false"
    if raw_cookies:
        settings["cookies_json"] = "***CONFIGURED***"
    return {"settings": settings}


@app.post("/api/settings")
def api_update_settings(req: SettingsUpdateRequest):
    for k, v in req.settings.items():
        if k == "cookies_json" and v == "***CONFIGURED***":
            continue
        set_setting(k, str(v))
    daemon.reschedule_next()
    return {
        "ok": True,
        "next_scheduled_run": daemon.next_run_info,
        "next_analytics_run": daemon.next_analytics_run_info,
    }


@app.post("/api/cookies")
def api_update_cookies(req: CookiesUpdateRequest):
    raw = req.cookies_json.strip()
    if not raw:
        set_setting("cookies_json", "")
        health = check_cookie_health()
        return {"ok": health["healthy"], "health": health}

    cleaned = raw.lstrip("\ufeff").strip()
    if cleaned.startswith("[") or cleaned.startswith("{"):
        try:
            cookies = parse_cookies_json_secure(cleaned)
        except CookieParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    else:
        cookies = parse_cookies(cleaned)
        if not cookies:
            raise HTTPException(status_code=400, detail="No valid TikTok cookies found.")

    set_setting("cookies_json", json.dumps(cookies))
    health = check_cookie_health()
    return {"ok": health["healthy"], "health": health}


@app.post("/api/cookies/import")
def api_import_cookies_file(req: CookieFileImportRequest):
    raw_name = (req.filename or "").strip()
    if "\x00" in raw_name:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    safe_name = os.path.basename(raw_name.replace("\\", "/"))
    if not safe_name or not safe_name.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="Only .json cookie files are supported.")

    try:
        cookies = parse_cookies_json_secure(req.content)
    except CookieParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    set_setting("cookies_json", json.dumps(cookies))
    health = check_cookie_health()
    return {
        "ok": health["healthy"],
        "health": health,
        "imported_count": len(cookies),
        "filename": safe_name,
    }


# --- TTS Voice Profiles & Queue Recovery Endpoints ---

@app.get("/api/tts/status")
def api_tts_status():
    return {
        "worker": get_tts_worker_health(),
        "profiles": list_voice_profiles(),
        "queue": tts_worker.status(),
        "tts_enabled": get_setting("tts_enabled", "false") == "true",
    }


@app.get("/api/tts/voices")
def api_tts_voices():
    return {"profiles": list_voice_profiles()}


@app.post("/api/tts/voices/{region}")
async def api_upload_voice_sample(
    region: str,
    file: UploadFile = File(...),
    denoise: bool = Form(False),
):
    raw = await file.read()
    try:
        clip = save_voice_sample(region, file.filename or "voice.wav", raw, denoise=denoise)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "clip": clip, "profiles": list_voice_profiles()}


@app.delete("/api/tts/voices/{region}/{filename}")
def api_delete_voice_sample(region: str, filename: str):
    try:
        deleted = delete_voice_sample(region, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="Voice clip not found.")
    return {"ok": True, "profiles": list_voice_profiles()}


@app.post("/api/tts/test")
def api_test_voice(req: TTSTestRequest):
    region = (req.region or "uk").strip().lower()
    sample_text = (req.text or "").strip() or (
        "Right then, I bet this one takes you straight back to 1985! Turn it up on Gen X seventy-one, what a brilliant tune!"
        if region == "uk"
        else "Where were you in 1985 when you heard this first? Turn it up on Gen X seventy-one!"
    )
    os.makedirs(TTS_CACHE_DIR, exist_ok=True)
    out_name = f"test_{region}_{int(__import__('time').time())}.wav"
    out_path = os.path.join(TTS_CACHE_DIR, out_name)
    tts_speed = float(get_setting("tts_speed", "1.05") or "1.05")
    try:
        res = synthesize_hook_audio(text=sample_text, region=region, output_path=out_path, speed=tts_speed)
        return {
            "ok": True,
            "region": region,
            "duration": res["duration"],
            "elapsed_sec": res["elapsed_sec"],
            "audio_url": f"/tts-cache/{out_name}",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/posts/{post_id}/retry-tts")
def api_retry_tts(post_id: int):
    post = get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    update_post_fields(post_id, status="tts_pending", error_message=None, tts_enabled=1)
    tts_worker.start()
    tts_worker.wake()
    return {"ok": True, "post": get_post_by_id(post_id)}


@app.post("/api/posts/{post_id}/render-no-tts")
def api_render_without_tts(post_id: int):
    post = get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    claimed = claim_post_for_processing(
        post_id,
        allowed_statuses=("tts_pending", "tts_failed", "failed"),
        target_status="rendering",
    )
    if not claimed:
        raise HTTPException(
            status_code=409,
            detail=f"Post cannot be rendered without TTS from status '{post.get('status')}'.",
        )
    update_post_fields(post_id, error_message=None, tts_enabled=0)

    def _fallback_worker():
        try:
            process_pending_post(post_id, render_without_tts=True)
        except Exception as exc:
            logger.exception("Fallback render without TTS failed for post #%s", post_id)
            update_post_status(post_id, "failed", error_message=str(exc))

    threading.Thread(target=_fallback_worker, daemon=True).start()
    return {"ok": True, "started": True, "post_id": post_id}


# --- Post Analytics & Local LLM Daily Report Endpoints ---

@app.get("/api/analytics/overview")
def api_analytics_overview():
    summary = build_analytics_summary(period="daily")
    weekly_summary = build_analytics_summary(period="weekly")
    monthly_summary = build_analytics_summary(period="monthly")
    reports = get_analytics_reports(limit=30)
    latest_report = reports[0] if reports else None
    active_feedback = get_active_feedback_weights()
    learning_summary = build_learning_summary()
    llm_health = get_llm_health()
    settings = get_all_settings()
    return {
        "totals": summary["totals"],
        "top_posts": summary["top_posts"],
        "breakdowns": summary["breakdowns"],
        "posts": summary["posts"],
        "period_stats": {
            "weekly": weekly_summary["period_comparison"],
            "monthly": monthly_summary["period_comparison"],
        },
        "learning": learning_summary,
        "latest_report": latest_report,
        "reports": reports,
        "active_feedback": active_feedback,
        "llm": llm_health,
        "next_analytics_run": daemon.next_analytics_run_info,
        "analytics_job": analytics_state,
        "config": {
            "analytics_report_enabled": settings.get("analytics_report_enabled", "true") == "true",
            "analytics_report_time": settings.get("analytics_report_time", "02:00"),
            "analytics_weekly_enabled": settings.get("analytics_weekly_enabled", "true") == "true",
            "analytics_monthly_enabled": settings.get("analytics_monthly_enabled", "true") == "true",
            "analytics_auto_sync": settings.get("analytics_auto_sync", "true") == "true",
            "analytics_feedback_enabled": settings.get("analytics_feedback_enabled", "true") == "true",
            "analytics_feedback_strength": settings.get("analytics_feedback_strength", "0.5"),
            "llm_model": settings.get("llm_model", "qwen3.5:2b"),
        },
    }


@app.post("/api/analytics/posts/{post_id}")
def api_update_post_analytics(post_id: int, req: PostAnalyticsUpdateRequest):
    post = get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    saved = upsert_post_analytics(
        post_id=post_id,
        views=req.views,
        likes=req.likes,
        comments=req.comments,
        shares=req.shares,
        saves=req.saves,
        avg_watch_time_sec=req.avg_watch_time_sec,
        completion_rate=req.completion_rate,
        tiktok_url=req.tiktok_url,
        source="manual",
    )
    return {"ok": True, "analytics": saved}


@app.post("/api/analytics/import")
def api_import_analytics(req: AnalyticsImportRequest):
    raw_name = (req.filename or "").strip()
    if not raw_name or "\x00" in raw_name:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if not (raw_name.lower().endswith(".csv") or raw_name.lower().endswith(".json")):
        raise HTTPException(status_code=400, detail="Only .csv or .json analytics files are supported.")
    try:
        res = import_analytics_file(req.content, raw_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Import failed: {exc}")
    return res


@app.post("/api/analytics/sync")
def api_sync_analytics():
    with analytics_lock:
        if analytics_state["running"]:
            raise HTTPException(status_code=409, detail="An analytics task is already running.")
        analytics_state["running"] = True
        analytics_state["task"] = "sync"
        analytics_state["progress"] = 5
        analytics_state["message"] = "Starting TikTok Studio analytics sync..."
        analytics_state["error"] = None

    def _worker():
        def _cb(msg: str, pct: int):
            analytics_state["message"] = msg
            analytics_state["progress"] = pct

        try:
            res = sync_tiktok_analytics(progress_cb=_cb)
            analytics_state["message"] = res.get("message", "Sync complete.")
            analytics_state["progress"] = 100
            if not res.get("ok"):
                analytics_state["error"] = res.get("message")
        except Exception as exc:
            logger.exception("TikTok analytics sync error")
            analytics_state["error"] = str(exc)
            analytics_state["message"] = f"Sync error: {exc}"
        finally:
            analytics_state["running"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True, "task": "sync"}


@app.post("/api/analytics/analyze")
def api_run_analytics_report(req: Optional[AnalyticsRunRequest] = None):
    body = req or AnalyticsRunRequest()
    period = body.period if body.period in ("daily", "weekly", "monthly") else "daily"
    with analytics_lock:
        if analytics_state["running"]:
            raise HTTPException(status_code=409, detail="An analytics task is already running.")
        analytics_state["running"] = True
        analytics_state["task"] = f"analyze:{period}"
        analytics_state["progress"] = 5
        analytics_state["message"] = f"Starting local LLM {period} analytics report..."
        analytics_state["error"] = None

    def _worker():
        def _cb(msg: str, pct: int):
            analytics_state["message"] = msg
            analytics_state["progress"] = pct

        try:
            res = run_analytics_report(
                period=period,
                sync_first=body.sync_first,
                auto_apply_feedback=body.auto_apply_feedback,
                progress_cb=_cb,
            )
            rep = res.get("report") or {}
            analytics_state["last_report_id"] = rep.get("id")
            analytics_state["progress"] = 100
        except Exception as exc:
            logger.exception("Analytics LLM report error")
            analytics_state["error"] = str(exc)
            analytics_state["message"] = f"Analysis error: {exc}"
        finally:
            analytics_state["running"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True, "task": f"analyze:{period}", "period": period}


@app.get("/api/analytics/analyze/status")
def api_analytics_job_status():
    return analytics_state


@app.get("/api/analytics/reports")
def api_analytics_reports(limit: int = Query(30, le=100), period: Optional[str] = None):
    return {"reports": get_analytics_reports(limit=limit, period_type=period)}


@app.post("/api/analytics/reports/{report_id}/apply")
def api_apply_analytics_report(report_id: int, req: Optional[ApplyFeedbackRequest] = None):
    body = req or ApplyFeedbackRequest()
    try:
        res = apply_report_feedback(report_id, apply_wildcard=body.apply_wildcard)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return res


@app.get("/api/llm/status")
def api_llm_status():
    return get_llm_health()


@app.post("/api/llm/pull")
def api_llm_pull(req: Optional[LLMPullRequest] = None):
    body = req or LLMPullRequest()
    with analytics_lock:
        if analytics_state["running"]:
            raise HTTPException(status_code=409, detail="An analytics/LLM task is already running.")
        analytics_state["running"] = True
        analytics_state["task"] = "pull"
        analytics_state["progress"] = 15
        analytics_state["message"] = f"Pulling local LLM '{body.model or get_setting('llm_model', 'qwen3.5:2b')}'..."
        analytics_state["error"] = None

    def _worker():
        try:
            res = ensure_model_pulled(model=body.model)
            if res.get("already_installed"):
                analytics_state["message"] = f"Model '{res['model']}' is already installed and ready."
            else:
                analytics_state["message"] = f"Pulled '{res['model']}' in {res.get('elapsed_sec', 0)}s!"
            analytics_state["progress"] = 100
        except Exception as exc:
            logger.exception("LLM model pull error")
            analytics_state["error"] = str(exc)
            analytics_state["message"] = f"Pull failed: {exc}"
        finally:
            analytics_state["running"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True, "task": "pull"}


# --- Static Files & PWA Routes ---

app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")
app.mount("/tts-cache", StaticFiles(directory=TTS_CACHE_DIR), name="tts-cache")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/manifest.json")
def pwa_manifest():
    return FileResponse(os.path.join(WEB_DIR, "manifest.json"), media_type="application/manifest+json")


@app.get("/sw.js")
def pwa_sw():
    return FileResponse(os.path.join(WEB_DIR, "sw.js"), media_type="application/javascript")


@app.get("/logo.png")
def pwa_logo():
    return FileResponse(os.path.join(PROJECT_ROOT, "genxlogofinal.png"), media_type="image/png")


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))
