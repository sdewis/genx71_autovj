"""
Integration & unit tests for GenX71 AutoVJ charts, audio clipping,
artwork palette, 9:16 video rendering, and FastAPI endpoints.
"""

import os
import wave
import json
import tempfile
import subprocess
import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.db import init_db, add_track, get_track_by_id, set_setting
from core.charts import GENX_CHART_CATALOG, seed_charts_if_empty, select_next_track, format_chart_badge
from core.artwork import _create_fallback_artwork, extract_palette
from core.audio import (
    find_intelligent_clip_window, extract_rms_envelope,
    prepare_track_audio, MIN_CLIP_DURATION, MAX_CLIP_DURATION
)
from core.facts import CURATED_TRACK_FACTS, get_or_fetch_track_facts
from core.pipeline import (
    select_era_appropriate_template, generate_post_video,
    CD_ERA_START_YEAR, VINYL_AUDIOPHILE_CHANCE
)
from core.video_renderer import (
    render_tiktok_video, compute_fact_schedule, get_fact_alpha,
    generate_watermark_schedule, get_watermark_state,
    FACT_CARD_Y, WM_SPIN_IN_SEC, WM_HOLD_SEC, WM_FADE_OUT_SEC
)
from api.app import app


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def _make_test_wav(path: str, duration: float = 3.0, sr: int = 44100):
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    # Modulated sine wave to create energy envelope variation
    env = 0.2 + 0.7 * np.abs(np.sin(2 * np.pi * 1.5 * t))
    sig = (env * np.sin(2 * np.pi * 440.0 * t) * 16000).astype(np.int16)
    stereo = np.column_stack([sig, sig])
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(stereo.tobytes())


def test_charts_seeding_and_selection(temp_db):
    inserted = seed_charts_if_empty(db_path=temp_db)
    assert inserted > 50

    hit = select_next_track(force_wildcard=False, db_path=temp_db)
    assert hit is not None
    assert hit["is_wildcard"] == 0

    wildcard = select_next_track(force_wildcard=True, db_path=temp_db)
    assert wildcard is not None
    assert wildcard["is_wildcard"] == 1
    badge = format_chart_badge(wildcard)
    assert str(wildcard["year"]) in badge


def test_curated_facts_cover_entire_catalog_and_cache(temp_db):
    # Every built-in catalog track must have 3 curated facts
    for artist, title, *_ in GENX_CHART_CATALOG:
        key = (artist.lower(), title.lower())
        assert key in CURATED_TRACK_FACTS, f"Missing curated facts for {artist} - {title}"
        assert len(CURATED_TRACK_FACTS[key]) == 3

    seed_charts_if_empty(db_path=temp_db)
    track = select_next_track(db_path=temp_db)
    facts = get_or_fetch_track_facts(track, db_path=temp_db, allow_network=False)
    assert len(facts) == 3
    assert all(len(f) > 15 for f in facts)

    # Custom track gets 3 fallback facts and caches them in SQLite
    tid = add_track("Obscure Band", "Lost Demo", 1988, chart_peak=37, chart_region="UK", is_wildcard=1, genre="Synth-Pop", db_path=temp_db)
    custom = get_track_by_id(tid, db_path=temp_db)
    custom_facts = get_or_fetch_track_facts(custom, db_path=temp_db, allow_network=False)
    assert len(custom_facts) == 3
    refreshed = get_track_by_id(tid, db_path=temp_db)
    assert json.loads(refreshed["facts_json"]) == custom_facts


def test_audio_clipping_and_rms():
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "test.wav")
        _make_test_wav(wav_path, duration=5.0)
        start, end = find_intelligent_clip_window(wav_path, target_duration=3.0)
        assert 0.0 <= start < end <= 5.0
        rms = extract_rms_envelope(wav_path, fps=30)
        assert len(rms) >= 140
        assert all(0.0 <= v <= 1.0 for v in rms)


def test_full_track_audio_clip_window_is_35_to_45s():
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "full_track.wav")
        _make_test_wav(wav_path, duration=120.0, sr=8000)
        start, end = find_intelligent_clip_window(wav_path)
        dur = end - start
        assert 35.0 <= dur <= 45.0
        assert MIN_CLIP_DURATION <= dur <= MAX_CLIP_DURATION


def test_prepare_track_audio_pads_short_preview_to_at_least_30s(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        preview_wav = os.path.join(tmp, "short_preview.wav")
        _make_test_wav(preview_wav, duration=28.0)

        monkeypatch.setattr("core.audio.fetch_youtube_audio", lambda a, t, out: preview_wav)
        res = prepare_track_audio("Test Artist", "Short Preview", cache_dir=tmp)
        assert res["duration"] >= MIN_CLIP_DURATION
        assert res["duration"] <= MAX_CLIP_DURATION


def test_fact_schedule_timing_2s_margins():
    facts = ["Fact one.", "Fact two.", "Fact three."]
    duration = 40.0
    schedule = compute_fact_schedule(facts, duration)
    assert len(schedule) == 3
    assert schedule[0]["start"] == pytest.approx(2.0, abs=0.01)
    assert schedule[-1]["end"] == pytest.approx(duration - 2.0, abs=0.01)

    # Invisible before 2s and after duration - 2s
    for slot in schedule:
        assert get_fact_alpha(slot, 1.5) == 0.0
        assert get_fact_alpha(slot, duration - 1.5) == 0.0
        mid = (slot["start"] + slot["end"]) / 2.0
        assert get_fact_alpha(slot, mid) == 1.0


def test_watermark_random_schedule_and_spin_fade_lifecycle():
    import random
    import math
    schedule = generate_watermark_schedule(40.0, wm_width=170, wm_height=100, rng=random.Random(42))
    assert len(schedule) >= 5

    badge_excl = (150, 165, 930, 265)
    for i, p in enumerate(schedule):
        # Within TikTok safe margins & above fact card
        assert p["x"] >= 60
        assert p["x"] + 170 <= 900
        assert p["y"] >= 160
        assert p["y"] + 100 <= FACT_CARD_Y - 25
        # Does not overlap chart badge
        assert (p["x"] + 170 <= badge_excl[0] or p["x"] >= badge_excl[2] or
                p["y"] + 100 <= badge_excl[1] or p["y"] >= badge_excl[3])
        if i > 0:
            prev = schedule[i - 1]
            assert math.hypot(p["cx"] - prev["cx"], p["cy"] - prev["cy"]) >= 260.0

    # Verify spin-fade IN, hold, and 3.0s fade OUT curve
    p0 = schedule[0]
    t0 = p0["start"]
    s_in = get_watermark_state(p0, t0 + WM_SPIN_IN_SEC * 0.5, peak_opacity=0.40)
    assert s_in is not None
    assert 0.0 < s_in[0] < 0.40
    assert 0.0 < s_in[1] < 360.0  # Spinning
    assert 0.6 < s_in[2] < 1.0    # Scaling in

    s_hold = get_watermark_state(p0, t0 + WM_SPIN_IN_SEC + 0.5, peak_opacity=0.40)
    assert s_hold == pytest.approx((0.40, 0.0, 1.0))

    s_fade_mid = get_watermark_state(p0, t0 + WM_SPIN_IN_SEC + WM_HOLD_SEC + WM_FADE_OUT_SEC * 0.5, peak_opacity=0.40)
    assert s_fade_mid is not None
    assert s_fade_mid[0] == pytest.approx(0.20, abs=0.01)
    assert s_fade_mid[1] == 0.0

    # Hidden after 3s fade-out completes
    assert get_watermark_state(p0, t0 + WM_SPIN_IN_SEC + WM_HOLD_SEC + WM_FADE_OUT_SEC + 0.05, peak_opacity=0.40) is None


@pytest.mark.parametrize("template", ["vinyl", "cd", "retro"])
def test_video_render_templates(template):
    with tempfile.TemporaryDirectory() as tmp:
        art_path = os.path.join(tmp, "art.jpg")
        _create_fallback_artwork("New Order", "Blue Monday", art_path)
        palette = extract_palette(art_path)
        assert "primary" in palette and "accent" in palette

        wav_path = os.path.join(tmp, "audio.wav")
        _make_test_wav(wav_path, duration=6.0)
        rms = extract_rms_envelope(wav_path, fps=30)

        out_mp4 = os.path.join(tmp, f"out_{template}.mp4")
        render_tiktok_video(
            artist="New Order",
            title="Blue Monday",
            year=1983,
            chart_badge="UK #9 • 1983",
            artwork_path=art_path,
            audio_path=wav_path,
            rms_envelope=rms,
            duration=6.0,
            output_path=out_mp4,
            template=template,
            facts=[
                "Best-selling 12-inch vinyl single of all time in the UK.",
                "Factory Records lost money on every floppy-disk sleeve.",
                "Built on an Oberheim DMX drum machine in 1983.",
            ]
        )
        assert os.path.exists(out_mp4)
        assert os.path.getsize(out_mp4) > 20000

        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", out_mp4
        ]).decode()
        info = json.loads(probe)
        assert info["streams"][0]["width"] == 1080
        assert info["streams"][0]["height"] == 1920


def test_api_endpoints():
    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert data["stats"]["total_tracks"] > 50

        r_tracks = client.get("/api/tracks?q=New%20Order")
        assert r_tracks.status_code == 200
        assert len(r_tracks.json()["tracks"]) >= 1

        r_settings = client.get("/api/settings")
        assert r_settings.status_code == 200
        assert "daily_post_times" in r_settings.json()["settings"]

        r_cookies = client.post("/api/cookies", json={"cookies_json": '[{"name":"sessionid","value":"abc123"}]'})
        assert r_cookies.status_code == 200
        assert r_cookies.json()["ok"] is True

        # Clean up test cookie
        client.post("/api/cookies", json={"cookies_json": ""})


def test_select_era_appropriate_template():
    import random

    # CD era (1988+) always selects 'cd'
    for year in (CD_ERA_START_YEAR, 1990, 1994, 1996):
        rng = random.Random(123)
        results = {select_era_appropriate_template(year, rng=rng) for _ in range(50)}
        assert results == {"cd"}

    # Vinyl era (pre-1988) selects 'vinyl' (~75%) or 'retro' audiophile (~25%)
    rng = random.Random(42)
    samples = [select_era_appropriate_template(1983, rng=rng) for _ in range(2000)]
    assert set(samples) == {"vinyl", "retro"}
    retro_ratio = samples.count("retro") / len(samples)
    assert abs(retro_ratio - VINYL_AUDIOPHILE_CHANCE) < 0.03

    # Exact threshold check around 25%
    class _FixedRng:
        def __init__(self, val: float):
            self.val = val
        def random(self) -> float:
            return self.val

    assert select_era_appropriate_template(1985, rng=_FixedRng(0.249)) == "retro"
    assert select_era_appropriate_template(1985, rng=_FixedRng(0.25)) == "vinyl"
    assert select_era_appropriate_template(1987, rng=_FixedRng(0.10)) == "retro"
    assert select_era_appropriate_template(1988, rng=_FixedRng(0.10)) == "cd"


def test_pipeline_era_appropriate_template_selection(temp_db, monkeypatch):
    tid_vinyl = add_track("Vinyl Era Band", "Analog Hit", 1983, chart_peak=5, db_path=temp_db)
    tid_cd = add_track("CD Era Band", "Digital Hit", 1992, chart_peak=3, db_path=temp_db)

    rendered_templates = []

    monkeypatch.setattr("core.pipeline.get_single_artwork", lambda a, t: "/tmp/fake_art.jpg")
    monkeypatch.setattr("core.pipeline.get_or_fetch_track_facts", lambda tr, db_path=None: ["F1", "F2", "F3"])
    monkeypatch.setattr(
        "core.pipeline.prepare_track_audio",
        lambda a, t: {"audio_path": "/tmp/fake.wav", "rms_envelope": [0.5], "duration": 35.0, "source_url": "mock"}
    )
    monkeypatch.setattr(
        "core.pipeline.render_tiktok_video",
        lambda **kwargs: rendered_templates.append(kwargs["template"]) or kwargs["output_path"]
    )

    # Explicit era_appropriate=True picks 'cd' for 1992 track even if template='vinyl' is passed
    post_cd = generate_post_video(
        track_id=tid_cd, template="vinyl", era_appropriate=True, publish=False, db_path=temp_db
    )
    assert post_cd["template_used"] == "cd"
    assert rendered_templates[-1] == "cd"

    # Explicit era_appropriate=True picks 'retro' when RNG < 0.25 for 1983 track
    monkeypatch.setattr("core.pipeline.random.random", lambda: 0.10)
    post_retro = generate_post_video(
        track_id=tid_vinyl, template="cd", era_appropriate=True, publish=False, db_path=temp_db
    )
    assert post_retro["template_used"] == "retro"
    assert rendered_templates[-1] == "retro"

    # Explicit era_appropriate=True picks 'vinyl' when RNG >= 0.25 for 1983 track
    monkeypatch.setattr("core.pipeline.random.random", lambda: 0.50)
    post_vinyl = generate_post_video(
        track_id=tid_vinyl, template="cd", era_appropriate=True, publish=False, db_path=temp_db
    )
    assert post_vinyl["template_used"] == "vinyl"
    assert rendered_templates[-1] == "vinyl"

    # Scheduled/default mode (template=None, era_appropriate=None) honors DB setting
    set_setting("era_appropriate_template", "true", db_path=temp_db)
    post_sched_cd = generate_post_video(track_id=tid_cd, publish=False, db_path=temp_db)
    assert post_sched_cd["template_used"] == "cd"

    set_setting("era_appropriate_template", "false", db_path=temp_db)
    set_setting("default_template", "retro", db_path=temp_db)
    post_sched_default = generate_post_video(track_id=tid_cd, publish=False, db_path=temp_db)
    assert post_sched_default["template_used"] == "retro"

