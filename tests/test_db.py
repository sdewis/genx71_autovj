"""
Tests for core/db.py database and deduplication logic.
"""

import os
import tempfile
import pytest
from core.db import (
    init_db, add_track, get_all_tracks, is_track_posted,
    record_post, update_post_status, is_blacklisted, add_to_blacklist,
    remove_from_blacklist, get_available_tracks, get_setting, set_setting
)


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_init_and_settings(temp_db):
    val = get_setting("daily_post_times", db_path=temp_db)
    assert val == "12:30,19:15"
    set_setting("daily_post_times", "11:00,18:00", db_path=temp_db)
    assert get_setting("daily_post_times", db_path=temp_db) == "11:00,18:00"


def test_tracks_and_deduplication(temp_db):
    t1 = add_track("New Order", "Blue Monday", 1983, 9, "UK", is_wildcard=0, db_path=temp_db)
    t2 = add_track("Joy Division", "Love Will Tear Us Apart", 1980, 13, "UK", is_wildcard=0, db_path=temp_db)
    t3 = add_track("The Chameleons", "Up the Down Escalator", 1983, 32, "UK", is_wildcard=1, db_path=temp_db)

    assert t1 is not None
    assert t2 is not None
    assert t3 is not None

    # Duplicate add should return existing id without crashing
    t1_dup = add_track("New Order", "Blue Monday", 1983, 9, "UK", db_path=temp_db)
    assert t1_dup == t1

    assert not is_track_posted("New Order", "Blue Monday", db_path=temp_db)

    # Post Blue Monday
    p_id = record_post(t1, "New Order", "Blue Monday", 1983, chart_info="UK #9", status="posted", db_path=temp_db)
    assert p_id > 0
    assert is_track_posted("New Order", "Blue Monday", db_path=temp_db)

    # Available tracks should not include Blue Monday
    available = get_available_tracks(db_path=temp_db)
    avail_titles = [t["title"] for t in available]
    assert "Blue Monday" not in avail_titles
    assert "Love Will Tear Us Apart" in avail_titles
    assert "Up the Down Escalator" in avail_titles

    # Wildcard filter
    wildcards = get_available_tracks(is_wildcard=True, db_path=temp_db)
    assert len(wildcards) == 1
    assert wildcards[0]["title"] == "Up the Down Escalator"


def test_blacklist(temp_db):
    t1 = add_track("Wham!", "Wake Me Up Before You Go-Go", 1984, 1, "UK", db_path=temp_db)
    assert not is_blacklisted("Wham!", db_path=temp_db)

    b_id = add_to_blacklist("Wham!", reason="User requested skip", db_path=temp_db)
    assert is_blacklisted("Wham!", db_path=temp_db)
    assert is_blacklisted("Wham!", "Wake Me Up Before You Go-Go", db_path=temp_db)

    # Available tracks should exclude blacklisted artist
    available = get_available_tracks(db_path=temp_db)
    assert not any(t["artist"] == "Wham!" for t in available)

    # Remove from blacklist
    remove_from_blacklist(b_id, db_path=temp_db)
    assert not is_blacklisted("Wham!", db_path=temp_db)
    available_again = get_available_tracks(db_path=temp_db)
    assert any(t["artist"] == "Wham!" for t in available_again)
