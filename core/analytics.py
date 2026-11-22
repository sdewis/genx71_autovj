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
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.db import (
    init_db,
    get_setting,
    set_setting,
    get_post_analytics_rows,
    upsert_post_analytics,
    save_analytics_report,
    get_analytics_report_by_id,
    mark_report_applied,
)
from core.llm_analyst import analyze_posts_with_llm

logger = logging.getLogger("genx71.analytics")


def _normalize_match_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


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


def build_analytics_summary(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate existing post analytics into KPIs, category breakdowns, and rankings."""
    init_db(db_path)
    rows = get_post_analytics_rows(limit=500, db_path=db_path)
    tracked = [r for r in rows if r.get("has_metrics") or int(r.get("views", 0)) > 0]

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


def sync_tiktok_analytics(
    db_path: Optional[str] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """
    Scrape existing posted video metrics from TikTok Studio Content page using
    stealth Playwright and stored session cookies, matching captions to local posts.
    """
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
    scraped_items: List[Dict[str, Any]] = []

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

        try:
            _prog("Opening TikTok Studio content list...", 35)
            page.goto("https://www.tiktok.com/tiktokstudio/content", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            if "login" in page.url.lower():
                return {
                    "ok": False,
                    "synced": 0,
                    "scanned": 0,
                    "message": "TikTok session expired (redirected to login). Re-import cookies.",
                }

            _dismiss_modals(page)
            page.wait_for_timeout(2000)

            _prog("Extracting video view & engagement metrics...", 65)
            # Extract rows from TikTok Studio content table / list
            scraped_items = page.evaluate("""
                () => {
                    const results = [];
                    const rows = document.querySelectorAll('tr, [role="row"], [class*="video-item"], [class*="VideoRow"]');
                    rows.forEach((row) => {
                        const text = (row.innerText || '').trim();
                        if (!text || text.length < 6) return;
                        const linkEl = row.querySelector('a[href*="/video/"]');
                        const href = linkEl ? linkEl.href : '';
                        const cells = Array.from(row.querySelectorAll('td, [role="cell"]'))
                            .map((c) => (c.innerText || '').trim())
                            .filter(Boolean);
                        results.push({ text, href, cells });
                    });
                    return results;
                }
            """) or []
        finally:
            browser.close()

    _prog("Matching TikTok metrics to local post history...", 85)
    synced = 0
    for item in scraped_items:
        text = item.get("text") or ""
        href = item.get("href") or ""
        tid_match = re.search(r"/video/(\d+)", href)
        tiktok_id = tid_match.group(1) if tid_match else ""

        target = _match_post_row(posts, caption=text, tiktok_id=tiktok_id)
        if not target:
            continue

        # Parse numeric metric tokens from table cells or lines (Views, Likes, Comments, Shares)
        metric_candidates: List[int] = []
        for token in re.findall(r"\b\d+(?:\.\d+)?[KMB]?\b", text.upper()):
            # Ignore 4-digit release years like 1984
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
            synced += 1

    msg = f"Synced {synced} post(s) from TikTok Studio ({len(scraped_items)} rows scanned)."
    _prog(msg, 100)
    return {"ok": True, "synced": synced, "scanned": len(scraped_items), "message": msg}


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


def run_daily_analytics_report(
    sync_first: Optional[bool] = None,
    auto_apply_feedback: Optional[bool] = None,
    db_path: Optional[str] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """
    Execute the full daily post analytics pipeline:
      1. Optionally sync latest metrics from TikTok Studio (if enabled & cookies healthy)
      2. Aggregate post metrics & breakdowns
      3. Analyze with the local CPU-efficient LLM (Qwen3.5 2B via Ollama, with statistical fallback)
      4. Persist the daily report and optionally activate feedback weights for future track selection
    """
    def _prog(msg: str, pct: int) -> None:
        logger.info("[Daily Analytics %d%%] %s", pct, msg)
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

    _prog("Aggregating post performance metrics...", 35)
    summary = build_analytics_summary(db_path=db_path)

    _prog("Running local CPU-efficient LLM analysis...", 55)
    analysis = analyze_posts_with_llm(summary, db_path=db_path)

    _prog("Saving daily analytics report...", 90)
    report_date = datetime.now().strftime("%Y-%m-%d")
    report_id = save_analytics_report(
        report_date=report_date,
        model_used=analysis["model_used"],
        summary_markdown=analysis["summary_markdown"],
        top_posts=analysis["top_posts"],
        improvements=analysis["improvements"],
        feedback_weights=analysis["feedback_weights"],
        posts_analyzed=summary["totals"]["tracked_posts"],
        elapsed_sec=analysis["elapsed_sec"],
        status=analysis["status"],
        error_message=analysis.get("error_message"),
        applied_to_feedback=do_apply and summary["totals"]["tracked_posts"] > 0,
        db_path=db_path,
    )

    report = get_analytics_report_by_id(report_id, db_path=db_path)
    _prog(
        f"Daily report #{report_id} complete ({analysis['model_used']}, {analysis['elapsed_sec']}s).",
        100,
    )
    return {
        "ok": True,
        "report": report,
        "sync": sync_result,
    }
