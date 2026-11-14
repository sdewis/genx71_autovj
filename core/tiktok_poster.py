"""
Stealth TikTok Poster using Playwright + playwright-stealth.
Uses imported browser cookies (sessionid) stored in SQLite, simulates human delays,
and uploads rendered 9:16 videos to TikTok Studio / Creator Center.
"""

import os
import json
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


def parse_cookies(raw_cookies: str) -> List[Dict[str, Any]]:
    """
    Parse cookies from JSON export (Cookie-Editor / EditThisCookie) or raw header string.
    Normalizes Playwright-compatible cookie dicts.
    """
    if not raw_cookies or not raw_cookies.strip():
        return []

    raw_cookies = raw_cookies.strip()
    if raw_cookies.startswith("["):
        data = json.loads(raw_cookies)
        normalized = []
        for c in data:
            cookie = {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain", ".tiktok.com"),
                "path": c.get("path", "/"),
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
            }
            same_site = str(c.get("sameSite", "")).lower()
            if same_site in ("strict", "lax", "none"):
                cookie["sameSite"] = same_site.capitalize()
            else:
                cookie["sameSite"] = "None"
            if cookie["name"] and cookie["value"]:
                normalized.append(cookie)
        return normalized

    # Fallback: raw 'key=val; key2=val2' string
    normalized = []
    for part in raw_cookies.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            normalized.append({
                "name": k.strip(),
                "value": v.strip(),
                "domain": ".tiktok.com",
                "path": "/",
                "secure": True,
                "sameSite": "None"
            })
    return normalized


def check_cookie_health(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Check if TikTok cookies are present and contain a valid sessionid."""
    raw = get_setting("cookies_json", "", db_path=db_path) or ""
    cookies = parse_cookies(raw)
    if not cookies:
        return {"healthy": False, "reason": "No TikTok cookies configured.", "cookie_count": 0}

    names = {c["name"] for c in cookies}
    if "sessionid" not in names and "sessionid_ss" not in names:
        return {
            "healthy": False,
            "reason": "Missing 'sessionid' token in cookies.",
            "cookie_count": len(cookies)
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
