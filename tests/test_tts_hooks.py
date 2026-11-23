"""
Tests for GenX71 AutoVJ Voice Hook TTS, Regional UK/US Routing,
Era-Specific DJ Hook Templates, Radio-Ramp Audio Mixing, and Async TTS Queue.
"""

import io
import os
import wave
import math
import struct
import random
import tempfile
import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.db import init_db, add_track, get_post_by_id, get_available_tracks, set_setting
from core.hooks import HOOK_TEMPLATES, build_dj_hook_script, get_era_bucket
from core.tts import (
    save_voice_sample, list_voice_profiles, delete_voice_sample,
    resolve_voice_region, extract_voice_rms,
)
from core.audio import mix_radio_ramp_audio, prepare_track_audio, MAX_CLIP_DURATION, MIN_CLIP_DURATION
from core.video_renderer import compute_fact_schedule
from core.browser_renderer import _build_scene_payload
from core.pipeline import enqueue_tts_post, process_pending_post
from api.app import app


def _make_sine_wav(path: str, duration: float = 5.0, freq: float = 440.0, sr: int = 22050, amp: float = 0.4):
    n = int(sr * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    data = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(data.tobytes())


def test_hook_templates_count_and_era_routing():
    assert len(HOOK_TEMPLATES) >= 25
    assert get_era_bucket(1979) == "late_70s"
    assert get_era_bucket(1985) == "eighties"
    assert get_era_bucket(1994) == "nineties"

    track = {"artist": "A-ha", "title": "Take On Me", "year": 1985, "chart_peak": 1, "chart_region": "US"}
    facts = ["Its iconic pencil-sketch video took 16 weeks and 3,000 rotoscoped drawings to animate."]

    seen_ids = []
    rng = random.Random(42)
    for _ in range(10):
        res = build_dj_hook_script(track, facts=facts, recent_hook_ids=seen_ids, rng=rng)
        assert res["hook_id"] not in seen_ids
        assert len(res["text"]) > 20
        seen_ids.append(res["hook_id"])

    custom = build_dj_hook_script(track, facts=facts, custom_text="  Custom DJ intro right here!  ")
    assert custom["hook_id"] == "custom"
    assert custom["text"] == "Custom DJ intro right here!"


def test_voice_profiles_upload_delete_and_regional_routing():
    with tempfile.TemporaryDirectory() as tmp:
        uk_wav = os.path.join(tmp, "uk_src.wav")
        us_wav = os.path.join(tmp, "us_src.wav")
        _make_sine_wav(uk_wav, duration=3.0, freq=220.0)
        _make_sine_wav(us_wav, duration=3.5, freq=330.0)

        with open(uk_wav, "rb") as f:
            uk_bytes = f.read()
        with open(us_wav, "rb") as f:
            us_bytes = f.read()

        # Initially empty
        assert resolve_voice_region("UK", voices_root=tmp) is None

        # Upload UK only -> both UK and US tracks route to UK fallback
        uk_clip = save_voice_sample("uk", "my_british_voice.wav", uk_bytes, voices_root=tmp)
        assert uk_clip["region"] == "uk"
        assert uk_clip["duration"] >= 2.5
        assert resolve_voice_region("UK", voices_root=tmp) == "uk"
        assert resolve_voice_region("US", voices_root=tmp) == "uk"

        # Upload US with background noise cleanup enabled -> US tracks route to US
        us_clip = save_voice_sample("us", "my_us_voice.wav", us_bytes, voices_root=tmp, denoise=True)
        assert us_clip["region"] == "us"
        assert us_clip["denoised"] is True
        assert "_clean_" in us_clip["filename"]
        assert us_clip["duration"] >= 2.5
        assert resolve_voice_region("US", voices_root=tmp) == "us"
        assert resolve_voice_region("UK", voices_root=tmp) == "uk"
        assert resolve_voice_region("UK", requested_region="us", voices_root=tmp) == "us"

        profiles = list_voice_profiles(voices_root=tmp)
        assert profiles["uk"]["clip_count"] == 1
        assert profiles["us"]["clip_count"] == 1

        # Voice RMS envelope
        rms = extract_voice_rms(uk_clip["path"], fps=30)
        assert len(rms) >= 60
        assert all(0.0 <= v <= 1.0 for v in rms)

        # Delete US clip -> falls back to UK
        assert delete_voice_sample("us", us_clip["filename"], voices_root=tmp) is True
        assert resolve_voice_region("US", voices_root=tmp) == "uk"


def test_radio_ramp_mix_backs_up_start_and_targets_near_59s():
    from core.audio import _load_wav_mono
    with tempfile.TemporaryDirectory() as tmp:
        raw_track = os.path.join(tmp, "full_song.wav")
        tts_wav = os.path.join(tmp, "dj_hook.wav")
        out_wav = os.path.join(tmp, "mixed.wav")

        _make_sine_wav(raw_track, duration=120.0, freq=260.0, sr=22050)
        # Silent TTS wav of 6.0s so we can measure the exact music bed envelope
        _make_sine_wav(tts_wav, duration=6.0, freq=520.0, sr=22050, amp=0.0)

        info = mix_radio_ramp_audio(
            raw_track_wav=raw_track,
            hook_start_sec=30.0,
            tts_wav_path=tts_wav,
            out_wav_path=out_wav,
            target_total_duration=58.0,
        )
        assert os.path.exists(out_wav)
        assert info["pre_roll_start"] == pytest.approx(23.85, abs=0.2)
        assert info["tts_duration"] == pytest.approx(6.0, abs=0.2)
        assert info["ramp_start"] >= 6.1
        assert 57.0 <= info["duration"] <= MAX_CLIP_DURATION

        # Verify music is still ducked at the very end of TTS (5.5s..6.0s) and loud after ramp (7.0s..7.5s)
        samples, sr = _load_wav_mono(out_wav)
        rms_mid_tts = float(np.sqrt(np.mean(samples[int(2.0 * sr):int(2.5 * sr)] ** 2)))
        rms_end_tts = float(np.sqrt(np.mean(samples[int(5.5 * sr):int(6.0 * sr)] ** 2)))
        rms_post_drop = float(np.sqrt(np.mean(samples[int(7.0 * sr):int(7.5 * sr)] ** 2)))
        assert rms_end_tts == pytest.approx(rms_mid_tts, rel=0.15)
        assert rms_post_drop > rms_end_tts * 3.0


def test_long_dj_intro_expands_video_beyond_60s_and_never_chops_sentences():
    from core.hooks import fits_hook_length

    # 1. Verify long facts are NEVER chopped mid-sentence (e.g. ending in "and the!"):
    #    - Under default hook length definitions, oversized multi-sentence hooks drop whole sentences to fit.
    #    - When limits allow, the full sentence is preserved intact.
    track = {"artist": "Donna Summer", "title": "I Feel Love", "year": 1977, "chart_peak": 1, "chart_region": "UK"}
    long_fact = (
        "Produced by Giorgio Moroder and Pete Bellotte using a Moog modular synthesizer "
        "and the pioneering electronic disco rhythm changed dance music forever."
    )
    hook_fit = build_dj_hook_script(track, facts=[long_fact], rng=random.Random(7))
    assert fits_hook_length(hook_fit["text"])
    assert not hook_fit["text"].lower().endswith("and the!")
    assert hook_fit["text"].endswith((".", "!", "?"))

    hook_wide = build_dj_hook_script(track, facts=[long_fact], rng=random.Random(7), max_words=40, max_chars=240)
    assert "changed dance music forever" in hook_wide["text"]
    assert not hook_wide["text"].lower().endswith("and the!")

    # 2. Verify a 15s DJ intro expands total video duration beyond 60s (~65s) with fade-up after 15.15s
    with tempfile.TemporaryDirectory() as tmp:
        raw_track = os.path.join(tmp, "full_song.wav")
        tts_wav = os.path.join(tmp, "long_dj_hook.wav")
        out_wav = os.path.join(tmp, "mixed_long.wav")

        _make_sine_wav(raw_track, duration=150.0, freq=260.0, sr=22050)
        _make_sine_wav(tts_wav, duration=15.0, freq=520.0, sr=22050, amp=0.0)

        info = mix_radio_ramp_audio(
            raw_track_wav=raw_track,
            hook_start_sec=40.0,
            tts_wav_path=tts_wav,
            out_wav_path=out_wav,
        )
        assert info["tts_duration"] == pytest.approx(15.0, abs=0.2)
        assert info["ramp_start"] >= 15.1
        assert info["duration"] >= 64.0  # Dynamically expanded past 60s to fit 15s intro + 50s main music!


def test_fact_schedule_offsets_after_hook_duration():
    facts = ["Fact one.", "Fact two.", "Fact three."]
    sched = compute_fact_schedule(facts, duration=58.0, start_margin=7.5)
    assert len(sched) == 3
    assert sched[0]["start"] >= 7.5
    assert sched[-1]["end"] <= 56.5


def test_scene_payload_includes_hook_overlay():
    with tempfile.TemporaryDirectory() as tmp:
        from PIL import Image
        art = os.path.join(tmp, "art.png")
        Image.new("RGB", (200, 200), (40, 80, 160)).save(art)
        hook = {"text": "Where were you in 1985?", "duration": 6.2, "voice_rms": [0.5] * 180, "region": "uk"}
        payload = _build_scene_payload(
            artist="A-ha", title="Take On Me", year=1985, chart_badge="US #1 (1985)",
            artwork_path=art, duration=58.0, template="hifi", rms_envelope=[0.4] * 100,
            audio_features=None, fact_schedule=[], watermark_path=None, watermark_opacity=0.65,
            hook_overlay=hook,
        )
        assert payload["hook"] is not None
        assert payload["hook"]["text"] == "Where were you in 1985?"
        assert payload["hook"]["duration"] == 6.2


def test_enqueue_tts_pending_and_failure_recovery(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test_tts.db")
        init_db(db_path)
        tid = add_track("Depeche Mode", "Enjoy the Silence", 1990, 6, "UK", 0, "Synth-pop", db_path=db_path)

        from PIL import Image
        art = os.path.join(tmp, "art.png")
        Image.new("RGB", (200, 200), (20, 40, 90)).save(art)

        monkeypatch.setattr("core.pipeline.get_single_artwork", lambda a, t: art)
        monkeypatch.setattr("core.pipeline.resolve_voice_region", lambda track_chart_region=None, requested_region=None: "uk")

        pending = enqueue_tts_post(track_id=tid, template="neon", db_path=db_path)
        assert pending["status"] == "tts_pending"
        assert pending["voice_region"] == "uk"
        assert pending["hook_text"]
        assert pending["hook_template_id"]

        # Track should be reserved so it isn't picked again while tts_pending
        avail_ids = [t["id"] for t in get_available_tracks(db_path=db_path)]
        assert tid not in avail_ids

        # Simulate TTS worker offline -> status becomes tts_failed
        def _fail_synth(*args, **kwargs):
            raise RuntimeError("TTS worker offline")

        monkeypatch.setattr("core.pipeline.synthesize_hook_audio", _fail_synth)
        failed = process_pending_post(pending["id"], db_path=db_path)
        assert failed["status"] == "tts_failed"
        assert "offline" in (failed["error_message"] or "").lower()


def test_tts_api_endpoints():
    client = TestClient(app)
    r = client.get("/api/tts/status")
    assert r.status_code == 200
    body = r.json()
    assert "worker" in body
    assert "profiles" in body
    assert "uk" in body["profiles"]
    assert "us" in body["profiles"]

    r2 = client.get("/api/tts/voices")
    assert r2.status_code == 200
    assert "profiles" in r2.json()
