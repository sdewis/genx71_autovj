"""
Tests for secure TikTok cookie JSON parsing and /api/cookies/import endpoint.
"""

import json
import os
import tempfile
import time
import pytest
from fastapi.testclient import TestClient

from core.db import init_db
from core.tiktok_poster import (
    CookieParseError,
    MAX_COOKIE_COUNT,
    MAX_COOKIE_FILE_BYTES,
    check_cookie_health,
    parse_cookies_json_secure,
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


def test_parse_valid_cookie_editor_array_with_bom():
    raw = "\ufeff" + json.dumps([
        {
            "name": "sessionid",
            "value": "tok_abc123",
            "domain": ".tiktok.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "no_restriction",
            "expirationDate": time.time() + 3600,
        },
        {
            "name": "other_site",
            "value": "should_be_filtered",
            "domain": ".google.com",
            "path": "/",
        },
    ])
    cookies = parse_cookies_json_secure(raw)
    assert len(cookies) == 1
    assert cookies[0]["name"] == "sessionid"
    assert cookies[0]["sameSite"] == "None"
    assert cookies[0]["domain"] == ".tiktok.com"


def test_parse_playwright_storage_state_wrapper():
    raw = json.dumps({
        "cookies": [
            {"name": "sessionid_ss", "value": "xyz789", "domain": "www.tiktok.com", "sameSite": "Lax"}
        ],
        "origins": [],
    })
    cookies = parse_cookies_json_secure(raw)
    assert len(cookies) == 1
    assert cookies[0]["name"] == "sessionid_ss"
    assert cookies[0]["sameSite"] == "Lax"


def test_parse_flat_dict_json():
    raw = json.dumps({"sessionid": "flat_tok_123", "tt_csrf_token": "csrf_abc"})
    cookies = parse_cookies_json_secure(raw)
    assert len(cookies) == 2
    assert {c["name"] for c in cookies} == {"sessionid", "tt_csrf_token"}


def test_parse_large_multi_site_export_filters_before_limit():
    items = [
        {"name": f"other_{i}", "value": "v", "domain": ".example.com"}
        for i in range(MAX_COOKIE_COUNT + 50)
    ]
    items.append({"name": "sessionid", "value": "tt_real", "domain": ".tiktok.com"})
    cookies = parse_cookies_json_secure(json.dumps(items))
    assert len(cookies) == 1
    assert cookies[0]["name"] == "sessionid"


@pytest.mark.parametrize("bad_payload", [
    "",
    "   ",
    "{not valid json",
    json.dumps({"not_cookies": []}),
    json.dumps([123]),
    json.dumps([{"name": 123, "value": "abc"}]),
    json.dumps([{"name": "bad;name", "value": "abc"}]),
    json.dumps([{"name": "sessionid", "value": "abc\r\ninjected"}]),
    json.dumps([{"name": "sessionid", "value": "abc", "path": "no_leading_slash"}]),
    json.dumps([{"name": "sessionid", "value": "abc", "domain": ".evil.com"}]),
    '[{"name":"sessionid","value":"abc\x00"}]',
])
def test_parse_rejects_malformed_or_unsafe_json(bad_payload):
    with pytest.raises(CookieParseError):
        parse_cookies_json_secure(bad_payload)


def test_parse_rejects_oversized_payload():
    oversized = "[" + (" " * (MAX_COOKIE_FILE_BYTES + 10)) + "]"
    with pytest.raises(CookieParseError):
        parse_cookies_json_secure(oversized)


def test_parse_rejects_excessive_cookie_count():
    too_many = json.dumps([
        {"name": f"c{i}", "value": "v", "domain": ".tiktok.com"}
        for i in range(MAX_COOKIE_COUNT + 1)
    ])
    with pytest.raises(CookieParseError):
        parse_cookies_json_secure(too_many)


def test_expired_sessionid_detected(temp_db):
    from core.db import set_setting

    expired = json.dumps([
        {
            "name": "sessionid",
            "value": "old_token",
            "domain": ".tiktok.com",
            "expires": time.time() - 100,
        }
    ])
    set_setting("cookies_json", expired, db_path=temp_db)
    health = check_cookie_health(db_path=temp_db)
    assert health["healthy"] is False
    assert "expired" in health["reason"].lower()


def test_api_cookies_import_endpoint():
    with TestClient(app) as client:
        # Reject non-.json filename
        r_bad_ext = client.post(
            "/api/cookies/import",
            json={"filename": "cookies.txt", "content": '[{"name":"sessionid","value":"abc"}]'},
        )
        assert r_bad_ext.status_code == 400

        # Reject invalid JSON
        r_bad_json = client.post(
            "/api/cookies/import",
            json={"filename": "cookies.json", "content": "{broken"},
        )
        assert r_bad_json.status_code == 400

        # Accept valid .json export and strip non-TikTok domains
        payload = json.dumps([
            {"name": "sessionid", "value": "secret_val", "domain": ".tiktok.com"},
            {"name": "ga", "value": "123", "domain": ".google.com"},
        ])
        r_ok = client.post(
            "/api/cookies/import",
            json={"filename": "C:\\fakepath\\tiktok_cookies.json", "content": payload},
        )
        assert r_ok.status_code == 200
        body = r_ok.json()
        assert body["ok"] is True
        assert body["imported_count"] == 1
        assert body["filename"] == "tiktok_cookies.json"
        assert "secret_val" not in json.dumps(body)

        # Settings endpoint must mask cookies_json
        r_settings = client.get("/api/settings")
        assert r_settings.status_code == 200
        assert r_settings.json()["settings"]["cookies_json"] == "***CONFIGURED***"

        # Clean up test cookie
        client.post("/api/cookies", json={"cookies_json": ""})


def test_parse_insecure_samesite_none_normalized_to_lax():
    raw = json.dumps([
        {
            "name": "ttwid",
            "value": "abc123",
            "domain": ".tiktok.com",
            "secure": False,
            "sameSite": "unspecified",
        }
    ])
    cookies = parse_cookies_json_secure(raw)
    assert len(cookies) == 1
    assert cookies[0]["secure"] is False
    assert cookies[0]["sameSite"] == "Lax"


def test_dismiss_modals_clicks_safe_button_and_avoids_discard():
    from core.tiktok_poster import _dismiss_modals

    clicked = []

    class FakeButton:
        def __init__(self, text, aria="", cls="", visible=True):
            self._text = text
            self._aria = aria
            self._cls = cls
            self._visible = visible

        def is_visible(self):
            return self._visible

        def inner_text(self, timeout=None):
            return self._text

        def get_attribute(self, name):
            if name == "aria-label":
                return self._aria
            if name == "class":
                return self._cls
            return None

        def click(self, timeout=None):
            clicked.append(self._text or self._aria)
            dialog._visible = False

    class FakeLocator:
        def __init__(self, items):
            self._items = items

        def count(self):
            return len(self._items)

        def nth(self, idx):
            return self._items[idx]

        @property
        def first(self):
            return self._items[0] if self._items else FakeButton("", visible=False)

    class FakeDialog:
        def __init__(self, text, buttons):
            self._text = text
            self._buttons = buttons
            self._visible = True

        def is_visible(self):
            return self._visible

        def inner_text(self, timeout=None):
            return self._text

        def locator(self, selector):
            return FakeLocator(self._buttons)

    dialog = FakeDialog(
        "Turn on automatic content checks?",
        [FakeButton("Discard"), FakeButton("Not now")],
    )

    class FakeKeyboard:
        def press(self, key):
            clicked.append(f"key:{key}")

    class FakePage:
        keyboard = FakeKeyboard()

        def locator(self, selector):
            if "dialog" in selector:
                return FakeLocator([dialog])
            return FakeLocator([])

    dismissed = _dismiss_modals(FakePage(), max_rounds=3)
    assert dismissed == 1
    assert clicked == ["Not now"]


def test_apply_stealth_supports_v2_and_v1(monkeypatch):
    import core.tiktok_poster as tp

    calls = []

    class FakeStealthV2:
        def apply_stealth_sync(self, page):
            calls.append(("v2", page))

    monkeypatch.setattr(tp, "_PlaywrightStealth", FakeStealthV2)
    monkeypatch.setattr(tp, "_legacy_stealth_sync", None)
    assert tp._apply_stealth("page1") is True
    assert calls == [("v2", "page1")]

    calls.clear()
    monkeypatch.setattr(tp, "_PlaywrightStealth", None)
    monkeypatch.setattr(tp, "_legacy_stealth_sync", lambda p: calls.append(("v1", p)))
    assert tp._apply_stealth("page2") is True
    assert calls == [("v1", "page2")]

