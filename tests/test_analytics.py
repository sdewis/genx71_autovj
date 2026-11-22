"""
Tests for Post Analytics, Local CPU-Efficient LLM Analyst, Daily 2AM Scheduling,
and Feedback-Weighted Future Track Selection.
"""

import json
import random
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from core.db import (
    init_db,
    add_track,
    record_post,
    set_setting,
    get_setting,
    upsert_post_analytics,
    get_post_analytics_rows,
    get_latest_analytics_report,
    get_active_feedback_weights,
)
from core.analytics import (
    build_analytics_summary,
    import_analytics_file,
    run_daily_analytics_report,
    apply_report_feedback,
)
from core.llm_analyst import (
    DEFAULT_LLM_MODEL,
    analyze_posts_with_llm,
    sanitize_feedback_weights,
)
from core.charts import compute_track_feedback_weight, select_next_track
from daemon import AutoVJDaemon


def _seed_sample_posts(db_path: str):
    init_db(db_path)
    t1 = add_track("New Order", "Blue Monday", 1983, 9, "UK", 0, "Synth-Pop", db_path=db_path)
    t2 = add_track("Depeche Mode", "Enjoy the Silence", 1990, 6, "UK", 0, "Synth-Pop", db_path=db_path)
    t3 = add_track("Nirvana", "Smells Like Teen Spirit", 1991, 6, "US", 0, "Grunge", db_path=db_path)
    t4 = add_track("Slowdive", "Alison", 1993, 35, "UK", 1, "Shoegaze", db_path=db_path)

    p1 = record_post(t1, "New Order", "Blue Monday", 1983, "UK #9 • 1983", template_used="neon", status="posted", db_path=db_path)
    p2 = record_post(t2, "Depeche Mode", "Enjoy the Silence", 1990, "UK #6 • 1990", template_used="vhs", status="posted", db_path=db_path)
    p3 = record_post(t3, "Nirvana", "Smells Like Teen Spirit", 1991, "US #6 • 1991", template_used="hifi", status="posted", db_path=db_path)
    p4 = record_post(t4, "Slowdive", "Alison", 1993, "DEEP CUT • UK #35 • 1993", template_used="riso", status="posted", db_path=db_path)
    return (t1, t2, t3, t4), (p1, p2, p3, p4)


def test_post_analytics_upsert_and_summary(tmp_path):
    db_path = str(tmp_path / "analytics.db")
    _, (p1, p2, p3, p4) = _seed_sample_posts(db_path)

    saved1 = upsert_post_analytics(
        p1, views=12500, likes=1400, comments=95, shares=210, saves=320,
        avg_watch_time_sec=26.4, completion_rate=64.0, db_path=db_path
    )
    saved3 = upsert_post_analytics(
        p3, views=1200, likes=60, comments=4, shares=5, saves=8,
        avg_watch_time_sec=11.0, completion_rate=22.0, db_path=db_path
    )

    assert saved1["engagement_rate"] > saved3["engagement_rate"]
    assert saved1["performance_score"] > saved3["performance_score"]

    summary = build_analytics_summary(db_path=db_path)
    assert summary["totals"]["total_posts"] == 4
    assert summary["totals"]["tracked_posts"] == 2
    assert summary["totals"]["total_views"] == 13700
    assert summary["top_posts"][0]["post_id"] == p1
    assert summary["breakdowns"]["by_genre"][0]["label"] == "Synth-Pop"


def test_import_analytics_csv_and_json(tmp_path):
    db_path = str(tmp_path / "import.db")
    _, (p1, p2, _, _) = _seed_sample_posts(db_path)

    csv_data = (
        "artist,title,views,likes,comments,shares,completion_rate\n"
        "New Order,Blue Monday,15.5K,1800,120,250,58.5%\n"
        "Unknown Band,Missing Song,500,10,1,0,10%\n"
    )
    res_csv = import_analytics_file(csv_data, "tiktok_export.csv", db_path=db_path)
    assert res_csv["ok"] is True
    assert res_csv["matched"] == 1
    assert res_csv["unmatched"] == 1

    json_data = json.dumps([
        {"post_id": p2, "views": 8400, "likes": 910, "comments": 44, "shares": 88, "completion_rate": 49.0}
    ])
    res_json = import_analytics_file(json_data, "metrics.json", db_path=db_path)
    assert res_json["matched"] == 1

    rows = {r["post_id"]: r for r in get_post_analytics_rows(db_path=db_path)}
    assert rows[p1]["views"] == 15500
    assert rows[p2]["views"] == 8400


def test_local_llm_analysis_with_mocked_ollama(tmp_path):
    db_path = str(tmp_path / "llm.db")
    _, (p1, p2, p3, _) = _seed_sample_posts(db_path)
    upsert_post_analytics(p1, views=25000, likes=3100, comments=180, shares=420, completion_rate=72.0, db_path=db_path)
    upsert_post_analytics(p2, views=18000, likes=2100, comments=95, shares=240, completion_rate=61.0, db_path=db_path)
    upsert_post_analytics(p3, views=900, likes=35, comments=2, shares=1, completion_rate=18.0, db_path=db_path)

    llm_json = {
        "summary_markdown": "1980s UK Synth-Pop dominated reach and completion rate.",
        "top_posts": [
            {"post_id": p1, "artist": "New Order", "title": "Blue Monday", "reason": "Strong 80s synth hook and neon visuals."}
        ],
        "improvements": [
            "Feature more 1980s UK Synth-Pop classics with neon or VHS style packs.",
            "Keep opening DJ hook under 5 seconds to maintain >60% completion."
        ],
        "feedback_weights": {
            "preferred_genres": {"Synth-Pop": 1.65, "Grunge": 0.75},
            "preferred_decades": {"1980s": 1.45, "1990s": 0.95},
            "preferred_regions": {"UK": 1.3, "US": 0.9},
            "recommended_wildcard_chance": 30,
            "preferred_templates": ["neon", "vhs"],
            "avoid_genres": [],
            "rationale": "Synth-Pop and 1980s UK tracks drove 95% of total views."
        }
    }

    mock_tags_resp = MagicMock()
    mock_tags_resp.content = b'{"models":[{"name":"qwen3.5:2b"}]}'
    mock_tags_resp.json.return_value = {"models": [{"name": DEFAULT_LLM_MODEL}]}
    mock_tags_resp.raise_for_status.return_value = None

    mock_chat_resp = MagicMock()
    mock_chat_resp.json.return_value = {"message": {"content": json.dumps(llm_json)}}
    mock_chat_resp.raise_for_status.return_value = None

    with patch("core.llm_analyst.requests.get", return_value=mock_tags_resp), \
         patch("core.llm_analyst.requests.post", return_value=mock_chat_resp):
        res = run_daily_analytics_report(sync_first=False, auto_apply_feedback=True, db_path=db_path)

    report = res["report"]
    assert report["model_used"] == DEFAULT_LLM_MODEL
    assert report["status"] == "completed"
    assert report["applied_to_feedback"] == 1
    assert report["posts_analyzed"] == 3
    assert report["feedback_weights"]["preferred_genres"]["Synth-Pop"] == 1.65

    active = get_active_feedback_weights(db_path=db_path)
    assert active is not None
    assert active["preferred_decades"]["1980s"] == 1.45


def test_local_llm_graceful_statistical_fallback(tmp_path):
    db_path = str(tmp_path / "fallback.db")
    _, (p1, _, p3, _) = _seed_sample_posts(db_path)
    upsert_post_analytics(p1, views=14000, likes=1200, comments=60, shares=150, completion_rate=55.0, db_path=db_path)
    upsert_post_analytics(p3, views=1000, likes=40, comments=2, shares=1, completion_rate=20.0, db_path=db_path)

    with patch("core.llm_analyst.requests.get", side_effect=ConnectionError("Ollama down")):
        res = run_daily_analytics_report(sync_first=False, auto_apply_feedback=True, db_path=db_path)

    report = res["report"]
    assert report["model_used"] == "statistical-fallback"
    assert report["status"] == "fallback"
    assert len(report["improvements"]) >= 1
    assert report["feedback_weights"]["preferred_genres"]["Synth-Pop"] > report["feedback_weights"]["preferred_genres"]["Grunge"]


def test_feedback_weights_bias_future_track_selection(tmp_path):
    db_path = str(tmp_path / "feedback.db")
    init_db(db_path)
    add_track("Soft Cell", "Tainted Love", 1981, 1, "UK", 0, "Synth-Pop", db_path=db_path)
    add_track("Nirvana", "Come As You Are", 1992, 9, "US", 0, "Grunge", db_path=db_path)

    # Save a report with strong Synth-Pop / 1980s / UK preference
    from core.db import save_analytics_report
    rep_id = save_analytics_report(
        report_date="2026-11-22",
        model_used=DEFAULT_LLM_MODEL,
        summary_markdown="Synth-Pop wins.",
        feedback_weights=sanitize_feedback_weights({
            "preferred_genres": {"Synth-Pop": 2.0, "Grunge": 0.5},
            "preferred_decades": {"1980s": 1.8, "1990s": 0.6},
            "preferred_regions": {"UK": 1.5, "US": 0.7},
            "recommended_wildcard_chance": 35,
        }),
        posts_analyzed=5,
        applied_to_feedback=False,
        db_path=db_path,
    )
    apply_res = apply_report_feedback(rep_id, apply_wildcard=True, db_path=db_path)
    assert apply_res["applied_wildcard_chance"] == 35
    assert get_setting("wildcard_chance", db_path=db_path) == "35"

    set_setting("analytics_feedback_enabled", "true", db_path=db_path)
    set_setting("analytics_feedback_strength", "1.0", db_path=db_path)

    w_synth = compute_track_feedback_weight(
        {"genre": "Synth-Pop", "year": 1981, "chart_region": "UK"},
        apply_res["feedback_weights"],
        strength=1.0,
    )
    w_grunge = compute_track_feedback_weight(
        {"genre": "Grunge", "year": 1992, "chart_region": "US"},
        apply_res["feedback_weights"],
        strength=1.0,
    )
    assert w_synth > w_grunge * 4

    # Sample 200 times deterministically and verify Synth-Pop is chosen significantly more often
    random.seed(42)
    picks = [select_next_track(force_wildcard=False, db_path=db_path)["artist"] for _ in range(200)]
    assert picks.count("Soft Cell") > picks.count("Nirvana") * 3


def test_daemon_daily_analytics_default_2am_and_configurable(tmp_path):
    db_path = str(tmp_path / "daemon.db")
    init_db(db_path)

    d = AutoVJDaemon(db_path=db_path)
    # Default time should be 02:00 (2 AM)
    assert get_setting("analytics_report_time", db_path=db_path) == "02:00"
    next_default = d._compute_next_analytics_run()
    assert next_default is not None
    assert next_default.hour == 2 and next_default.minute == 0

    # Configurable time
    set_setting("analytics_report_time", "04:45", db_path=db_path)
    next_custom = d._compute_next_analytics_run()
    assert next_custom is not None
    assert next_custom.hour == 4 and next_custom.minute == 45

    # Disable daily report
    set_setting("analytics_report_enabled", "false", db_path=db_path)
    assert d._compute_next_analytics_run() is None


def test_analytics_api_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import core.db as db_mod
    import api.app as app_mod

    db_path = str(tmp_path / "api_analytics.db")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(app_mod.daemon, "db_path", db_path)
    _, (p1, p2, _, _) = _seed_sample_posts(db_path)

    client = TestClient(app_mod.app)

    # 1. Update metrics via API
    r_upd = client.post(
        f"/api/analytics/posts/{p1}",
        json={"views": 9500, "likes": 1100, "comments": 75, "shares": 140, "saves": 200, "completion_rate": 60.0},
    )
    assert r_upd.status_code == 200
    assert r_upd.json()["ok"] is True
    assert r_upd.json()["analytics"]["views"] == 9500

    # 2. Import CSV via API
    csv_body = "artist,title,views,likes,comments,shares\nDepeche Mode,Enjoy the Silence,5000,450,20,35\n"
    r_imp = client.post("/api/analytics/import", json={"filename": "stats.csv", "content": csv_body})
    assert r_imp.status_code == 200
    assert r_imp.json()["matched"] == 1

    # 3. Overview endpoint returns aggregated stats & config
    with patch("core.llm_analyst.requests.get", side_effect=ConnectionError("offline")):
        r_ov = client.get("/api/analytics/overview")
    assert r_ov.status_code == 200
    ov = r_ov.json()
    assert ov["totals"]["tracked_posts"] == 2
    assert ov["totals"]["total_views"] == 14500
    assert ov["config"]["analytics_report_time"] == "02:00"
    assert ov["config"]["llm_model"] == DEFAULT_LLM_MODEL

    # 4. Run report and apply via API
    with patch("core.llm_analyst.requests.get", side_effect=ConnectionError("offline")):
        rep_res = run_daily_analytics_report(sync_first=False, auto_apply_feedback=False, db_path=db_path)
    rep_id = rep_res["report"]["id"]
    r_apply = client.post(f"/api/analytics/reports/{rep_id}/apply", json={"apply_wildcard": True})
    assert r_apply.status_code == 200
    assert r_apply.json()["ok"] is True


def test_extract_tiktok_videos_from_json_and_matching(tmp_path):
    from core.analytics import _extract_tiktok_videos_from_json, _match_post_row

    payload = {
        "status_code": 0,
        "item_list": [
            {
                "id": "7412345678901234567",
                "desc": "The Human League - Dont You Want Me (1981) | UK #1 #GenX",
                "stats": {
                    "playCount": 18400,
                    "diggCount": 2150,
                    "commentCount": 112,
                    "shareCount": 310,
                    "collectCount": 420,
                },
                "metrics": {
                    "avgWatchTime": 24.5,
                    "completionRate": 0.62,
                },
            },
            {
                "item_id": "7498765432109876543",
                "title": "New Order - Blue Monday (1983)",
                "view_count": "12.5K",
                "like_count": "1.1K",
                "comment_count": 45,
                "share_count": 90,
            },
        ],
    }

    videos = _extract_tiktok_videos_from_json(payload)
    assert len(videos) == 2

    by_id = {v["tiktok_id"]: v for v in videos}
    v1 = by_id["7412345678901234567"]
    assert v1["views"] == 18400
    assert v1["likes"] == 2150
    assert v1["saves"] == 420
    assert v1["avg_watch_time_sec"] == 24.5
    assert v1["completion_rate"] == 62.0

    v2 = by_id["7498765432109876543"]
    assert v2["views"] == 12500
    assert v2["likes"] == 1100

    # Verify apostrophe-tolerant caption matching
    local_posts = [
        {"post_id": 10, "artist": "The Human League", "title": "Don't You Want Me", "tiktok_id": None},
        {"post_id": 11, "artist": "New Order", "title": "Blue Monday", "tiktok_id": None},
    ]
    assert _match_post_row(local_posts, caption=v1["caption"], tiktok_id=v1["tiktok_id"])["post_id"] == 10
    assert _match_post_row(local_posts, caption=v2["caption"], tiktok_id=v2["tiktok_id"])["post_id"] == 11


