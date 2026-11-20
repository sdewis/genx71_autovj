"""
Stealth TikTok Poster using Playwright + playwright-stealth.
Uses imported browser cookies (sessionid) stored in SQLite, simulates human delays,
and uploads rendered 9:16 videos to TikTok Studio / Creator Center.
"""

import os
import re
import json
import math
import time
import random
import logging
from typing import Dict, Any, List, Optional
from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = None

from core.db import get_setting, set_setting

logger = logging.getLogger(__name__)

MAX_COOKIE_FILE_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_RAW_COOKIE_ITEMS = 5000
MAX_COOKIE_COUNT = 200
MAX_COOKIE_NAME_LEN = 256
MAX_COOKIE_VALUE_LEN = 8192

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_COOKIE_NAME_INVALID_RE = re.compile(r"[\x00-\x20\x7f;=]")
_DOMAIN_RE = re.compile(
    r"^\.?[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


class CookieParseError(ValueError):
    """Raised when imported cookie data fails security or schema validation."""


def _has_control_chars(val: str) -> bool:
    return bool(_CONTROL_CHAR_RE.search(val))


def _is_tiktok_domain(domain: str) -> bool:
    d = domain.strip().lower()
    if not d or len(d) > 253 or not _DOMAIN_RE.match(d):
        return False
    bare = d.lstrip(".")
    return bare == "tiktok.com" or bare.endswith(".tiktok.com")


def _normalize_same_site(raw_same_site: Any) -> str:
    if not isinstance(raw_same_site, str):
        return "None"
    s = raw_same_site.strip().lower()
    if s in ("strict", "lax", "none"):
        return s.capitalize()
    if s in ("no_restriction", "unspecified", ""):
        return "None"
    return "None"


def _parse_expires(raw_cookie: Dict[str, Any]) -> Optional[float]:
    raw_exp = raw_cookie.get("expirationDate", raw_cookie.get("expires"))
    if raw_exp is None or isinstance(raw_exp, bool):
        return None
    if isinstance(raw_exp, (int, float)):
        exp = float(raw_exp)
    elif isinstance(raw_exp, str) and raw_exp.strip():
        try:
            exp = float(raw_exp.strip())
        except ValueError:
            return None
    else:
        return None

    if not math.isfinite(exp) or exp <= 0:
        return None
    # Convert millisecond timestamps if provided
    if exp > 1e12:
        exp /= 1000.0
    return exp


def parse_cookies_json_secure(raw_json: str) -> List[Dict[str, Any]]:
    """
    Strictly and securely parse a TikTok cookie JSON export.
    Supports:
      - Cookie-Editor / EditThisCookie arrays (`[...]`)
      - Playwright / wrapper objects (`{"cookies": [...]}`)
      - Flat key-value maps (`{"sessionid": "...", ...}`)
    Validates types, sizes, control characters, paths, and restricts domains to *.tiktok.com.
    Raises CookieParseError on malformed or unsafe input.
    """
    if not isinstance(raw_json, str):
        raise CookieParseError("Cookie payload must be text.")

    raw_bytes = raw_json.encode("utf-8", errors="strict")
    if len(raw_bytes) > MAX_COOKIE_FILE_BYTES:
        raise CookieParseError(
            f"Cookie file exceeds maximum allowed size ({MAX_COOKIE_FILE_BYTES // (1024 * 1024)} MB)."
        )

    cleaned = raw_json.lstrip("\ufeff").strip()
    if not cleaned:
        raise CookieParseError("Cookie JSON is empty.")
    if "\x00" in cleaned:
        raise CookieParseError("Cookie JSON contains invalid null bytes.")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise CookieParseError(f"Invalid JSON syntax: {exc.msg}.") from exc

    if isinstance(data, dict):
        if "cookies" in data and isinstance(data["cookies"], list):
            cookie_items = data["cookies"]
        elif data and all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            cookie_items = [
                {"name": k, "value": v, "domain": ".tiktok.com", "path": "/"}
                for k, v in data.items()
            ]
        else:
            raise CookieParseError(
                "JSON object must contain a 'cookies' array or a map of cookie names to string values."
            )
    elif isinstance(data, list):
        cookie_items = data
    else:
        raise CookieParseError("Cookie JSON must be an array of cookie objects.")

    if len(cookie_items) > MAX_RAW_COOKIE_ITEMS:
        raise CookieParseError(
            f"Too many cookie entries ({len(cookie_items)}); maximum allowed is {MAX_RAW_COOKIE_ITEMS}."
        )

    normalized: List[Dict[str, Any]] = []
    for idx, item in enumerate(cookie_items):
        if not isinstance(item, dict):
            raise CookieParseError(f"Cookie entry at index {idx} must be a JSON object.")

        raw_domain = item.get("domain", ".tiktok.com")
        if raw_domain is None or raw_domain == "":
            domain = ".tiktok.com"
        elif isinstance(raw_domain, str):
            domain = raw_domain.strip().lower()
            if _has_control_chars(domain):
                raise CookieParseError(f"Cookie entry at index {idx} has invalid domain characters.")
            if not _is_tiktok_domain(domain):
                # Skip non-TikTok cookies if user exported a full browser cookie jar
                continue
        else:
            raise CookieParseError(f"Cookie entry at index {idx} has invalid domain type.")

        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise CookieParseError(f"Cookie entry at index {idx} must have string 'name' and 'value'.")

        name = name.strip()
        if not name or not value:
            continue
        if len(name) > MAX_COOKIE_NAME_LEN or len(value) > MAX_COOKIE_VALUE_LEN:
            raise CookieParseError(f"Cookie '{name[:32]}' exceeds maximum field length.")
        if _COOKIE_NAME_INVALID_RE.search(name):
            raise CookieParseError(f"Cookie name '{name[:32]}' contains invalid characters.")
        if _has_control_chars(value):
            raise CookieParseError(f"Cookie '{name}' value contains invalid control characters.")

        raw_path = item.get("path", "/")
        if raw_path is None or raw_path == "":
            path = "/"
        elif isinstance(raw_path, str):
            path = raw_path.strip()
            if _has_control_chars(path) or not path.startswith("/"):
                raise CookieParseError(f"Cookie '{name}' has an invalid path.")
        else:
            raise CookieParseError(f"Cookie '{name}' has invalid path type.")

        if len(normalized) >= MAX_COOKIE_COUNT:
            raise CookieParseError(
                f"Too many TikTok cookies (exceeds maximum of {MAX_COOKIE_COUNT})."
            )

        cookie: Dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": bool(item.get("secure", True)),
            "httpOnly": bool(item.get("httpOnly", False)),
            "sameSite": _normalize_same_site(item.get("sameSite", "")),
        }

        exp = _parse_expires(item)
        if exp is not None:
            cookie["expires"] = exp

        normalized.append(cookie)

    if not normalized:
        raise CookieParseError("No valid TikTok (*.tiktok.com) cookies found in JSON.")

    return normalized


def parse_cookies(raw_cookies: str) -> List[Dict[str, Any]]:
    """
    Parse cookies from JSON export (Cookie-Editor / EditThisCookie / Playwright) or raw header string.
    Normalizes Playwright-compatible cookie dicts safely.
    """
    if not isinstance(raw_cookies, str) or not raw_cookies.strip():
        return []

    cleaned = raw_cookies.lstrip("\ufeff").strip()
    if cleaned.startswith("[") or cleaned.startswith("{"):
        try:
            return parse_cookies_json_secure(cleaned)
        except CookieParseError as exc:
            logger.warning("Failed to parse stored cookie JSON: %s", exc)
            return []

    if len(cleaned.encode("utf-8", errors="ignore")) > MAX_COOKIE_FILE_BYTES:
        return []

    # Fallback: raw 'key=val; key2=val2' string
    normalized: List[Dict[str, Any]] = []
    for part in cleaned.split(";"):
        if len(normalized) >= MAX_COOKIE_COUNT:
            break
        if "=" in part:
            k, v = part.strip().split("=", 1)
            k = k.strip()
            v = v.strip()
            if (
                k
                and v
                and len(k) <= MAX_COOKIE_NAME_LEN
                and len(v) <= MAX_COOKIE_VALUE_LEN
                and not _COOKIE_NAME_INVALID_RE.search(k)
                and not _has_control_chars(v)
            ):
                normalized.append({
                    "name": k,
                    "value": v,
                    "domain": ".tiktok.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "None",
                })
    return normalized


def check_cookie_health(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Check if TikTok cookies are present and contain a valid, unexpired sessionid."""
    raw = get_setting("cookies_json", "", db_path=db_path) or ""
    cookies = parse_cookies(raw)
    if not cookies:
        return {"healthy": False, "reason": "No TikTok cookies configured.", "cookie_count": 0}

    session_cookies = [c for c in cookies if c["name"] in ("sessionid", "sessionid_ss")]
    if not session_cookies:
        return {
            "healthy": False,
            "reason": "Missing 'sessionid' token in cookies.",
            "cookie_count": len(cookies),
        }

    now = time.time()
    all_expired = all("expires" in c and c["expires"] < now for c in session_cookies)
    if all_expired:
        return {
            "healthy": False,
            "reason": "TikTok 'sessionid' cookie has expired. Please import a fresh cookie .json file.",
            "cookie_count": len(cookies),
        }

    return {"healthy": True, "reason": "Session cookies present.", "cookie_count": len(cookies)}


def _human_sleep(min_s: float = 1.2, max_s: float = 3.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def build_caption(artist: str, title: str, year: int, chart_info: str) -> str:
    decade = f"{(year // 10) * 10}s"
    return (
        f"{artist} - {title} ({year}) | {chart_info} 🎶✨\n"
        f"#GenX #{decade}Music #VinylTikTok #ThrowbackMusic #ClassicHits #SeanyGenX"
    )


def upload_to_tiktok(
    video_path: str,
    caption: str,
    auto_publish: bool = False,
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Upload video to TikTok Studio using stealth Playwright & session cookies.
    If auto_publish is False, uploads & fills caption then saves/leaves as draft.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    health = check_cookie_health(db_path=db_path)
    if not health["healthy"]:
        raise RuntimeError(f"Cookie error: {health['reason']}")

    cookies = parse_cookies(get_setting("cookies_json", "", db_path=db_path) or "")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="en-GB"
        )
        context.add_cookies(cookies)
        page = context.new_page()
        if stealth_sync:
            stealth_sync(page)

        try:
            logger.info("Navigating to TikTok Studio upload page...")
            page.goto("https://www.tiktok.com/tiktokstudio/upload", wait_until="domcontentloaded", timeout=45000)
            _human_sleep(2.5, 4.5)

            if "login" in page.url.lower():
                raise RuntimeError("TikTok session expired or invalid (redirected to login).")

            # Locate file input
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(video_path, timeout=30000)
            logger.info("Video file attached, waiting for upload & processing...")
            _human_sleep(5.0, 8.0)

            # Wait for caption editor
            caption_box = page.locator('[contenteditable="true"]').first
            caption_box.wait_for(state="visible", timeout=60000)
            caption_box.click()
            _human_sleep(0.6, 1.4)

            # Clear existing filename text and type caption with human cadence
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            _human_sleep(0.3, 0.7)
            page.keyboard.type(caption, delay=random.randint(25, 55))
            _human_sleep(2.0, 3.5)

            if auto_publish:
                post_btn = page.locator('button:has-text("Post")').last
                post_btn.wait_for(state="visible", timeout=30000)
                post_btn.click()
                _human_sleep(4.0, 7.0)
                status = "posted"
                msg = "Published to TikTok."
            else:
                # Save as draft if button exists, otherwise leave uploaded
                draft_btn = page.locator('button:has-text("Save draft"), button:has-text("Draft")').first
                if draft_btn.count() > 0 and draft_btn.is_visible():
                    draft_btn.click()
                    _human_sleep(2.5, 4.0)
                status = "draft"
                msg = "Uploaded to TikTok Drafts."

            return {"status": status, "message": msg}
        finally:
            browser.close()
