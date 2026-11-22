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
