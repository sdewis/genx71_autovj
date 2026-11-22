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
    get_all_tracks, add_track, add_to_blacklist, remove_from_blacklist,
    get_blacklist, get_all_settings, set_setting, get_setting, get_post_by_id,
    update_post_status, update_post_fields, is_track_posted, is_blacklisted,
    queue_post, get_queued_posts, remove_from_queue, reorder_queue, clear_queue,
    delete_post
)
from core.charts import seed_charts_if_empty
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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


# --- API Endpoints ---

@app.get("/api/status")
def api_status():
    total_tracks = count_tracks()
    avail_all = len(get_available_tracks())
    avail_wild = len(get_available_tracks(is_wildcard=True))
    posts = get_recent_posts(limit=500)
    posted_count = sum(1 for p in posts if p["status"] in ("posted", "draft"))
    queued_count = len(get_queued_posts())
    tts_pending_count = sum(1 for p in posts if p["status"] in ("tts_pending", "rendering"))
    cookie_health = check_cookie_health()

    return {
        "daemon_running": daemon.running,
        "next_scheduled_run": daemon.next_run_info,
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
        "tts_queue": tts_worker.status(),
    }


@app.get("/api/styles")
def api_styles():
    from core.styles import get_style_metadata
    return {"styles": get_style_metadata()}


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

    def _worker():
        def _cb(msg: str, pct: int):
            gen_state["message"] = msg
            gen_state["progress"] = pct

        try:
            post = generate_post_video(
                track_id=req.track_id,
                template=req.template,
                era_appropriate=req.era_appropriate,
                force_wildcard=req.force_wildcard,
                publish=req.publish,
                auto_queue=req.auto_queue,
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
    if post.get("status") == "rendering":
        raise HTTPException(status_code=409, detail="Cannot delete a post while it is actively rendering.")
    if publish_state["running"] and publish_state["post_id"] == post_id:
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


@app.post("/api/posts/{post_id}/publish")
def api_publish_post(post_id: int, auto_publish: Optional[bool] = None):
    post = get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    if not post.get("video_path") or not os.path.exists(post["video_path"]):
        raise HTTPException(status_code=400, detail="Video file missing on disk.")

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
def api_tracks(q: str = "", limit: int = 200):
    tracks = get_all_tracks(limit=500)
    q_lower = q.strip().lower()
    results = []
    for t in tracks:
        if q_lower and q_lower not in t["artist"].lower() and q_lower not in t["title"].lower() and q_lower not in str(t["year"]):
            continue
        t["posted"] = is_track_posted(t["artist"], t["title"])
        t["blacklisted"] = is_blacklisted(t["artist"], t["title"])
        results.append(t)
        if len(results) >= limit:
            break
    return {"tracks": results}


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
    return {"ok": True, "next_scheduled_run": daemon.next_run_info}


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
    update_post_fields(post_id, status="rendering", error_message=None, tts_enabled=0)

    def _fallback_worker():
        try:
            process_pending_post(post_id, render_without_tts=True)
        except Exception as exc:
            logger.exception("Fallback render without TTS failed for post #%s", post_id)
            update_post_status(post_id, "failed", error_message=str(exc))

    threading.Thread(target=_fallback_worker, daemon=True).start()
    return {"ok": True, "started": True, "post_id": post_id}


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
