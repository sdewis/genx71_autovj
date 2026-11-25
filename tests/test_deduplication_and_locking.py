"""
Regression tests for duplicate-post prevention:
  1. Synchronous TTS generation (daemon -> generate_post_video) never races with TTSJobWorker.
  2. Concurrent process_pending_post calls on the same post_id execute only once.
  3. Atomic claim_post_for_publish prevents concurrent or repeat TikTok uploads.
  4. Daemon negative-jitter runs never re-fire the same scheduled time slot.
  5. Daemon queued-post publishing respects atomic publish locks.
  6. API publish endpoint blocks already-posted / currently-publishing posts.
  7. Startup recovery safely handles interrupted renders and uploads without auto-republishing.
"""

import os
import tempfile
import threading
import time
from datetime import datetime

import pytest
from fastapi import HTTPException

import api.app as api_app
import core.db as core_db
from core.db import (
    init_db,
    add_track,
    record_post,
    get_post_by_id,
    get_pending_tts_posts,
    claim_next_pending_tts_post,
    claim_post_for_publish,
    queue_post,
    set_setting,
    recover_interrupted_posts,
)
from core.pipeline import generate_post_video, enqueue_tts_post, process_pending_post
from daemon import AutoVJDaemon


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_sync_tts_generate_does_not_race_with_tts_worker(temp_db, monkeypatch):
    """
    When daemon calls generate_post_video(publish=True) with TTS enabled,
    the post must be created in 'tts_processing' so TTSJobWorker cannot pick it up
    and upload a duplicate to TikTok.
    """
    tid = add_track("New Order", "Blue Monday", 1983, 9, "UK", db_path=temp_db)
    set_setting("tts_enabled", "true", db_path=temp_db)
    set_setting("auto_publish", "true", db_path=temp_db)

    upload_calls = []
    worker_saw_pending = []

    monkeypatch.setattr("core.pipeline.resolve_voice_region", lambda **kwargs: "uk")
    monkeypatch.setattr("core.pipeline.get_single_artwork", lambda a, t: "/tmp/fake_art.jpg")
    monkeypatch.setattr("core.pipeline.get_or_fetch_track_facts", lambda tr, db_path=None: ["F1", "F2", "F3"])
    monkeypatch.setattr("core.pipeline.check_cookie_health", lambda db_path=None: {"healthy": True, "reason": "ok"})

    def fake_synth(text, region, output_path, speed=1.05, db_path=None):
        # While TTS synthesis is in progress, simulate TTSJobWorker polling the DB
        worker_saw_pending.append(get_pending_tts_posts(db_path=temp_db))
        worker_saw_pending.append(claim_next_pending_tts_post(db_path=temp_db))
        return {"audio_path": output_path, "duration": 5.0, "voice_rms": [0.5] * 150}

    monkeypatch.setattr("core.pipeline.synthesize_hook_audio", fake_synth)
    monkeypatch.setattr(
        "core.pipeline.prepare_track_audio",
        lambda a, t, tts_audio_path=None: {
            "audio_path": "/tmp/fake.wav",
            "rms_envelope": [0.5],
            "duration": 40.0,
            "source_url": "mock",
        },
    )
    monkeypatch.setattr("core.pipeline.render_tiktok_video", lambda **kwargs: kwargs["output_path"])
    monkeypatch.setattr(
        "core.pipeline.upload_to_tiktok",
        lambda video_path, caption, auto_publish=False, db_path=None: (
            upload_calls.append((video_path, caption)) or {"status": "posted", "message": "Published"}
        ),
    )

    post = generate_post_video(track_id=tid, publish=True, db_path=temp_db)
    assert post["status"] == "posted"
    assert len(upload_calls) == 1
    assert worker_saw_pending == [[], None]


def test_concurrent_process_pending_post_runs_only_once(temp_db, monkeypatch):
    tid = add_track("The Cure", "Just Like Heaven", 1987, 29, "UK", db_path=temp_db)
    monkeypatch.setattr("core.pipeline.resolve_voice_region", lambda **kwargs: "uk")
    monkeypatch.setattr("core.pipeline.get_single_artwork", lambda a, t: "/tmp/fake_art.jpg")
    monkeypatch.setattr("core.pipeline.get_or_fetch_track_facts", lambda tr, db_path=None: ["F1", "F2", "F3"])
    monkeypatch.setattr("core.pipeline.check_cookie_health", lambda db_path=None: {"healthy": True, "reason": "ok"})

    pending = enqueue_tts_post(track_id=tid, publish=True, db_path=temp_db)
    entered = threading.Event()
    release = threading.Event()
    render_calls = []
    upload_calls = []

    def slow_synth(text, region, output_path, speed=1.05, db_path=None):
        entered.set()
        release.wait(timeout=5.0)
        return {"audio_path": output_path, "duration": 5.0, "voice_rms": [0.5] * 150}

    monkeypatch.setattr("core.pipeline.synthesize_hook_audio", slow_synth)
    monkeypatch.setattr(
        "core.pipeline.prepare_track_audio",
        lambda a, t, tts_audio_path=None: {
            "audio_path": "/tmp/fake.wav",
            "rms_envelope": [0.5],
            "duration": 40.0,
            "source_url": "mock",
        },
    )
    monkeypatch.setattr(
        "core.pipeline.render_tiktok_video",
        lambda **kwargs: render_calls.append(kwargs["output_path"]) or kwargs["output_path"],
    )
    monkeypatch.setattr(
        "core.pipeline.upload_to_tiktok",
        lambda video_path, caption, auto_publish=False, db_path=None: (
            upload_calls.append(video_path) or {"status": "posted", "message": "Published"}
        ),
    )

    t1 = threading.Thread(target=process_pending_post, kwargs={"post_id": pending["id"], "db_path": temp_db})
    t1.start()
    assert entered.wait(timeout=3.0)

    # Second concurrent call must immediately skip without rendering or uploading
    res2 = process_pending_post(pending["id"], db_path=temp_db)
    assert res2["id"] == pending["id"]

    release.set()
    t1.join(timeout=5.0)

    # Third call after completion must also skip
    res3 = process_pending_post(pending["id"], db_path=temp_db)
    assert res3["status"] == "posted"
    assert len(render_calls) == 1
    assert len(upload_calls) == 1


def test_claim_post_for_publish_blocks_concurrent_and_already_posted(temp_db):
    tid = add_track("Depeche Mode", "Enjoy the Silence", 1990, 6, "UK", db_path=temp_db)
    pid = record_post(tid, "Depeche Mode", "Enjoy the Silence", 1990, status="rendered", db_path=temp_db)
    queue_post(pid, db_path=temp_db)

    first = claim_post_for_publish(pid, db_path=temp_db)
    assert first is not None
    assert first["status"] == "publishing"
    assert first["queue_position"] is None

    # Second claim while publishing must fail
    second = claim_post_for_publish(pid, db_path=temp_db)
    assert second is None

    # Once posted, cannot be claimed again unless allow_republish=True
    core_db.update_post_status(pid, "posted", db_path=temp_db)
    third = claim_post_for_publish(pid, allow_republish=False, db_path=temp_db)
    assert third is None


def test_daemon_negative_jitter_does_not_refire_same_slot(temp_db, monkeypatch):
    """
    If the 12:30 slot fires early at 12:10 due to negative jitter (-20m),
    rescheduling at 12:12 must NOT pick the 12:30 slot again (must advance to 19:15).
    """
    set_setting("schedule_enabled", "true", db_path=temp_db)
    set_setting("daily_post_times", "12:30,19:15", db_path=temp_db)
    set_setting("jitter_minutes", "30", db_path=temp_db)

    d = AutoVJDaemon(db_path=temp_db)

    # Force negative jitter (-20 min) at 11:00 -> picks 12:30 slot at ~12:10
    monkeypatch.setattr("daemon.random.gauss", lambda mu, sigma: -20.0)
    monkeypatch.setattr("daemon.random.randint", lambda a, b: 0)
    first_run = d._compute_next_jittered_run(now=datetime(2026, 11, 25, 11, 0, 0))
    assert first_run == datetime(2026, 11, 25, 12, 10, 0)
    assert d._current_slot_base == datetime(2026, 11, 25, 12, 30, 0)

    # Simulate that 12:30 slot executing at 12:10 and persisting last_scheduled_slot
    set_setting("last_scheduled_slot", d._current_slot_base.isoformat(), db_path=temp_db)

    # At 12:12, even with positive jitter (+5 min -> 12:35), 12:30 must be skipped!
    monkeypatch.setattr("daemon.random.gauss", lambda mu, sigma: 5.0)
    next_run = d._compute_next_jittered_run(now=datetime(2026, 11, 25, 12, 12, 0))
    assert next_run == datetime(2026, 11, 25, 19, 20, 0)
    assert d._current_slot_base == datetime(2026, 11, 25, 19, 15, 0)


def test_daemon_queued_post_skips_when_already_claimed(temp_db, monkeypatch):
    tid = add_track("Blondie", "Heart of Glass", 1979, 1, "UK", db_path=temp_db)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(b"fake-video")
        vid_path = tf.name

    try:
        pid = record_post(tid, "Blondie", "Heart of Glass", 1979, video_path=vid_path, status="rendered", db_path=temp_db)
        queue_post(pid, db_path=temp_db)

        upload_calls = []
        d = AutoVJDaemon(db_path=temp_db)
        monkeypatch.setattr("daemon.check_cookie_health", lambda db_path=None: {"healthy": True, "reason": "ok"})
        # Simulate another thread claiming the post right before daemon claims it
        monkeypatch.setattr("daemon.claim_post_for_publish", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "daemon.upload_to_tiktok",
            lambda *args, **kwargs: upload_calls.append(args) or {"status": "posted", "message": "ok"},
        )
        monkeypatch.setattr(d, "reschedule_next", lambda: None)

        d._run_scheduled_job()
        assert upload_calls == []
    finally:
        if os.path.exists(vid_path):
            os.remove(vid_path)


def test_api_publish_blocks_already_posted_and_publishing(temp_db, monkeypatch):
    monkeypatch.setattr(core_db, "DEFAULT_DB_PATH", temp_db)
    monkeypatch.setattr(api_app, "check_cookie_health", lambda: {"healthy": True, "reason": "ok"})
    api_app.publish_state["running"] = False

    tid = add_track("A-ha", "Take On Me", 1985, 1, "US", db_path=temp_db)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(b"fake-video")
        vid_path = tf.name

    try:
        pid_posted = record_post(tid, "A-ha", "Take On Me", 1985, video_path=vid_path, status="posted", db_path=temp_db)
        with pytest.raises(HTTPException) as exc1:
            api_app.api_publish_post(pid_posted)
        assert exc1.value.status_code == 409

        pid_pub = record_post(tid, "A-ha", "Take On Me", 1985, video_path=vid_path, status="publishing", db_path=temp_db)
        with pytest.raises(HTTPException) as exc2:
            api_app.api_publish_post(pid_pub)
        assert exc2.value.status_code == 409
    finally:
        if os.path.exists(vid_path):
            os.remove(vid_path)


def test_recover_interrupted_posts_on_startup(temp_db):
    t1 = add_track("Artist 1", "Song 1", 1981, 1, "UK", db_path=temp_db)
    t2 = add_track("Artist 2", "Song 2", 1982, 2, "UK", db_path=temp_db)
    t3 = add_track("Artist 3", "Song 3", 1983, 3, "UK", db_path=temp_db)

    p_tts = record_post(t1, "Artist 1", "Song 1", 1981, status="tts_processing", tts_enabled=1, db_path=temp_db)
    p_render = record_post(t2, "Artist 2", "Song 2", 1982, status="rendering", tts_enabled=0, db_path=temp_db)
    p_pub = record_post(t3, "Artist 3", "Song 3", 1983, status="publishing", tts_enabled=1, db_path=temp_db)

    res = recover_interrupted_posts(db_path=temp_db)
    assert res == {"requeued_tts": 1, "failed_renders": 1, "interrupted_publishes": 1}

    assert get_post_by_id(p_tts, db_path=temp_db)["status"] == "tts_pending"
    assert get_post_by_id(p_render, db_path=temp_db)["status"] == "failed"
    assert get_post_by_id(p_pub, db_path=temp_db)["status"] == "failed"


def test_prepare_track_audio_rejects_truncated_clip(monkeypatch):
    import wave
    import numpy as np
    from core.audio import prepare_track_audio

    with tempfile.TemporaryDirectory() as tmp:
        def _write_wav(path: str, duration: float, sr: int = 8000):
            n = int(sr * duration)
            samples = np.zeros(n, dtype=np.int16)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(samples.tobytes())

        raw_src = os.path.join(tmp, "src.wav")
        _write_wav(raw_src, duration=40.0)

        monkeypatch.setattr("core.audio.fetch_youtube_audio", lambda a, t, base: raw_src)

        def fake_run(cmd, **kwargs):
            out_path = cmd[-1]
            # First ffmpeg call converts yt_out -> raw_wav (40s); second produces a truncated 1.5s output
            if out_path.endswith("_raw.wav"):
                _write_wav(out_path, duration=40.0)
            else:
                _write_wav(out_path, duration=1.5)

        monkeypatch.setattr("core.audio.subprocess.run", fake_run)

        with pytest.raises(RuntimeError, match="unexpectedly short"):
            prepare_track_audio("Test Artist", "Truncated Song", cache_dir=tmp)

