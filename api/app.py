"""
FastAPI Backend Server & PWA Host for GenX71 AutoVJ.
Provides full admin, scheduling, manual generation, video preview,
cookie management, and deduplication history for both Samsung S24 Ultra PWA
and Linux desktop browsers.
"""

import os
import json
import threading
import logging
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.db import (
    init_db, count_tracks, get_available_tracks, get_recent_posts,
    get_all_tracks, add_track, add_to_blacklist, remove_from_blacklist,
    get_blacklist, get_all_settings, set_setting, get_post_by_id,
    update_post_status, is_track_posted, is_blacklisted
)
from core.charts import seed_charts_if_empty
from core.pipeline import generate_post_video
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

daemon = AutoVJDaemon()

# Live generation job state
gen_state: Dict[str, Any] = {
    "running": False,
    "progress": 0,
    "message": "Idle",
    "last_post": None,
    "error": None
}
gen_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_charts_if_empty()
    daemon.start()
    yield
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


# --- API Endpoints ---

@app.get("/api/status")
def api_status():
    total_tracks = count_tracks()
    avail_all = len(get_available_tracks())
    avail_wild = len(get_available_tracks(is_wildcard=True))
    posts = get_recent_posts(limit=500)
    posted_count = sum(1 for p in posts if p["status"] in ("posted", "draft"))
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
            "published_count": posted_count
        },
        "generation": gen_state
    }


@app.post("/api/generate")
def api_generate(req: GenerateRequest):
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
    return {"started": True}


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

    caption = build_caption(post["artist"], post["title"], post["year"], post.get("chart_info", ""))
    try:
        res = upload_to_tiktok(post["video_path"], caption, auto_publish=auto_publish)
        update_post_status(post_id, res["status"])
        return {"ok": True, "status": res["status"], "message": res["message"]}
    except Exception as e:
        update_post_status(post_id, "failed", error_message=str(e))
        raise HTTPException(status_code=500, detail=str(e))


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


# --- Static Files & PWA Routes ---

app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")
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
