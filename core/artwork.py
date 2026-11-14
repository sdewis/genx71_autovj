"""
High-resolution Single Artwork Fetcher and Palette Extractor for GenX71 AutoVJ.
Queries iTunes Search API and MusicBrainz Cover Art Archive, prioritizing authentic
7"/12" Vinyl and CD single sleeves, and extracts dominant/accent colors.
"""

import os
import re
import hashlib
import logging
from typing import Optional, Tuple, Dict
import requests
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "artwork")
HEADERS = {"User-Agent": "GenX71AutoVJ/2.0 (https://github.com/sdewis/genx71_autovj)"}


def _slugify(text: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return clean[:60]


def fetch_itunes_artwork(artist: str, title: str, timeout: int = 10) -> Optional[str]:
    """Query iTunes Search API for high-res single/track artwork."""
    try:
        query = f"{artist} {title}"
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"term": query, "media": "music", "entity": "song", "limit": 8},
            headers=HEADERS,
            timeout=timeout
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None

        artist_lower = artist.lower()
        title_lower = title.lower()

        # Score candidates: prefer exact artist+title match and "Single" releases
        best_url = None
        best_score = -1

        for item in results:
            score = 0
            r_artist = item.get("artistName", "").lower()
            r_title = item.get("trackName", "").lower()
            r_collection = item.get("collectionName", "").lower()

            if artist_lower in r_artist or r_artist in artist_lower:
                score += 3
            if title_lower in r_title or r_title in title_lower:
                score += 3
            if "single" in r_collection or "ep" in r_collection:
                score += 2
            if "greatest hits" in r_collection or "best of" in r_collection or "compilation" in r_collection:
                score -= 1

            art_url = item.get("artworkUrl100")
            if art_url and score > best_score:
                best_score = score
                # Request high-res 1200x1200 version
                best_url = art_url.replace("100x100bb", "1200x1200bb")

        return best_url
    except Exception as e:
        logger.warning(f"iTunes artwork lookup failed for {artist} - {title}: {e}")
        return None


def fetch_musicbrainz_artwork(artist: str, title: str, timeout: int = 10) -> Optional[str]:
    """Query MusicBrainz for a single release and fetch Cover Art Archive front cover."""
    try:
        query = f'recording:"{title}" AND artist:"{artist}"'
        resp = requests.get(
            "https://musicbrainz.org/ws/2/recording",
            params={"query": query, "fmt": "json", "limit": 5},
            headers=HEADERS,
            timeout=timeout
        )
        resp.raise_for_status()
        recordings = resp.json().get("recordings", [])
        for rec in recordings:
            for release in rec.get("releases", []):
                mbid = release.get("id")
                if not mbid:
                    continue
                caa_url = f"https://coverartarchive.org/release/{mbid}/front-500"
                head = requests.head(caa_url, headers=HEADERS, allow_redirects=True, timeout=timeout)
                if head.status_code == 200:
                    return head.url
        return None
    except Exception as e:
        logger.warning(f"MusicBrainz artwork lookup failed for {artist} - {title}: {e}")
        return None


def _create_fallback_artwork(artist: str, title: str, out_path: str) -> str:
    """Create a sleek retro graphic sleeve if no external artwork is available."""
    size = 1000
    img = Image.new("RGB", (size, size), (24, 24, 34))
    draw = ImageDraw.Draw(img)

    # Geometric neon/retro rings
    h = int(hashlib.md5(f"{artist}{title}".encode()).hexdigest()[:6], 16)
    c1 = ((h >> 16) & 255, (h >> 8) & 255, h & 255)
    c1 = (max(80, c1[0]), max(60, c1[1]), max(100, c1[2]))

    draw.rectangle([40, 40, size - 40, size - 40], outline=c1, width=8)
    draw.ellipse([180, 180, size - 180, size - 180], outline=(240, 240, 255), width=4)
    draw.ellipse([280, 280, size - 280, size - 280], fill=c1)

    try:
        font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
        font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
    except Exception:
        font_lg = ImageFont.load_default()
        font_sm = ImageFont.load_default()

    draw.text((80, 80), artist.upper()[:24], fill=(255, 255, 255), font=font_lg)
    draw.text((80, size - 140), title[:30], fill=(210, 210, 230), font=font_sm)

    img.save(out_path, "JPEG", quality=95)
    return out_path


def get_single_artwork(artist: str, title: str, cache_dir: Optional[str] = None) -> str:
    """
    Download and cache high-resolution single artwork for the track.
    Returns local file path to the square JPEG image.
    """
    target_dir = cache_dir or CACHE_DIR
    os.makedirs(target_dir, exist_ok=True)
    slug = _slugify(f"{artist}_{title}")
    out_path = os.path.join(target_dir, f"{slug}.jpg")

    if os.path.exists(out_path) and os.path.getsize(out_path) > 5000:
        return out_path

    url = fetch_itunes_artwork(artist, title)
    if not url:
        url = fetch_musicbrainz_artwork(artist, title)

    if url:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            tmp_path = out_path + ".tmp"
            with open(tmp_path, "wb") as f:
                f.write(resp.content)
            # Normalize to square RGB JPEG
            with Image.open(tmp_path) as im:
                im = im.convert("RGB")
                w, h = im.size
                min_dim = min(w, h)
                left = (w - min_dim) // 2
                top = (h - min_dim) // 2
                im = im.crop((left, top, left + min_dim, top + min_dim))
                im = im.resize((1000, 1000), Image.Resampling.LANCZOS)
                im.save(out_path, "JPEG", quality=95)
            os.remove(tmp_path)
            return out_path
        except Exception as e:
            logger.warning(f"Failed downloading artwork from {url}: {e}")

    return _create_fallback_artwork(artist, title, out_path)


def extract_palette(image_path: str) -> Dict[str, Tuple[int, int, int]]:
    """Extract dominant dark background and vibrant accent RGB colors from artwork."""
    try:
        with Image.open(image_path) as im:
            im = im.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
            quantized = im.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
            palette = quantized.getpalette()
            counts = sorted(quantized.getcolors(), reverse=True)

            colors = []
            for count, idx in counts:
                r, g, b = palette[idx * 3: idx * 3 + 3]
                colors.append((r, g, b))

            # Pick dominant vibrant color (avoid pure black/white if possible)
            vibrant = colors[0]
            for c in colors:
                lum = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
                sat = max(c) - min(c)
                if 35 < lum < 220 and sat > 35:
                    vibrant = c
                    break

            bg_dark = (
                max(10, int(vibrant[0] * 0.22)),
                max(10, int(vibrant[1] * 0.22)),
                max(15, int(vibrant[2] * 0.25))
            )
            accent = (
                min(255, max(80, int(vibrant[0] * 1.2))),
                min(255, max(80, int(vibrant[1] * 1.2))),
                min(255, max(80, int(vibrant[2] * 1.2)))
            )
            return {"primary": vibrant, "bg_dark": bg_dark, "accent": accent}
    except Exception:
        return {
            "primary": (0, 210, 255),
            "bg_dark": (14, 16, 28),
            "accent": (255, 60, 140)
        }
