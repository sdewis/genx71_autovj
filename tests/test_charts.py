"""
Tests for core/charts.py batch-based page-shifting loader and Wikipedia parsers.
"""

import os
import tempfile
from unittest.mock import patch
import pytest

from core.db import (
    init_db, add_track, add_to_blacklist,
    get_track_batches, get_tracks_by_batch, count_tracks,
)
from core.charts import (
    GENX_CHART_CATALOG,
    seed_charts_if_empty,
    load_new_chart_batch,
    _parse_billboard_yearend_wikitext,
    _parse_uk_topten_wikitext,
)


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_load_new_batch_pushes_pages_back(temp_db):
    seeded = seed_charts_if_empty(temp_db)
    assert seeded == len(GENX_CHART_CATALOG)
    assert get_track_batches(temp_db) == [1]

    original_keys = {
        (t["artist"].lower(), t["title"].lower())
        for t in get_tracks_by_batch(1, db_path=temp_db)
    }

    # First "Load New Items" -> new batch 2 becomes Page 1, original becomes Page 2
    res1 = load_new_chart_batch(count=25, db_path=temp_db, use_wikipedia=False)
    assert res1["ok"] is True
    assert res1["added"] == 25
    assert res1["batch_num"] == 2
    assert res1["source"] == "catalog"
    assert res1["total_pages"] == 2

    batches = get_track_batches(temp_db)
    assert batches == [2, 1]

    page1_tracks = get_tracks_by_batch(batches[0], db_path=temp_db)
    page2_tracks = get_tracks_by_batch(batches[1], db_path=temp_db)
    assert len(page1_tracks) == 25
    assert len(page2_tracks) == seeded

    page1_keys = {(t["artist"].lower(), t["title"].lower()) for t in page1_tracks}
    page2_keys = {(t["artist"].lower(), t["title"].lower()) for t in page2_tracks}
    assert page2_keys == original_keys
    assert page1_keys.isdisjoint(page2_keys)

    # Second "Load New Items" -> batch 3 becomes Page 1, batch 2 -> Page 2, batch 1 -> Page 3
    res2 = load_new_chart_batch(count=25, db_path=temp_db, use_wikipedia=False)
    assert res2["added"] == 25
    assert res2["batch_num"] == 3
    assert res2["total_pages"] == 3

    batches = get_track_batches(temp_db)
    assert batches == [3, 2, 1]
    assert len(get_tracks_by_batch(batches[0], db_path=temp_db)) == 25
    assert {(t["artist"].lower(), t["title"].lower()) for t in get_tracks_by_batch(batches[1], db_path=temp_db)} == page1_keys
    assert {(t["artist"].lower(), t["title"].lower()) for t in get_tracks_by_batch(batches[2], db_path=temp_db)} == original_keys


def test_custom_track_joins_current_page_1(temp_db):
    seed_charts_if_empty(temp_db)
    load_new_chart_batch(count=25, db_path=temp_db, use_wikipedia=False)
    assert get_track_batches(temp_db) == [2, 1]

    tid = add_track("Blancmange", "Blind Vision", 1983, 10, "UK", db_path=temp_db)
    assert tid is not None
    # Should stay on Page 1 (batch 2), not create batch 3
    assert get_track_batches(temp_db) == [2, 1]
    page1_titles = [t["title"] for t in get_tracks_by_batch(2, db_path=temp_db)]
    assert "Blind Vision" in page1_titles


def test_hybrid_wikipedia_plus_catalog_fallback(temp_db):
    seed_charts_if_empty(temp_db)
    fake_wiki_tracks = [
        {"artist": "Orchestral Test", "title": "Wiki Hit One", "year": 1984, "chart_peak": 3, "chart_region": "US", "is_wildcard": 0, "genre": "Pop/Rock"},
        {"artist": "Synth Test", "title": "Wiki Hit Two", "year": 1986, "chart_peak": 27, "chart_region": "US", "is_wildcard": 1, "genre": "Pop/Rock"},
    ]

    with patch("core.charts.fetch_wikipedia_chart_tracks", return_value=fake_wiki_tracks):
        res = load_new_chart_batch(count=25, db_path=temp_db, use_wikipedia=True)

    assert res["added"] == 25
    assert res["source"] == "hybrid"
    page1_titles = {t["title"] for t in get_tracks_by_batch(res["batch_num"], db_path=temp_db)}
    assert "Wiki Hit One" in page1_titles
    assert "Wiki Hit Two" in page1_titles


def test_blacklisted_tracks_excluded_from_new_batch(temp_db):
    seed_charts_if_empty(temp_db)
    add_to_blacklist("Buzzcocks", "Ever Fallen in Love (With Someone You Shouldn't've)", db_path=temp_db)
    # Load all extended catalog tracks across batches and ensure blacklisted track never enters
    for _ in range(6):
        res = load_new_chart_batch(count=25, db_path=temp_db, use_wikipedia=False)
        if res["added"] == 0:
            break
    all_batches = get_track_batches(temp_db)
    all_titles = {
        (t["artist"], t["title"])
        for b in all_batches
        for t in get_tracks_by_batch(b, db_path=temp_db)
    }
    assert ("Buzzcocks", "Ever Fallen in Love (With Someone You Shouldn't've)") not in all_titles


def test_wikipedia_parsers():
    billboard_sample = """
{| class="wikitable sortable"
|-
! No. !! Title !! Artist(s)
|-
| 1 || "[[When Doves Cry]]" || [[Prince (musician)|Prince]]<ref>Billboard</ref>
|-
| 25 || "[[Ghostbusters (song)|Ghostbusters]]" || [[Ray Parker Jr.]]
|-
| 12
| "[[Jump (Van Halen song)|Jump]]"
| [[Van Halen]]
|}
"""
    parsed_us = _parse_billboard_yearend_wikitext(billboard_sample, 1984)
    assert len(parsed_us) == 3
    assert parsed_us[0]["artist"] == "Prince"
    assert parsed_us[0]["title"] == "When Doves Cry"
    assert parsed_us[0]["chart_peak"] == 1
    assert parsed_us[0]["chart_region"] == "US"
    assert parsed_us[0]["is_wildcard"] == 0
    assert parsed_us[1]["is_wildcard"] == 1
    assert parsed_us[2]["artist"] == "Van Halen"

    uk_sample = """
{| class="wikitable"
|-
| 14 January || "[[Relax (song)|Relax]]" || [[Frankie Goes to Hollywood]] || 1 || 5
|-
| 21 January || "[[99 Red Balloons]]" || [[Nena]] || 1 || 3
|}
"""
    parsed_uk = _parse_uk_topten_wikitext(uk_sample, 1984)
    assert len(parsed_uk) == 2
    assert parsed_uk[0]["artist"] == "Frankie Goes to Hollywood"
    assert parsed_uk[0]["title"] == "Relax"
    assert parsed_uk[0]["chart_region"] == "UK"
    assert parsed_uk[0]["chart_peak"] == 1
    assert parsed_uk[1]["artist"] == "Nena"
    assert parsed_uk[1]["title"] == "99 Red Balloons"
