"""
Tests for POST /api/posts/{post_id}/rerender endpoint.
Verifies atomic deletion + re-render initiation, pre-flight guards that
preserve the existing render on error, and TTS vs non-TTS flows.
"""

import os
import tempfile
import threading
import pytest
from fastapi import HTTPException

import api.app as api_app
import core.db as core_db


@pytest.fixture
def isolated_env(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    core_db.init_db(db_path)

    out_dir = tempfile.mkdtemp(prefix="genx71_out_")
    tts_dir = tempfile.mkdtemp(prefix="genx71_tts_")

    monkeypatch.setattr(core_db, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(api_app, "OUTPUT_DIR", out_dir)
    monkeypatch.setattr(api_app, "TTS_CACHE_DIR", tts_dir)

    # Reset shared in-memory job states
    api_app.gen_state.update({
        "running": False,
        "progress": 0,
        "message": "Idle",
        "last_post": None,
        "error": None,
    })
    api_app.publish_state.update({
        "running": False,
        "post_id": None,
        "label": "",
        "progress": 0,
        "message": "Idle",
        "status": None,
        "error": None,
    })

    yield {
        "db_path": db_path,
        "out_dir": out_dir,
        "tts_dir": tts_dir,
    }

    for p in (db_path,):
        if os.path.exists(p):
            os.remove(p)
    for d in (out_dir, tts_dir):
        if os.path.isdir(d):
            for fname in os.listdir(d):
                try:
                    os.remove(os.path.join(d, fname))
                except OSError:
                    pass
            try:
                os.rmdir(d)
            except OSError:
                pass


def test_rerender_deletes_old_files_and_starts_generation(isolated_env, monkeypatch):
    tid = core_db.add_track("New Order", "Blue Monday", 1983, 9, "UK")
    video_file = os.path.join(isolated_env["out_dir"], "old_render.mp4")
    tts_file = os.path.join(isolated_env["tts_dir"], "old_hook.wav")
    with open(video_file, "wb") as f:
        f.write(b"fake-mp4")
    with open(tts_file, "wb") as f:
        f.write(b"fake-wav")

    pid = core_db.record_post(
        track_id=tid,
        artist="New Order",
        title="Blue Monday",
        year=1983,
        chart_info="UK #9",
        template_used="vinyl",
        video_path=video_file,
        tts_audio_path=tts_file,
        status="rendered",
    )

    done = threading.Event()
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        done.set()
        return {"id": pid + 1, "track_id": kwargs["track_id"], "status": "rendered"}

    monkeypatch.setattr(api_app, "generate_post_video", fake_generate)

    res = api_app.api_rerender_post(
        pid,
        api_app.GenerateRequest(template="neon", era_appropriate=False, auto_queue=True, tts_enabled=False),
    )

    assert res["started"] is True
    assert res["queued_tts"] is False
    assert res["deleted_id"] == pid
    assert res["track_id"] == tid
    assert res["file_removed"] is True
    assert res["tts_removed"] is True

    assert core_db.get_post_by_id(pid) is None
    assert not os.path.exists(video_file)
    assert not os.path.exists(tts_file)

    assert done.wait(timeout=3.0)
    assert captured["track_id"] == tid
    assert captured["template"] == "neon"
    assert captured["era_appropriate"] is False
    assert captured["auto_queue"] is True
    assert captured["tts_enabled"] is False


def test_rerender_tts_flow(isolated_env, monkeypatch):
    tid = core_db.add_track("The Cure", "Just Like Heaven", 1987, 29, "UK")
    pid = core_db.record_post(
        track_id=tid,
        artist="The Cure",
        title="Just Like Heaven",
        year=1987,
        chart_info="UK #29",
        template_used="hifi",
        status="rendered",
    )

    monkeypatch.setattr(api_app, "resolve_voice_region", lambda requested_region=None: "uk")
    enqueued_kwargs = {}

    def fake_enqueue(**kwargs):
        enqueued_kwargs.update(kwargs)
        return {"id": 999, "track_id": kwargs["track_id"], "artist": "The Cure", "title": "Just Like Heaven", "status": "tts_pending", "voice_region": "uk"}

    monkeypatch.setattr(api_app, "enqueue_tts_post", fake_enqueue)
    monkeypatch.setattr(api_app.tts_worker, "start", lambda: None)
    monkeypatch.setattr(api_app.tts_worker, "wake", lambda: None)

    res = api_app.api_rerender_post(
        pid,
        api_app.GenerateRequest(tts_enabled=True, voice_region="uk", custom_hook_text="Classic 87!"),
    )

    assert res["started"] is True
    assert res["queued_tts"] is True
    assert res["deleted_id"] == pid
    assert res["track_id"] == tid
    assert res["post"]["id"] == 999
    assert core_db.get_post_by_id(pid) is None
    assert enqueued_kwargs["track_id"] == tid
    assert enqueued_kwargs["voice_region"] == "uk"
    assert enqueued_kwargs["custom_hook_text"] == "Classic 87!"


def test_rerender_preserves_post_when_generation_busy(isolated_env):
    tid = core_db.add_track("Depeche Mode", "Enjoy the Silence", 1990, 6, "UK")
    pid = core_db.record_post(
        track_id=tid,
        artist="Depeche Mode",
        title="Enjoy the Silence",
        year=1990,
        status="rendered",
    )
    api_app.gen_state["running"] = True

    with pytest.raises(HTTPException) as exc:
        api_app.api_rerender_post(pid, api_app.GenerateRequest(tts_enabled=False))

    assert exc.value.status_code == 409
    assert core_db.get_post_by_id(pid) is not None


def test_rerender_preserves_post_when_tts_missing_voice(isolated_env, monkeypatch):
    tid = core_db.add_track("Tears for Fears", "Shout", 1984, 4, "UK")
    pid = core_db.record_post(
        track_id=tid,
        artist="Tears for Fears",
        title="Shout",
        year=1984,
        status="rendered",
    )
    monkeypatch.setattr(api_app, "resolve_voice_region", lambda requested_region=None: None)

    with pytest.raises(HTTPException) as exc:
        api_app.api_rerender_post(pid, api_app.GenerateRequest(tts_enabled=True))

    assert exc.value.status_code == 400
    assert core_db.get_post_by_id(pid) is not None


def test_rerender_guards_rendering_and_uploading_posts(isolated_env):
    tid = core_db.add_track("A-ha", "Take On Me", 1985, 2, "UK")
    pid_rendering = core_db.record_post(
        track_id=tid, artist="A-ha", title="Take On Me", year=1985, status="rendering"
    )
    with pytest.raises(HTTPException) as exc1:
        api_app.api_rerender_post(pid_rendering, api_app.GenerateRequest(tts_enabled=False))
    assert exc1.value.status_code == 409
    assert core_db.get_post_by_id(pid_rendering) is not None

    pid_uploading = core_db.record_post(
        track_id=tid, artist="A-ha", title="Take On Me", year=1985, status="rendered"
    )
    api_app.publish_state["running"] = True
    api_app.publish_state["post_id"] = pid_uploading
    with pytest.raises(HTTPException) as exc2:
        api_app.api_rerender_post(pid_uploading, api_app.GenerateRequest(tts_enabled=False))
    assert exc2.value.status_code == 409
    assert core_db.get_post_by_id(pid_uploading) is not None


def test_rerender_resolves_track_when_track_id_missing(isolated_env, monkeypatch):
    tid = core_db.add_track("Duran Duran", "Rio", 1982, 9, "UK")
    pid = core_db.record_post(
        track_id=None,
        artist="Duran Duran",
        title="Rio",
        year=1982,
        status="rendered",
    )

    done = threading.Event()
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        done.set()
        return {"id": pid + 1, "track_id": kwargs["track_id"], "status": "rendered"}

    monkeypatch.setattr(api_app, "generate_post_video", fake_generate)

    res = api_app.api_rerender_post(pid, api_app.GenerateRequest(tts_enabled=False))
    assert res["started"] is True
    assert res["track_id"] == tid
    assert done.wait(timeout=3.0)
    assert captured["track_id"] == tid
