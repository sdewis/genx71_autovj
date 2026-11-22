"""
Post Analytics Engine, TikTok Studio Metrics Sync, CSV/JSON Importer,
and Daily Local-LLM Report Orchestrator for GenX71 AutoVJ.
"""

import io
import re
import csv
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from core.db import (
    init_db,
    get_setting,
    set_setting,
    get_post_analytics_rows,
    upsert_post_analytics,
    save_analytics_report,
    get_analytics_reports,
    get_analytics_report_by_id,
    update_report_outcome_evaluation,
    get_evaluated_learning_history,
    mark_report_applied,
)
from core.llm_analyst import analyze_posts_with_llm

logger = logging.getLogger("genx71.analytics")


def _normalize_match_text(text: str) -> str:
    cleaned = (text or "").lower().replace("'", "").replace("\u2019", "")
    return re.sub(r"[^a-z0-9]+", " ", cleaned).strip()


def _parse_human_number(raw: Any) -> int:
    """Parse TikTok formatted counts like '12.4K', '1.2M', '3,450', or integers."""
    if raw is None or isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        return max(0, int(round(raw)))
    s = str(raw).strip().upper().replace(",", "")
    if not s:
        return 0
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMB])?$", s)
    if not m:
        digits = re.sub(r"[^0-9]", "", s)
        return int(digits) if digits else 0
    num = float(m.group(1))
    suffix = m.group(2)
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return max(0, int(round(num * mult)))


def _parse_float(raw: Any) -> float:
    if raw is None or isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace("%", "").replace("s", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _group_breakdown(rows: List[Dict[str, Any]], key_fn: Callable[[Dict[str, Any]], str]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        label = key_fn(r)
        if label:
            buckets[label].append(r)

    summary = []
    for label, items in buckets.items():
        n = len(items)
        views = sum(int(i.get("views", 0)) for i in items)
        likes = sum(int(i.get("likes", 0)) for i in items)
        avg_views = round(views / n, 1) if n else 0.0
        avg_eng = round(sum(float(i.get("engagement_rate", 0.0)) for i in items) / n, 2) if n else 0.0
        avg_comp = round(sum(float(i.get("completion_rate", 0.0)) for i in items) / n, 1) if n else 0.0
        avg_score = round(sum(float(i.get("performance_score", 0.0)) for i in items) / n, 2) if n else 0.0
        summary.append({
            "label": label,
            "count": n,
            "total_views": views,
            "total_likes": likes,
            "avg_views": avg_views,
            "avg_engagement_rate": avg_eng,
            "avg_completion_rate": avg_comp,
            "avg_score": avg_score,
        })
    summary.sort(key=lambda x: (x["avg_score"], x["avg_views"]), reverse=True)
    return summary


PERIOD_WINDOW_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
}


def _parse_iso_dt(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _post_timestamp(row: Dict[str, Any]) -> Optional[datetime]:
    return _parse_iso_dt(row.get("posted_at")) or _parse_iso_dt(row.get("created_at"))


def _pct_delta(curr: float, prev: float) -> Optional[float]:
    if prev <= 0:
        return None
    return round(((curr - prev) / prev) * 100.0, 1)


def _window_stats(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(items)
    if n == 0:
        return {"count": 0, "total_views": 0, "avg_views": 0.0, "avg_engagement": 0.0, "avg_completion": 0.0, "avg_score": 0.0}
    total_views = sum(int(i.get("views", 0)) for i in items)
    return {
        "count": n,
        "total_views": total_views,
        "avg_views": round(total_views / n, 1),
        "avg_engagement": round(sum(float(i.get("engagement_rate", 0.0)) for i in items) / n, 2),
        "avg_completion": round(sum(float(i.get("completion_rate", 0.0)) for i in items) / n, 1),
        "avg_score": round(sum(float(i.get("performance_score", 0.0)) for i in items) / n, 2),
    }


def build_analytics_summary(
    db_path: Optional[str] = None,
    period: str = "daily",
    reference_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Aggregate existing post analytics into KPIs, category breakdowns, rankings,
    and period-over-period comparisons (daily, weekly 7d vs prior 7d, monthly 30d vs prior 30d).
    """
    init_db(db_path)
    period_key = period if period in PERIOD_WINDOW_DAYS else "daily"
    window_days = PERIOD_WINDOW_DAYS[period_key]
    now = (reference_dt or datetime.now()).replace(tzinfo=None)

    rows = get_post_analytics_rows(limit=500, db_path=db_path)
    all_tracked = [r for r in rows if r.get("has_metrics") or int(r.get("views", 0)) > 0]

    # Partition into current window vs previous window for weekly/monthly trends
    curr_start = now - timedelta(days=window_days)
    prev_start = now - timedelta(days=window_days * 2)
    current_window_posts: List[Dict[str, Any]] = []
    previous_window_posts: List[Dict[str, Any]] = []
    for r in all_tracked:
        ts = _post_timestamp(r)
        if ts is None:
            continue
        if ts >= curr_start:
            current_window_posts.append(r)
        elif ts >= prev_start:
            previous_window_posts.append(r)

    used_all_time_fallback = False
    if period_key in ("weekly", "monthly"):
        if current_window_posts:
            tracked = current_window_posts
        else:
            tracked = all_tracked
            used_all_time_fallback = bool(all_tracked)
    else:
        tracked = all_tracked

    curr_stats = _window_stats(current_window_posts)
    prev_stats = _window_stats(previous_window_posts)
    period_comparison = {
        "period": period_key,
        "window_days": window_days,
        "current_posts": curr_stats["count"],
        "previous_posts": prev_stats["count"],
        "current_total_views": curr_stats["total_views"],
        "previous_total_views": prev_stats["total_views"],
        "current_avg_views": curr_stats["avg_views"],
        "previous_avg_views": prev_stats["avg_views"],
        "views_delta_pct": _pct_delta(curr_stats["avg_views"], prev_stats["avg_views"]),
        "current_avg_engagement": curr_stats["avg_engagement"],
        "previous_avg_engagement": prev_stats["avg_engagement"],
        "engagement_delta_pct": _pct_delta(curr_stats["avg_engagement"], prev_stats["avg_engagement"]),
        "current_avg_completion": curr_stats["avg_completion"],
        "previous_avg_completion": prev_stats["avg_completion"],
        "current_avg_score": curr_stats["avg_score"],
        "previous_avg_score": prev_stats["avg_score"],
        "score_delta_pct": _pct_delta(curr_stats["avg_score"], prev_stats["avg_score"]),
        "used_all_time_fallback": used_all_time_fallback,
    }

    total_posts = len(rows)
    tracked_count = len(tracked)
    total_views = sum(int(r.get("views", 0)) for r in tracked)
    total_likes = sum(int(r.get("likes", 0)) for r in tracked)
    total_comments = sum(int(r.get("comments", 0)) for r in tracked)
    total_shares = sum(int(r.get("shares", 0)) for r in tracked)
    total_saves = sum(int(r.get("saves", 0)) for r in tracked)

    avg_eng = (
        round(sum(float(r.get("engagement_rate", 0.0)) for r in tracked) / tracked_count, 2)
        if tracked_count
        else 0.0
    )
    avg_comp = (
        round(sum(float(r.get("completion_rate", 0.0)) for r in tracked) / tracked_count, 1)
        if tracked_count
        else 0.0
    )
    avg_score = (
        round(sum(float(r.get("performance_score", 0.0)) for r in tracked) / tracked_count, 2)
        if tracked_count
        else 0.0
    )

    ranked = sorted(
        tracked,
        key=lambda r: (float(r.get("performance_score", 0.0)), int(r.get("views", 0))),
        reverse=True,
    )
    top_posts = ranked[:10]
    bottom_posts = list(reversed(ranked[-5:])) if len(ranked) > 3 else []

    breakdowns = {
        "by_genre": _group_breakdown(tracked, lambda r: str(r.get("genre") or "Pop/Rock")),
        "by_decade": _group_breakdown(
            tracked, lambda r: f"{(int(r['year']) // 10) * 10}s" if r.get("year") else "Unknown"
        ),
        "by_region": _group_breakdown(tracked, lambda r: str(r.get("chart_region") or "UK")),
        "by_wildcard": _group_breakdown(
            tracked, lambda r: "Wildcard" if int(r.get("is_wildcard") or 0) else "Top 20 Hit"
        ),
        "by_template": _group_breakdown(tracked, lambda r: str(r.get("template_used") or "vinyl")),
        "by_tts": _group_breakdown(
            tracked, lambda r: "Voice Hook TTS" if int(r.get("tts_enabled") or 0) else "Music Only"
        ),
    }

    return {
        "period": period_key,
        "period_comparison": period_comparison,
        "totals": {
            "total_posts": total_posts,
            "tracked_posts": tracked_count,
            "total_views": total_views,
            "total_likes": total_likes,
            "total_comments": total_comments,
            "total_shares": total_shares,
            "total_saves": total_saves,
            "avg_engagement_rate": avg_eng,
            "avg_completion_rate": avg_comp,
            "avg_performance_score": avg_score,
        },
        "top_posts": top_posts,
        "bottom_posts": bottom_posts,
        "breakdowns": breakdowns,
        "posts": rows,
    }


def _match_post_row(
    posts: List[Dict[str, Any]],
    post_id: Optional[int] = None,
    artist: str = "",
    title: str = "",
    caption: str = "",
    tiktok_id: str = "",
) -> Optional[Dict[str, Any]]:
    if post_id is not None:
        for p in posts:
            if int(p["post_id"]) == int(post_id):
                return p

    if tiktok_id:
        for p in posts:
            if p.get("tiktok_id") and str(p["tiktok_id"]) == str(tiktok_id):
                return p

    norm_artist = _normalize_match_text(artist)
    norm_title = _normalize_match_text(title)
    if norm_artist and norm_title:
        for p in posts:
            if (
                _normalize_match_text(p["artist"]) == norm_artist
                and _normalize_match_text(p["title"]) == norm_title
            ):
                return p

    norm_cap = _normalize_match_text(caption or f"{artist} {title}")
    if norm_cap:
        for p in posts:
            p_artist = _normalize_match_text(p["artist"])
            p_title = _normalize_match_text(p["title"])
            if p_artist and p_title and p_artist in norm_cap and p_title in norm_cap:
                return p

    return None


def import_analytics_file(
    content: str,
    filename: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Import post analytics from a CSV or JSON file.
    Matches rows to existing posts by post_id, tiktok_id, artist+title, or caption.
    """
    init_db(db_path)
    posts = get_post_analytics_rows(limit=1000, db_path=db_path)
    lower_name = (filename or "").lower()

    records: List[Dict[str, Any]] = []
    if lower_name.endswith(".json") or content.lstrip().startswith(("[", "{")):
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            records = parsed.get("posts") or parsed.get("items") or parsed.get("data") or []
        elif isinstance(parsed, list):
            records = parsed
        else:
            raise ValueError("JSON analytics file must be an array or object containing 'posts'/'data'.")
    else:
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
        records = [dict(r) for r in reader]

    matched = 0
    unmatched = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        norm = {str(k).strip().lower().replace(" ", "_"): v for k, v in rec.items() if k is not None}

        pid_raw = norm.get("post_id") or norm.get("id")
        pid = None
        if pid_raw is not None and str(pid_raw).strip().isdigit():
            pid = int(str(pid_raw).strip())

        target = _match_post_row(
            posts,
            post_id=pid,
            artist=str(norm.get("artist") or ""),
            title=str(norm.get("title") or norm.get("track") or ""),
            caption=str(norm.get("caption") or norm.get("video_title") or norm.get("description") or ""),
            tiktok_id=str(norm.get("tiktok_id") or norm.get("video_id") or ""),
        )
        if not target:
            unmatched += 1
            continue

        upsert_post_analytics(
            post_id=target["post_id"],
            views=_parse_human_number(norm.get("views") or norm.get("video_views") or norm.get("play_count")),
            likes=_parse_human_number(norm.get("likes") or norm.get("like_count")),
            comments=_parse_human_number(norm.get("comments") or norm.get("comment_count")),
            shares=_parse_human_number(norm.get("shares") or norm.get("share_count")),
            saves=_parse_human_number(norm.get("saves") or norm.get("favorites") or norm.get("bookmarks")),
            avg_watch_time_sec=_parse_float(norm.get("avg_watch_time_sec") or norm.get("average_watch_time") or 0),
            completion_rate=_parse_float(norm.get("completion_rate") or norm.get("watched_full_video") or 0),
            tiktok_url=str(norm.get("tiktok_url") or norm.get("link") or norm.get("video_link") or "").strip() or None,
            source="import",
            db_path=db_path,
        )
        matched += 1

    return {
        "ok": True,
        "matched": matched,
        "unmatched": unmatched,
        "total_rows": len(records),
    }


def _first_present(d: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return None


def _extract_tiktok_videos_from_json(payload: Any) -> List[Dict[str, Any]]:
    """
    Recursively scan a TikTok Studio / Creator / Web API JSON payload and extract
    normalized video metric dicts (caption, tiktok_id, views, likes, comments,
    shares, saves, avg_watch_time_sec, completion_rate, tiktok_url).
    """
    found: List[Dict[str, Any]] = []
    seen_obj_ids = set()

    _ID_KEYS = ("id", "item_id", "itemId", "aweme_id", "awemeId", "video_id", "videoId")
    _TEXT_KEYS = ("desc", "title", "caption", "description", "video_title", "item_title", "name")
    _STAT_CONTAINERS = ("stats", "statsV2", "statistics", "metrics", "metric", "item_metrics")
    _VIEW_KEYS = ("playCount", "play_count", "views", "view_count", "video_views", "vv", "play")
    _LIKE_KEYS = ("diggCount", "digg_count", "likeCount", "like_count", "likes")
    _COMMENT_KEYS = ("commentCount", "comment_count", "comments")
    _SHARE_KEYS = ("shareCount", "share_count", "shares")
    _SAVE_KEYS = ("collectCount", "collect_count", "favoriteCount", "favorite_count", "saves", "bookmarks")
    _WATCH_KEYS = ("avgWatchTime", "avg_watch_time", "average_watch_time", "avg_play_time", "avgTimeWatched")
    _COMP_KEYS = ("completionRate", "completion_rate", "full_video_watched_rate", "watched_full_video_rate")

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 10 or node is None:
            return
        if isinstance(node, list):
            for elem in node:
                _walk(elem, depth + 1)
            return
        if not isinstance(node, dict):
            return
        oid = id(node)
        if oid in seen_obj_ids:
            return
        seen_obj_ids.add(oid)

        # Check if this dict looks like a TikTok video/post record
        raw_text = _first_present(node, _TEXT_KEYS)
        raw_id = _first_present(node, _ID_KEYS)

        stat_src: Dict[str, Any] = {}
        for sk in _STAT_CONTAINERS:
            sub = node.get(sk)
            if isinstance(sub, dict):
                stat_src.update(sub)
        # Merge top-level metric fields if present directly on the video dict
        combined_stats = {**node, **stat_src}

        raw_views = _first_present(combined_stats, _VIEW_KEYS)
        raw_likes = _first_present(combined_stats, _LIKE_KEYS)
        raw_comments = _first_present(combined_stats, _COMMENT_KEYS)
        raw_shares = _first_present(combined_stats, _SHARE_KEYS)
        raw_saves = _first_present(combined_stats, _SAVE_KEYS)
        raw_watch = _first_present(combined_stats, _WATCH_KEYS)
        raw_comp = _first_present(combined_stats, _COMP_KEYS)

        has_any_metric = any(v is not None for v in (raw_views, raw_likes, raw_comments, raw_shares))
        caption = str(raw_text).strip() if isinstance(raw_text, str) else ""
        tid = str(raw_id).strip() if raw_id is not None and re.fullmatch(r"\d{8,25}", str(raw_id).strip()) else ""

        if has_any_metric and (caption or tid):
            comp = _parse_float(raw_comp)
            if 0.0 < comp <= 1.0:
                comp = round(comp * 100.0, 2)
            share_url = _first_present(node, ("share_url", "shareUrl", "video_url", "url", "link"))
            tiktok_url = str(share_url).strip() if isinstance(share_url, str) and "tiktok.com" in share_url else ""
            if not tiktok_url and tid:
                tiktok_url = f"https://www.tiktok.com/@_/video/{tid}"
            found.append({
                "tiktok_id": tid,
                "caption": caption,
                "views": _parse_human_number(raw_views),
                "likes": _parse_human_number(raw_likes),
                "comments": _parse_human_number(raw_comments),
                "shares": _parse_human_number(raw_shares),
                "saves": _parse_human_number(raw_saves),
                "avg_watch_time_sec": _parse_float(raw_watch),
                "completion_rate": comp,
                "tiktok_url": tiktok_url,
            })

        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk(v, depth + 1)

    _walk(payload)

    # Deduplicate by tiktok_id (or normalized caption), keeping the richest entry
    merged: Dict[str, Dict[str, Any]] = {}
    for item in found:
        key = item["tiktok_id"] or _normalize_match_text(item["caption"])
        if not key:
            continue
        prev = merged.get(key)
        if not prev:
            merged[key] = item
        else:
            merged[key] = {
                "tiktok_id": prev["tiktok_id"] or item["tiktok_id"],
                "caption": prev["caption"] if len(prev["caption"]) >= len(item["caption"]) else item["caption"],
                "views": max(prev["views"], item["views"]),
                "likes": max(prev["likes"], item["likes"]),
                "comments": max(prev["comments"], item["comments"]),
                "shares": max(prev["shares"], item["shares"]),
                "saves": max(prev["saves"], item["saves"]),
                "avg_watch_time_sec": max(prev["avg_watch_time_sec"], item["avg_watch_time_sec"]),
                "completion_rate": max(prev["completion_rate"], item["completion_rate"]),
                "tiktok_url": prev["tiktok_url"] or item["tiktok_url"],
            }
    return list(merged.values())


def sync_tiktok_analytics(
    db_path: Optional[str] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """
    Sync existing posted video metrics from TikTok Studio using stealth Playwright
    and stored session cookies via 3 layered strategies:
      1. Intercept internal TikTok Studio JSON API responses (exact structured metrics)
      2. Parse embedded hydration JSON (__UNIVERSAL_DATA_FOR_REHYDRATION__ / SIGI_STATE)
      3. Broad DOM extraction fallback (video links, table rows, virtualized cards)
    """
    import os
    from playwright.sync_api import sync_playwright
    from core.tiktok_poster import check_cookie_health, parse_cookies, _apply_stealth, _dismiss_modals

    def _prog(msg: str, pct: int) -> None:
        logger.info("[Analytics Sync %d%%] %s", pct, msg)
        if progress_cb:
            progress_cb(msg, pct)

    init_db(db_path)
    health = check_cookie_health(db_path=db_path)
    if not health["healthy"]:
        return {
            "ok": False,
            "synced": 0,
            "scanned": 0,
            "message": f"Skipped TikTok sync: {health['reason']}",
        }

    cookies = parse_cookies(get_setting("cookies_json", "", db_path=db_path) or "")
    posts = get_post_analytics_rows(limit=500, db_path=db_path)
    if not posts:
        return {"ok": True, "synced": 0, "scanned": 0, "message": "No local posts found to match."}

    _prog("Launching stealth browser for TikTok Studio analytics...", 15)
    api_videos: Dict[str, Dict[str, Any]] = {}
    dom_items: List[Dict[str, Any]] = []
    intercepted_urls: List[str] = []
    landed_url = ""
    captcha_detected = False

    def _merge_api_videos(items: List[Dict[str, Any]]) -> None:
        for item in items:
            key = item["tiktok_id"] or _normalize_match_text(item["caption"])
            if not key:
                continue
            prev = api_videos.get(key)
            if not prev:
                api_videos[key] = item
            else:
                api_videos[key] = {
                    "tiktok_id": prev["tiktok_id"] or item["tiktok_id"],
                    "caption": prev["caption"] if len(prev["caption"]) >= len(item["caption"]) else item["caption"],
                    "views": max(prev["views"], item["views"]),
                    "likes": max(prev["likes"], item["likes"]),
                    "comments": max(prev["comments"], item["comments"]),
                    "shares": max(prev["shares"], item["shares"]),
                    "saves": max(prev["saves"], item["saves"]),
                    "avg_watch_time_sec": max(prev["avg_watch_time_sec"], item["avg_watch_time_sec"]),
                    "completion_rate": max(prev["completion_rate"], item["completion_rate"]),
                    "tiktok_url": prev["tiktok_url"] or item["tiktok_url"],
                }

    def _on_response(response: Any) -> None:
        try:
            if response.status != 200:
                return
            url = response.url or ""
            if "tiktok.com" not in url:
                return
            ctype = (response.headers or {}).get("content-type", "").lower()
            if "json" not in ctype:
                return
            body = response.json()
            extracted = _extract_tiktok_videos_from_json(body)
            if extracted:
                intercepted_urls.append(url.split("?")[0])
                _merge_api_videos(extracted)
        except Exception:
            pass

    def _scan_page_hydration_and_dom(page: Any) -> None:
        # Strategy 2: embedded hydration JSON
        try:
            hydration_blobs = page.evaluate("""
                () => {
                    const out = [];
                    ['__UNIVERSAL_DATA_FOR_REHYDRATION__', 'SIGI_STATE'].forEach((id) => {
                        const el = document.getElementById(id);
                        if (el && el.textContent) out.push(el.textContent);
                    });
                    return out;
                }
            """) or []
            for blob in hydration_blobs:
                try:
                    _merge_api_videos(_extract_tiktok_videos_from_json(json.loads(blob)))
                except Exception:
                    continue
        except Exception:
            pass

        # Strategy 3: broad DOM extraction (video links + table/list/card rows)
        try:
            rows = page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();
                    const addNode = (el, hrefHint) => {
                        if (!el) return;
                        const text = (el.innerText || '').trim();
                        if (!text || text.length < 6 || text.length > 2000) return;
                        const linkEl = el.querySelector ? el.querySelector('a[href*="/video/"]') : null;
                        const href = (linkEl && linkEl.href) || hrefHint || '';
                        const sig = href || text.slice(0, 120);
                        if (seen.has(sig)) return;
                        seen.add(sig);
                        results.push({ text, href });
                    };

                    // 1. Anchors pointing to /video/<id> -> walk up to row/card container
                    document.querySelectorAll('a[href*="/video/"]').forEach((a) => {
                        let cur = a;
                        for (let i = 0; i < 4 && cur.parentElement; i++) {
                            cur = cur.parentElement;
                            const t = (cur.innerText || '').trim();
                            if (t.length >= 25 && t.length <= 1200) {
                                addNode(cur, a.href);
                                break;
                            }
                        }
                        addNode(a, a.href);
                    });

                    // 2. Generic table / list / virtualized post rows
                    const sel = [
                        'tr', '[role="row"]', '[data-tt*="Post"]',
                        '[class*="video-item" i]', '[class*="VideoRow" i]',
                        '[class*="post-row" i]', '[class*="PostRow" i]',
                        '[class*="table-row" i]', '[class*="TableRow" i]',
                        '[data-e2e*="video" i]'
                    ].join(', ');
                    document.querySelectorAll(sel).forEach((el) => addNode(el, ''));
                    return results;
                }
            """) or []
            dom_items.extend(rows)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
        )
        context.add_cookies(cookies)
        page = context.new_page()
        _apply_stealth(page)
        page.on("response", _on_response)

        try:
            urls_to_try = [
                ("https://www.tiktok.com/tiktokstudio/content", "Opening TikTok Studio content list...", 30),
                ("https://www.tiktok.com/tiktokstudio/analytics/content", "Checking TikTok Studio analytics view...", 55),
            ]
            for target_url, label, pct in urls_to_try:
                _prog(label, pct)
                page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)
                landed_url = page.url

                if "login" in landed_url.lower():
                    return {
                        "ok": False,
                        "synced": 0,
                        "scanned": 0,
                        "message": "TikTok session expired (redirected to login). Re-import cookies.",
                    }

                _dismiss_modals(page)

                # Check if TikTok presented a verification/captcha challenge
                try:
                    body_txt = (page.inner_text("body", timeout=2000) or "").lower()
                    if any(w in body_txt for w in ("verify to continue", "drag the slider", "captcha", "puzzle")):
                        captcha_detected = True
                except Exception:
                    pass

                # Scroll to trigger lazy-loaded rows / paginated API calls
                for _ in range(2):
                    try:
                        page.mouse.wheel(0, 1400)
                    except Exception:
                        pass
                    page.wait_for_timeout(1500)

                _scan_page_hydration_and_dom(page)
                if api_videos:
                    break

            # Save a diagnostic screenshot whenever nothing was discovered
            if not api_videos and not dom_items:
                try:
                    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
                    os.makedirs(out_dir, exist_ok=True)
                    page.screenshot(path=os.path.join(out_dir, "tiktok_analytics_last_sync.png"), full_page=False)
                except Exception:
                    pass
        finally:
            browser.close()

    _prog("Matching TikTok metrics to local post history...", 85)
    synced = 0
    matched_post_ids = set()

    # Prefer structured JSON API records first
    discovered_api = list(api_videos.values())
    for item in discovered_api:
        target = _match_post_row(posts, caption=item["caption"], tiktok_id=item["tiktok_id"])
        if not target:
            continue
        upsert_post_analytics(
            post_id=target["post_id"],
            views=item["views"],
            likes=item["likes"],
            comments=item["comments"],
            shares=item["shares"],
            saves=item["saves"],
            avg_watch_time_sec=item["avg_watch_time_sec"],
            completion_rate=item["completion_rate"],
            tiktok_url=item["tiktok_url"] or None,
            source="tiktok_sync",
            db_path=db_path,
        )
        matched_post_ids.add(target["post_id"])
        synced += 1

    # Supplement with DOM fallback for any local post not already matched via API
    for item in dom_items:
        text = item.get("text") or ""
        href = item.get("href") or ""
        tid_match = re.search(r"/video/(\d+)", href)
        tiktok_id = tid_match.group(1) if tid_match else ""

        target = _match_post_row(posts, caption=text, tiktok_id=tiktok_id)
        if not target or target["post_id"] in matched_post_ids:
            continue

        metric_candidates: List[int] = []
        for token in re.findall(r"\b\d+(?:\.\d+)?[KMB]?\b", text.upper()):
            if re.fullmatch(r"19\d\d|20\d\d", token):
                continue
            metric_candidates.append(_parse_human_number(token))

        views = metric_candidates[0] if len(metric_candidates) >= 1 else 0
        likes = metric_candidates[1] if len(metric_candidates) >= 2 else 0
        comments = metric_candidates[2] if len(metric_candidates) >= 3 else 0
        shares = metric_candidates[3] if len(metric_candidates) >= 4 else 0

        if views > 0 or likes > 0:
            upsert_post_analytics(
                post_id=target["post_id"],
                views=views,
                likes=likes,
                comments=comments,
                shares=shares,
                tiktok_url=href or None,
                source="tiktok_sync",
                db_path=db_path,
            )
            matched_post_ids.add(target["post_id"])
            synced += 1

    total_scanned = len(discovered_api) if discovered_api else len(dom_items)
    if captcha_detected and total_scanned == 0:
        msg = (
            "TikTok Studio requested a verification challenge in headless mode (0 rows scanned). "
            "Export analytics CSV from TikTok Studio and use Import CSV/JSON, or re-import fresh cookies."
        )
        _prog(msg, 100)
        return {"ok": False, "synced": 0, "scanned": 0, "message": msg}

    if total_scanned == 0:
        msg = (
            f"Synced 0 post(s) from TikTok Studio (0 rows scanned at {landed_url or 'tiktokstudio'}). "
            "Check output/tiktok_analytics_last_sync.png or use Import CSV/JSON."
        )
    elif synced == 0:
        msg = (
            f"Found {total_scanned} video(s) on TikTok Studio, but 0 matched your local Post History "
            "(matching artist & title in caption)."
        )
    else:
        src_note = "via TikTok API" if discovered_api else "via DOM"
        msg = f"Synced {synced} post(s) from TikTok Studio ({total_scanned} scanned {src_note})."

    _prog(msg, 100)
    return {
        "ok": True,
        "synced": synced,
        "scanned": total_scanned,
        "api_endpoints": list(dict.fromkeys(intercepted_urls))[:5],
        "message": msg,
    }


def apply_report_feedback(
    report_id: int,
    apply_wildcard: bool = True,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Activate a report's feedback weights for future track selection and optionally
    apply its recommended wildcard chance.
    """
    report = get_analytics_report_by_id(report_id, db_path=db_path)
    if not report:
        raise ValueError(f"Analytics report #{report_id} not found.")

    mark_report_applied(report_id, db_path=db_path)
    weights = report.get("feedback_weights") or {}
    applied_wc = None
    if apply_wildcard and weights.get("recommended_wildcard_chance") is not None:
        wc = max(0, min(100, int(weights["recommended_wildcard_chance"])))
        set_setting("wildcard_chance", str(wc), db_path=db_path)
        applied_wc = wc

    return {
        "ok": True,
        "report_id": report_id,
        "applied_wildcard_chance": applied_wc,
        "feedback_weights": weights,
    }


def evaluate_past_reports(db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Closed-loop AI self-evaluation:
    Grades past analytics reports' recommendations (genre boosts, decade boosts,
    preferred visual templates, avoid lists) against subsequent real post metrics.
    Persists `outcome_evaluation` on each evaluated report and returns a summary
    of suggestion successes and failures to feed back into future AI analyses.
    """
    init_db(db_path)
    all_rows = get_post_analytics_rows(limit=1000, db_path=db_path)
    tracked = [r for r in all_rows if r.get("has_metrics") or int(r.get("views", 0)) > 0]
    reports = get_analytics_reports(limit=30, db_path=db_path)

    evaluated_count = 0
    for rep in reports:
        fw = rep.get("feedback_weights") or {}
        if not fw:
            continue
        rep_ts = _parse_iso_dt(rep.get("created_at")) or _parse_iso_dt(rep.get("report_date"))
        if rep_ts is None:
            continue

        before_posts = [p for p in tracked if (_post_timestamp(p) or rep_ts) <= rep_ts]
        after_posts = [p for p in tracked if (_post_timestamp(p) or rep_ts) > rep_ts]
        if not after_posts:
            continue

        before_stats = _window_stats(before_posts)
        after_stats = _window_stats(after_posts)
        baseline_score = before_stats["avg_score"] if before_stats["count"] > 0 else after_stats["avg_score"]
        score_delta_pct = _pct_delta(after_stats["avg_score"], before_stats["avg_score"])
        views_delta_pct = _pct_delta(after_stats["avg_views"], before_stats["avg_views"])

        succeeded: List[str] = []
        failed: List[str] = []

        # 1. Evaluate boosted & reduced genres
        for genre, w in (fw.get("preferred_genres") or {}).items():
            g_posts = [p for p in after_posts if (p.get("genre") or "").strip().lower() == genre.strip().lower()]
            if not g_posts:
                continue
            g_avg = _window_stats(g_posts)["avg_score"]
            delta = _pct_delta(g_avg, baseline_score)
            if w > 1.05:
                if g_avg >= baseline_score * 1.02:
                    succeeded.append(
                        f"Boosting genre '{genre}' (x{w:.2f}) succeeded: {len(g_posts)} post(s) averaged score {g_avg} ({delta:+.1f}% vs baseline)"
                        if delta is not None else
                        f"Boosting genre '{genre}' (x{w:.2f}) succeeded: avg score {g_avg}"
                    )
                elif g_avg < baseline_score * 0.95:
                    failed.append(
                        f"Boosting genre '{genre}' (x{w:.2f}) underperformed: {len(g_posts)} post(s) averaged score {g_avg} ({delta:+.1f}% vs baseline)"
                        if delta is not None else
                        f"Boosting genre '{genre}' (x{w:.2f}) underperformed: avg score {g_avg}"
                    )
            elif w < 0.95:
                if g_avg > baseline_score * 1.10:
                    failed.append(
                        f"Reducing genre '{genre}' (x{w:.2f}) was too conservative: subsequent posts scored {g_avg} (+{delta:.1f}%)"
                        if delta is not None else
                        f"Reducing genre '{genre}' (x{w:.2f}) was too conservative: avg score {g_avg}"
                    )

        # 2. Evaluate boosted decades
        for dec, w in (fw.get("preferred_decades") or {}).items():
            d_posts = [
                p for p in after_posts
                if p.get("year") and f"{(int(p['year']) // 10) * 10}s".lower() == dec.strip().lower()
            ]
            if not d_posts:
                continue
            d_avg = _window_stats(d_posts)["avg_score"]
            delta = _pct_delta(d_avg, baseline_score)
            if w > 1.05:
                if d_avg >= baseline_score * 1.02:
                    succeeded.append(
                        f"Prioritizing {dec} (x{w:.2f}) succeeded: avg score {d_avg} ({delta:+.1f}% vs baseline)"
                        if delta is not None else
                        f"Prioritizing {dec} (x{w:.2f}) succeeded: avg score {d_avg}"
                    )
                elif d_avg < baseline_score * 0.95:
                    failed.append(
                        f"Prioritizing {dec} (x{w:.2f}) underperformed: avg score {d_avg} ({delta:+.1f}% vs baseline)"
                        if delta is not None else
                        f"Prioritizing {dec} (x{w:.2f}) underperformed: avg score {d_avg}"
                    )

        # 3. Evaluate preferred visual templates
        pref_tpls = [t.strip().lower() for t in (fw.get("preferred_templates") or []) if t]
        if pref_tpls:
            t_posts = [p for p in after_posts if (p.get("template_used") or "").strip().lower() in pref_tpls]
            if t_posts:
                t_avg = _window_stats(t_posts)["avg_score"]
                delta = _pct_delta(t_avg, baseline_score)
                tpl_str = "/".join(pref_tpls[:2])
                if t_avg >= baseline_score * 1.02:
                    succeeded.append(
                        f"Recommended style '{tpl_str}' succeeded: avg score {t_avg} ({delta:+.1f}% vs baseline)"
                        if delta is not None else
                        f"Recommended style '{tpl_str}' succeeded: avg score {t_avg}"
                    )
                elif t_avg < baseline_score * 0.95:
                    failed.append(
                        f"Recommended style '{tpl_str}' underperformed: avg score {t_avg} ({delta:+.1f}% vs baseline)"
                        if delta is not None else
                        f"Recommended style '{tpl_str}' underperformed: avg score {t_avg}"
                    )

        if score_delta_pct is not None and score_delta_pct >= 5.0 and len(succeeded) >= len(failed):
            verdict = "improved"
        elif score_delta_pct is not None and score_delta_pct <= -5.0 and len(failed) >= len(succeeded):
            verdict = "regressed"
        elif succeeded and not failed:
            verdict = "improved"
        elif failed and not succeeded:
            verdict = "regressed"
        else:
            verdict = "mixed"

        evaluation = {
            "verdict": verdict,
            "before_posts": before_stats["count"],
            "after_posts": after_stats["count"],
            "before_avg_score": before_stats["avg_score"],
            "after_avg_score": after_stats["avg_score"],
            "score_delta_pct": score_delta_pct,
            "views_delta_pct": views_delta_pct,
            "succeeded_suggestions": succeeded[:5],
            "failed_suggestions": failed[:5],
            "evaluated_at": datetime.now().isoformat(timespec="seconds"),
        }
        update_report_outcome_evaluation(rep["id"], evaluation, db_path=db_path)
        evaluated_count += 1

    return build_learning_summary(db_path=db_path, newly_evaluated=evaluated_count)


def build_learning_summary(db_path: Optional[str] = None, newly_evaluated: int = 0) -> Dict[str, Any]:
    """Compile a compact summary of past AI suggestion successes & failures for prompts & UI."""
    evaluated_reports = get_evaluated_learning_history(limit=8, db_path=db_path)
    recent_successes: List[str] = []
    recent_failures: List[str] = []
    history_entries: List[Dict[str, Any]] = []
    improved_count = 0
    regressed_count = 0

    for rep in evaluated_reports:
        ev = rep.get("outcome_evaluation") or {}
        if not isinstance(ev, dict):
            continue
        verdict = ev.get("verdict", "mixed")
        if verdict == "improved":
            improved_count += 1
        elif verdict == "regressed":
            regressed_count += 1

        for s in ev.get("succeeded_suggestions") or []:
            if s not in recent_successes:
                recent_successes.append(s)
        for f in ev.get("failed_suggestions") or []:
            if f not in recent_failures:
                recent_failures.append(f)

        history_entries.append({
            "report_id": rep["id"],
            "report_date": rep.get("report_date"),
            "period_type": rep.get("period_type") or "daily",
            "verdict": verdict,
            "score_delta_pct": ev.get("score_delta_pct"),
            "views_delta_pct": ev.get("views_delta_pct"),
            "after_posts": ev.get("after_posts", 0),
            "succeeded": (ev.get("succeeded_suggestions") or [])[:3],
            "failed": (ev.get("failed_suggestions") or [])[:3],
        })

    return {
        "evaluated_reports": len(history_entries),
        "newly_evaluated": newly_evaluated,
        "improved_count": improved_count,
        "regressed_count": regressed_count,
        "recent_successes": recent_successes[:6],
        "recent_failures": recent_failures[:6],
        "history": history_entries[:6],
    }


def run_analytics_report(
    period: str = "daily",
    sync_first: Optional[bool] = None,
    auto_apply_feedback: Optional[bool] = None,
    db_path: Optional[str] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """
    Execute a daily, weekly, or monthly post analytics pipeline:
      1. Optionally sync latest metrics from TikTok Studio
      2. Evaluate past AI reports against subsequent post performance (learning loop!)
      3. Aggregate period-windowed metrics & period-over-period comparisons
      4. Analyze with the local CPU-efficient LLM (fed with past suggestion wins & failures)
      5. Persist the report with lessons_learned & optionally activate feedback weights
    """
    period_key = period if period in ("daily", "weekly", "monthly") else "daily"
    period_label = period_key.capitalize()

    def _prog(msg: str, pct: int) -> None:
        logger.info("[%s Analytics %d%%] %s", period_label, pct, msg)
        if progress_cb:
            progress_cb(msg, pct)

    init_db(db_path)
    do_sync = (
        bool(sync_first)
        if sync_first is not None
        else get_setting("analytics_auto_sync", "true", db_path=db_path) == "true"
    )
    do_apply = (
        bool(auto_apply_feedback)
        if auto_apply_feedback is not None
        else get_setting("analytics_feedback_enabled", "true", db_path=db_path) == "true"
    )

    sync_result: Optional[Dict[str, Any]] = None
    if do_sync:
        _prog("Checking TikTok Studio for updated post metrics...", 15)
        try:
            sync_result = sync_tiktok_analytics(db_path=db_path)
        except Exception as exc:
            logger.warning("TikTok analytics sync skipped due to error: %s", exc)
            sync_result = {"ok": False, "synced": 0, "message": str(exc)}

    _prog("Evaluating past AI suggestions against recent post outcomes...", 30)
    learning_summary = evaluate_past_reports(db_path=db_path)

    _prog(f"Aggregating {period_key} post performance metrics...", 45)
    summary = build_analytics_summary(db_path=db_path, period=period_key)

    _prog(f"Running local CPU-efficient LLM ({period_key} analysis + learning loop)...", 60)
    analysis = analyze_posts_with_llm(
        summary,
        db_path=db_path,
        period=period_key,
        learning_history=learning_summary,
    )

    _prog(f"Saving {period_key} analytics report...", 90)
    report_date = datetime.now().strftime("%Y-%m-%d")
    report_id = save_analytics_report(
        report_date=report_date,
        period_type=period_key,
        model_used=analysis["model_used"],
        summary_markdown=analysis["summary_markdown"],
        top_posts=analysis["top_posts"],
        improvements=analysis["improvements"],
        feedback_weights=analysis["feedback_weights"],
        period_comparison=summary.get("period_comparison"),
        lessons_learned=analysis.get("lessons_learned") or [],
        posts_analyzed=summary["totals"]["tracked_posts"],
        elapsed_sec=analysis["elapsed_sec"],
        status=analysis["status"],
        error_message=analysis.get("error_message"),
        applied_to_feedback=do_apply and summary["totals"]["tracked_posts"] > 0,
        db_path=db_path,
    )

    report = get_analytics_report_by_id(report_id, db_path=db_path)
    _prog(
        f"{period_label} report #{report_id} complete ({analysis['model_used']}, {analysis['elapsed_sec']}s).",
        100,
    )
    return {
        "ok": True,
        "period": period_key,
        "report": report,
        "sync": sync_result,
        "learning": learning_summary,
    }


def run_daily_analytics_report(
    sync_first: Optional[bool] = None,
    auto_apply_feedback: Optional[bool] = None,
    db_path: Optional[str] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """Backward-compatible wrapper for running a daily analytics report."""
    return run_analytics_report(
        period="daily",
        sync_first=sync_first,
        auto_apply_feedback=auto_apply_feedback,
        db_path=db_path,
        progress_cb=progress_cb,
    )
