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

from core.db import init_db
from core.charts import seed_charts_if_empty, select_next_track, format_chart_badge
from core.artwork import _create_fallback_artwork, extract_palette
from core.audio import find_intelligent_clip_window, extract_rms_envelope
from core.video_renderer import render_tiktok_video
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


def test_audio_clipping_and_rms():
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "test.wav")
        _make_test_wav(wav_path, duration=5.0)
        start, end = find_intelligent_clip_window(wav_path, target_duration=3.0)
        assert 0.0 <= start < end <= 5.0
        rms = extract_rms_envelope(wav_path, fps=30)
        assert len(rms) >= 140
        assert all(0.0 <= v <= 1.0 for v in rms)


@pytest.mark.parametrize("template", ["vinyl", "cd", "retro"])
def test_video_render_templates(template):
    with tempfile.TemporaryDirectory() as tmp:
        art_path = os.path.join(tmp, "art.jpg")
        _create_fallback_artwork("New Order", "Blue Monday", art_path)
        palette = extract_palette(art_path)
        assert "primary" in palette and "accent" in palette

        wav_path = os.path.join(tmp, "audio.wav")
        _make_test_wav(wav_path, duration=1.5)
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
            duration=1.5,
            output_path=out_mp4,
            template=template
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
