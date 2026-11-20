"""
Database management and deduplication tracking for GenX71 AutoVJ.
Stores tracks, post history, blacklist rules, and runtime settings in SQLite.
"""

import os
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "autovj.db")


def get_db(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    conn = get_db(db_path)
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT NOT NULL,
            title TEXT NOT NULL,
            year INTEGER NOT NULL,
            chart_peak INTEGER,
            chart_region TEXT DEFAULT 'UK',
            is_wildcard INTEGER DEFAULT 0,
            genre TEXT DEFAULT 'Pop/Rock',
            facts_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(artist, title)
        );

        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id INTEGER,
            artist TEXT NOT NULL,
            title TEXT NOT NULL,
            year INTEGER NOT NULL,
            chart_info TEXT,
            template_used TEXT DEFAULT 'vinyl',
            video_path TEXT,
            audio_url TEXT,
            artwork_path TEXT,
            status TEXT NOT NULL DEFAULT 'rendered',
            scheduled_for TIMESTAMP,
            posted_at TIMESTAMP,
            tiktok_id TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(track_id) REFERENCES tracks(id)
        );

        CREATE TABLE IF NOT EXISTS blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT,
            title TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tracks_artist_title ON tracks(artist, title);
        CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
        CREATE INDEX IF NOT EXISTS idx_posts_artist_title ON posts(artist, title);
        """)

        # Safe migration for existing databases missing facts_json
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        if "facts_json" not in cols:
            conn.execute("ALTER TABLE tracks ADD COLUMN facts_json TEXT")

        # Default settings if not already set
        defaults = {
            "schedule_enabled": "true",
            "daily_post_times": "12:30,19:15",
            "jitter_minutes": "30",
            "wildcard_chance": "25",
            "default_template": "vinyl",
            "era_appropriate_template": "false",
            "auto_publish": "false",  # Drafts first or direct publish
            "watermark_opacity": "0.40",
            "cookies_json": ""
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


# --- Track Management ---

def add_track(artist: str, title: str, year: int, chart_peak: Optional[int] = None,
              chart_region: str = "UK", is_wildcard: int = 0, genre: str = "Pop/Rock",
              facts_json: Optional[str] = None, db_path: Optional[str] = None) -> Optional[int]:
    conn = get_db(db_path)
    with conn:
        try:
            cur = conn.execute("""
                INSERT INTO tracks (artist, title, year, chart_peak, chart_region, is_wildcard, genre, facts_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (artist.strip(), title.strip(), year, chart_peak, chart_region, is_wildcard, genre, facts_json))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # Already exists
            row = conn.execute("SELECT id FROM tracks WHERE LOWER(artist)=LOWER(?) AND LOWER(title)=LOWER(?)",
                               (artist.strip(), title.strip())).fetchone()
            return row["id"] if row else None


def add_tracks_batch(tracks_list: List[Dict[str, Any]], db_path: Optional[str] = None) -> int:
    conn = get_db(db_path)
    inserted = 0
    with conn:
        for t in tracks_list:
            try:
                conn.execute("""
                    INSERT INTO tracks (artist, title, year, chart_peak, chart_region, is_wildcard, genre, facts_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    t["artist"].strip(),
                    t["title"].strip(),
                    int(t["year"]),
                    t.get("chart_peak"),
                    t.get("chart_region", "UK"),
                    int(t.get("is_wildcard", 0)),
                    t.get("genre", "Pop/Rock"),
                    t.get("facts_json")
                ))
                inserted += 1
            except sqlite3.IntegrityError:
                continue
    return inserted


def update_track_facts(track_id: int, facts_json: str, db_path: Optional[str] = None) -> bool:
    conn = get_db(db_path)
    with conn:
        cur = conn.execute("UPDATE tracks SET facts_json = ? WHERE id = ?", (facts_json, track_id))
        return cur.rowcount > 0


def get_all_tracks(limit: int = 500, offset: int = 0, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    cur = conn.execute("SELECT * FROM tracks ORDER BY year ASC, chart_peak ASC LIMIT ? OFFSET ?", (limit, offset))
    return [dict(r) for r in cur.fetchall()]


def count_tracks(db_path: Optional[str] = None) -> int:
    conn = get_db(db_path)
    row = conn.execute("SELECT COUNT(*) as count FROM tracks").fetchone()
    return row["count"] if row else 0


def get_track_by_id(track_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return dict(row) if row else None


# --- Deduplication & Availability Filtering ---

def is_track_posted(artist: str, title: str, db_path: Optional[str] = None) -> bool:
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT id FROM posts 
        WHERE LOWER(artist) = LOWER(?) AND LOWER(title) = LOWER(?)
        AND status IN ('posted', 'rendered', 'scheduled', 'draft')
    """, (artist.strip(), title.strip())).fetchone()
    return row is not None


def is_blacklisted(artist: str, title: Optional[str] = None, db_path: Optional[str] = None) -> bool:
    conn = get_db(db_path)
    if title:
        row = conn.execute("""
            SELECT id FROM blacklist 
            WHERE (LOWER(artist) = LOWER(?) AND (title IS NULL OR title = '' OR LOWER(title) = LOWER(?)))
        """, (artist.strip(), title.strip())).fetchone()
    else:
        row = conn.execute("SELECT id FROM blacklist WHERE LOWER(artist) = LOWER(?)", (artist.strip(),)).fetchone()
    return row is not None


def get_available_tracks(is_wildcard: Optional[bool] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    query = """
        SELECT t.* FROM tracks t
        WHERE t.id NOT IN (
            SELECT DISTINCT track_id FROM posts WHERE track_id IS NOT NULL AND status IN ('posted', 'rendered', 'scheduled', 'draft')
        )
        AND NOT EXISTS (
            SELECT 1 FROM blacklist b
            WHERE LOWER(b.artist) = LOWER(t.artist)
            AND (b.title IS NULL OR b.title = '' OR LOWER(b.title) = LOWER(t.title))
        )
    """
    params = []
    if is_wildcard is not None:
        query += " AND t.is_wildcard = ?"
        params.append(1 if is_wildcard else 0)

    cur = conn.execute(query, params)
    return [dict(r) for r in cur.fetchall()]


# --- Blacklist Management ---

def add_to_blacklist(artist: str, title: Optional[str] = None, reason: str = "", db_path: Optional[str] = None) -> int:
    conn = get_db(db_path)
    with conn:
        cur = conn.execute("""
            INSERT INTO blacklist (artist, title, reason) VALUES (?, ?, ?)
        """, (artist.strip(), title.strip() if title else None, reason.strip()))
        return cur.lastrowid


def remove_from_blacklist(blacklist_id: int, db_path: Optional[str] = None) -> bool:
    conn = get_db(db_path)
    with conn:
        cur = conn.execute("DELETE FROM blacklist WHERE id = ?", (blacklist_id,))
        return cur.rowcount > 0


def get_blacklist(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    cur = conn.execute("SELECT * FROM blacklist ORDER BY created_at DESC")
    return [dict(r) for r in cur.fetchall()]


# --- Post History Management ---

def record_post(track_id: Optional[int], artist: str, title: str, year: int,
                chart_info: str = "", template_used: str = "vinyl",
                video_path: Optional[str] = None, audio_url: Optional[str] = None,
                artwork_path: Optional[str] = None, status: str = "rendered",
                scheduled_for: Optional[datetime] = None, db_path: Optional[str] = None) -> int:
    conn = get_db(db_path)
    with conn:
        cur = conn.execute("""
            INSERT INTO posts (
                track_id, artist, title, year, chart_info, template_used,
                video_path, audio_url, artwork_path, status, scheduled_for
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            track_id, artist.strip(), title.strip(), year, chart_info, template_used,
            video_path, audio_url, artwork_path, status,
            scheduled_for.isoformat() if scheduled_for else None
        ))
        return cur.lastrowid


def update_post_status(post_id: int, status: str, tiktok_id: Optional[str] = None,
                       error_message: Optional[str] = None, db_path: Optional[str] = None) -> bool:
    conn = get_db(db_path)
    with conn:
        posted_at = datetime.utcnow().isoformat() if status == "posted" else None
        cur = conn.execute("""
            UPDATE posts 
            SET status = ?, tiktok_id = COALESCE(?, tiktok_id),
                error_message = ?, posted_at = COALESCE(?, posted_at)
            WHERE id = ?
        """, (status, tiktok_id, error_message, posted_at, post_id))
        return cur.rowcount > 0


def get_recent_posts(limit: int = 50, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    cur = conn.execute("""
        SELECT * FROM posts 
        ORDER BY created_at DESC 
        LIMIT ?
    """, (limit,))
    return [dict(r) for r in cur.fetchall()]


def get_post_by_id(post_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return dict(row) if row else None


# --- Settings Store ---

def get_setting(key: str, default: Optional[str] = None, db_path: Optional[str] = None) -> Optional[str]:
    conn = get_db(db_path)
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, db_path: Optional[str] = None) -> None:
    conn = get_db(db_path)
    with conn:
        conn.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))


def get_all_settings(db_path: Optional[str] = None) -> Dict[str, str]:
    conn = get_db(db_path)
    cur = conn.execute("SELECT key, value FROM settings")
    return {row["key"]: row["value"] for row in cur.fetchall()}
