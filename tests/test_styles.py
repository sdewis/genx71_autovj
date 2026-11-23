"""
Tests for the GenX71 AutoVJ Style Pack Renderer, multi-band FFT audio features,
era-weighted anti-repeat style rotation, and /api/styles endpoint.
"""

import os
import json
import wave
import random
import tempfile
import subprocess
import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.artwork import _create_fallback_artwork
from core.audio import extract_audio_features
from core.db import init_db, record_post, get_recent_templates
from core.styles import (
    STYLE_PACKS,
    VALID_STYLE_IDS,
    resolve_style_and_variant,
    select_style_pack,
)
from core.pipeline import select_next_style
from core.video_renderer import render_tiktok_video
from api.app import app


def _make_bass_wav(path: str, duration: float = 2.0, sr: int = 44100):
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    kick = (np.abs(np.sin(2 * np.pi * 1.0 * t)) ** 4) * np.sin(2 * np.pi * 80.0 * t)
    lead = 0.4 * np.sin(2 * np.pi * 880.0 * t)
    sig = np.clip((kick + lead) * 14000, -32767, 32767).astype(np.int16)
    stereo = np.column_stack([sig, sig])
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(stereo.tobytes())


def test_extract_audio_features_shapes_and_bounds():
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "beat.wav")
        _make_bass_wav(wav_path, duration=3.0)
        feats = extract_audio_features(wav_path, fps=30, n_bands=24)

        assert feats["fps"] == 30
        assert feats["n_bands"] == 24
        n = len(feats["rms"])
        assert n >= 85
        assert len(feats["bass"]) == n
        assert len(feats["onset"]) == n
        assert len(feats["bands"]) == n
        assert all(len(row) == 24 for row in feats["bands"])
        assert all(0.0 <= v <= 1.0 for v in feats["rms"])
        assert all(0.0 <= v <= 1.0 for v in feats["bass"])
        assert all(0.0 <= v <= 1.0 for v in feats["onset"])
        assert all(0.0 <= v <= 1.0 for row in feats["bands"] for v in row)


def test_style_pack_resolution_and_legacy_mapping():
    assert len(STYLE_PACKS) == 6
    assert VALID_STYLE_IDS == {"hifi", "vhs", "neon", "riso", "kinetic", "cassette"}

    assert resolve_style_and_variant("vinyl", 1983) == ("hifi", "vinyl")
    assert resolve_style_and_variant("cd", 1991) == ("hifi", "cd")
    assert resolve_style_and_variant("retro", 1985) == ("hifi", "card")
    assert resolve_style_and_variant("hifi", 1983) == ("hifi", "vinyl")
    assert resolve_style_and_variant("hifi", 1992) == ("hifi", "cd")
    assert resolve_style_and_variant("neon", 1984) == ("neon", None)
    assert resolve_style_and_variant("cassette", 1986) == ("cassette", None)


def test_era_weighted_auto_rotate_avoids_last_two_styles():
    rng = random.Random(42)
    recent = ["neon", "vhs"]
    for _ in range(100):
        picked = select_style_pack(1985, recent_styles=recent, rng=rng)
        assert picked in VALID_STYLE_IDS
        assert picked not in {"neon", "vhs"}

    # Legacy names in recent history also exclude their resolved style ('hifi')
    for _ in range(50):
        picked = select_style_pack(1994, recent_styles=["cd", "kinetic"], rng=rng)
        assert picked not in {"hifi", "kinetic"}


def test_select_next_style_reads_db_history():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        init_db(db_path)
        record_post(None, "Artist A", "Song A", 1984, template_used="neon", db_path=db_path)
        record_post(None, "Artist B", "Song B", 1985, template_used="cassette", db_path=db_path)
        assert get_recent_templates(limit=2, db_path=db_path) == ["cassette", "neon"]

        rng = random.Random(7)
        for _ in range(40):
            nxt = select_next_style(1985, db_path=db_path, rng=rng)
            assert nxt not in {"neon", "cassette"}
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_api_styles_endpoint():
    with TestClient(app) as client:
        r = client.get("/api/styles")
        assert r.status_code == 200
        styles = r.json()["styles"]
        assert {s["id"] for s in styles} == VALID_STYLE_IDS


@pytest.mark.parametrize("style", ["neon", "cassette"])
def test_browser_style_pack_mp4_render(style):
    with tempfile.TemporaryDirectory() as tmp:
        art_path = os.path.join(tmp, "art.jpg")
        _create_fallback_artwork("A-ha", "Take On Me", art_path)

        wav_path = os.path.join(tmp, "audio.wav")
        _make_bass_wav(wav_path, duration=1.5)
        feats = extract_audio_features(wav_path, fps=30)

        out_mp4 = os.path.join(tmp, f"out_{style}.mp4")
        render_tiktok_video(
            artist="A-ha",
            title="Take On Me",
            year=1985,
            chart_badge="US #1 • 1985",
            artwork_path=art_path,
            audio_path=wav_path,
            rms_envelope=feats["rms"],
            duration=1.5,
            output_path=out_mp4,
            template=style,
            facts=["Fact one.", "Fact two.", "Fact three."],
            audio_features=feats,
            use_browser=True,
        )
        assert os.path.exists(out_mp4)
        assert os.path.getsize(out_mp4) > 15000

        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", out_mp4
        ]).decode()
        info = json.loads(probe)
        assert info["streams"][0]["width"] == 1080
        assert info["streams"][0]["height"] == 1920


def test_delete_post_unlocks_track_and_compacts_queue():
    from core.db import (
        add_track, is_track_posted, queue_post, get_queued_posts, delete_post, get_post_by_id
    )

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        init_db(db_path)
        t1 = add_track("Delete Test Band", "Song One", 1984, chart_peak=5, db_path=db_path)
        t2 = add_track("Delete Test Band", "Song Two", 1985, chart_peak=8, db_path=db_path)
        p1 = record_post(t1, "Delete Test Band", "Song One", 1984, status="rendered", db_path=db_path)
        p2 = record_post(t2, "Delete Test Band", "Song Two", 1985, status="rendered", db_path=db_path)
        queue_post(p1, db_path=db_path)
        queue_post(p2, db_path=db_path)

        assert is_track_posted("Delete Test Band", "Song One", db_path=db_path) is True
        deleted = delete_post(p1, db_path=db_path)
        assert deleted is not None and deleted["id"] == p1
        assert get_post_by_id(p1, db_path=db_path) is None
        # Track is unlocked for re-rendering
        assert is_track_posted("Delete Test Band", "Song One", db_path=db_path) is False
        # Remaining scheduled queue item compacted to position #1
        queued = get_queued_posts(db_path=db_path)
        assert len(queued) == 1
        assert queued[0]["id"] == p2
        assert queued[0]["queue_position"] == 1
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_api_delete_post_removes_file_and_blocks_rendering():
    from core.video_renderer import OUTPUT_DIR
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    fake_mp4 = os.path.join(OUTPUT_DIR, "_pytest_delete_render_test.mp4")
    with open(fake_mp4, "wb") as f:
        f.write(b"fake-mp4-bytes")

    pid_rendered = record_post(
        None, "API Delete Band", "Removable Hit", 1986,
        template_used="neon", video_path=fake_mp4, status="rendered"
    )
    pid_rendering = record_post(
        None, "API Delete Band", "Active Render", 1987,
        template_used="vhs", status="rendering"
    )

    try:
        with TestClient(app) as client:
            # Active render is protected (409)
            r_busy = client.delete(f"/api/posts/{pid_rendering}")
            assert r_busy.status_code == 409

            # Rendered post deletes cleanly and removes its MP4 file
            r_del = client.delete(f"/api/posts/{pid_rendered}")
            assert r_del.status_code == 200
            body = r_del.json()
            assert body["ok"] is True
            assert body["file_removed"] is True
            assert body["track_unlocked"] is True
            assert not os.path.exists(fake_mp4)

            # Deleting again returns 404
            r_missing = client.delete(f"/api/posts/{pid_rendered}")
            assert r_missing.status_code == 404
    finally:
        from core.db import delete_post
        delete_post(pid_rendered)
        delete_post(pid_rendering)
        if os.path.exists(fake_mp4):
            os.remove(fake_mp4)


def test_vinyl_tonearm_home_cue_and_tracking():
    """
    Verify the vinyl turntable tonearm starts in its home position outside the
    disc, auto-lifts and cues to 24% during the hook, and progressively tracks
    to 80% as the main audio ends (both with and without a DJ hook).
    """
    from playwright.sync_api import sync_playwright
    from core.browser_renderer import STUDIO_HTML_PATH, _launch_chromium

    template_url = f"file://{STUDIO_HTML_PATH}"
    base_payload = {
        "style": "hifi",
        "media_variant": "vinyl",
        "artist": "The Human League",
        "title": "Don't You Want Me",
        "year": 1981,
        "chart_badge": "UK #1 • 1981",
        "duration": 50.0,
        "palette": {
            "primary": [195, 45, 78],
            "accent": [255, 82, 118],
            "bg_dark": [14, 15, 24],
        },
        "artwork_data_url": None,
        "logo_data_url": None,
        "watermark_opacity": 0.6,
        "fact_schedule": [],
        "audio": {
            "fps": 30,
            "n_bands": 24,
            "rms": [0.4] * 60,
            "bass": [0.4] * 60,
            "onset": [0.1] * 60,
            "bands": [[0.4] * 24 for _ in range(60)],
        },
    }

    with sync_playwright() as p:
        browser = _launch_chromium(p)
        try:
            page = browser.new_page(viewport={"width": 1080, "height": 1920})
            page.goto(template_url)

            # 1. With a 6.0s spoken DJ hook
            payload_hook = dict(base_payload, hook={
                "text": "Pure 1981 vinyl magic right here!",
                "duration": 6.0,
                "voice_rms": [0.5] * 180,
                "region": "uk",
            })
            page.evaluate("async (pl) => await window.initScene(pl)", payload_hook)

            s_home = page.evaluate("window.computeVinylTonearmState(1.0)")
            assert s_home["phase"] == "home"
            assert s_home["lift"] == 0
            assert s_home["radiusFromCenter"] > 261.2  # parked outside vinyl rim

            s_cue = page.evaluate("window.computeVinylTonearmState(4.2)")
            assert s_cue["phase"] == "cue"
            assert s_cue["lift"] > 0.9

            s_start = page.evaluate("window.computeVinylTonearmState(6.15)")
            assert s_start["phase"] == "play"
            assert s_start["lift"] == 0
            assert abs(s_start["recordPct"] - 0.24) < 0.005

            s_mid = page.evaluate("window.computeVinylTonearmState(28.0)")
            assert s_mid["phase"] == "play"
            assert 0.45 < s_mid["recordPct"] < 0.60
            assert s_mid["radiusFromCenter"] < s_start["radiusFromCenter"]

            s_end = page.evaluate("window.computeVinylTonearmState(49.35)")
            assert s_end["phase"] == "play"
            assert s_end["lift"] == 0
            assert abs(s_end["recordPct"] - 0.80) < 0.005
            assert s_end["radiusFromCenter"] < s_mid["radiusFromCenter"]

            # 2. Without a DJ hook (still starts at home, auto-cues to 24%, ends at 80%)
            payload_no_hook = dict(base_payload, hook=None)
            page.evaluate("async (pl) => await window.initScene(pl)", payload_no_hook)

            nh_home = page.evaluate("window.computeVinylTonearmState(0.2)")
            assert nh_home["phase"] == "home"
            assert nh_home["radiusFromCenter"] > 261.2

            nh_start = page.evaluate("window.computeVinylTonearmState(2.5)")
            assert nh_start["phase"] == "play"
            assert abs(nh_start["recordPct"] - 0.24) < 0.005

            nh_end = page.evaluate("window.computeVinylTonearmState(49.35)")
            assert nh_end["phase"] == "play"
            assert abs(nh_end["recordPct"] - 0.80) < 0.005
        finally:
            browser.close()


