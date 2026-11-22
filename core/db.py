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
            batch_num INTEGER NOT NULL DEFAULT 1,
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

        CREATE TABLE IF NOT EXISTS post_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER UNIQUE NOT NULL,
            views INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            saves INTEGER DEFAULT 0,
            avg_watch_time_sec REAL DEFAULT 0.0,
            completion_rate REAL DEFAULT 0.0,
            engagement_rate REAL DEFAULT 0.0,
            performance_score REAL DEFAULT 0.0,
            tiktok_url TEXT,
            source TEXT DEFAULT 'manual',
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS analytics_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            period_type TEXT NOT NULL DEFAULT 'daily',
            model_used TEXT NOT NULL,
            summary_markdown TEXT NOT NULL,
            top_posts_json TEXT,
            improvements_json TEXT,
            feedback_weights_json TEXT,
            period_comparison_json TEXT,
            lessons_learned_json TEXT,
            outcome_evaluation_json TEXT,
            posts_analyzed INTEGER DEFAULT 0,
            elapsed_sec REAL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'completed',
            error_message TEXT,
            applied_to_feedback INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS analytics_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            snapshot_date TEXT NOT NULL,
            views INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            saves INTEGER DEFAULT 0,
            engagement_rate REAL DEFAULT 0.0,
            completion_rate REAL DEFAULT 0.0,
            performance_score REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(post_id, snapshot_date),
            FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tracks_artist_title ON tracks(artist, title);
        CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
        CREATE INDEX IF NOT EXISTS idx_posts_artist_title ON posts(artist, title);
        CREATE INDEX IF NOT EXISTS idx_post_analytics_post_id ON post_analytics(post_id);
        CREATE INDEX IF NOT EXISTS idx_analytics_reports_date ON analytics_reports(report_date DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_analytics_snapshots_date ON analytics_snapshots(snapshot_date DESC, post_id);
        """)

        # Safe migrations for existing databases on tracks
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        if "facts_json" not in cols:
            conn.execute("ALTER TABLE tracks ADD COLUMN facts_json TEXT")
        if "batch_num" not in cols:
            conn.execute("ALTER TABLE tracks ADD COLUMN batch_num INTEGER NOT NULL DEFAULT 1")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_batch ON tracks(batch_num)")

        # Safe migrations for existing databases on posts
        post_cols = {row["name"] for row in conn.execute("PRAGMA table_info(posts)").fetchall()}
        post_migrations = {
            "queue_position": "INTEGER",
            "hook_text": "TEXT",
            "hook_template_id": "TEXT",
            "voice_region": "TEXT",
            "tts_audio_path": "TEXT",
            "tts_enabled": "INTEGER DEFAULT 0",
            "auto_queue": "INTEGER DEFAULT 0",
            "publish_after_render": "INTEGER DEFAULT 0",
        }
        for col_name, col_type in post_migrations.items():
            if col_name not in post_cols:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {col_name} {col_type}")

        # Safe migrations for existing databases on analytics_reports
        rep_cols = {row["name"] for row in conn.execute("PRAGMA table_info(analytics_reports)").fetchall()}
        rep_migrations = {
            "period_type": "TEXT NOT NULL DEFAULT 'daily'",
            "period_comparison_json": "TEXT",
            "lessons_learned_json": "TEXT",
            "outcome_evaluation_json": "TEXT",
        }
        for col_name, col_type in rep_migrations.items():
            if col_name not in rep_cols:
                conn.execute(f"ALTER TABLE analytics_reports ADD COLUMN {col_name} {col_type}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_analytics_reports_period ON analytics_reports(period_type, id DESC)")

        # Default settings if not already set
        defaults = {
            "schedule_enabled": "true",
            "daily_post_times": "12:30,19:15",
            "jitter_minutes": "30",
            "wildcard_chance": "25",
            "default_template": "auto",
            "era_appropriate_template": "false",
            "auto_queue_on_generate": "false",
            "auto_publish": "false",  # Drafts first or direct publish
            "watermark_opacity": "0.65",
            "cookies_json": "",
            "tts_enabled": "false",
            "tts_speed": "1.05",
            "tts_worker_url": "",
            "analytics_report_enabled": "true",
            "analytics_report_time": "02:00",
            "analytics_weekly_enabled": "true",
            "analytics_monthly_enabled": "true",
            "analytics_auto_sync": "true",
            "analytics_feedback_enabled": "true",
            "analytics_feedback_strength": "0.5",
            "llm_model": "qwen3.5:2b",
            "llm_worker_url": "",
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))



# --- Track Management ---

def get_max_track_batch(db_path: Optional[str] = None) -> int:
    conn = get_db(db_path)
    row = conn.execute("SELECT MAX(batch_num) AS max_batch FROM tracks").fetchone()
    return int(row["max_batch"]) if row and row["max_batch"] is not None else 0


def get_track_batches(db_path: Optional[str] = None) -> List[int]:
    """Return distinct batch numbers in descending order (newest batch = Page 1)."""
    conn = get_db(db_path)
    rows = conn.execute("SELECT DISTINCT batch_num FROM tracks ORDER BY batch_num DESC").fetchall()
    return [int(r["batch_num"]) for r in rows]


def get_tracks_by_batch(batch_num: int, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    cur = conn.execute(
        "SELECT * FROM tracks WHERE batch_num = ? ORDER BY year ASC, chart_peak ASC, id ASC",
        (int(batch_num),),
    )
    return [dict(r) for r in cur.fetchall()]


def get_existing_track_keys(db_path: Optional[str] = None) -> set:
    """Return a set of lowercase (artist, title) tuples already in the tracks table."""
    conn = get_db(db_path)
    rows = conn.execute("SELECT LOWER(artist) AS a, LOWER(title) AS t FROM tracks").fetchall()
    return {(r["a"], r["t"]) for r in rows}


def add_track(artist: str, title: str, year: int, chart_peak: Optional[int] = None,
              chart_region: str = "UK", is_wildcard: int = 0, genre: str = "Pop/Rock",
              facts_json: Optional[str] = None, batch_num: Optional[int] = None,
              db_path: Optional[str] = None) -> Optional[int]:
    conn = get_db(db_path)
    if batch_num is None:
        row = conn.execute("SELECT MAX(batch_num) AS max_batch FROM tracks").fetchone()
        batch_num = int(row["max_batch"]) if row and row["max_batch"] is not None else 1
    with conn:
        try:
            cur = conn.execute("""
                INSERT INTO tracks (artist, title, year, chart_peak, chart_region, is_wildcard, genre, facts_json, batch_num)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (artist.strip(), title.strip(), year, chart_peak, chart_region, is_wildcard, genre, facts_json, int(batch_num)))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # Already exists
            row = conn.execute("SELECT id FROM tracks WHERE LOWER(artist)=LOWER(?) AND LOWER(title)=LOWER(?)",
                               (artist.strip(), title.strip())).fetchone()
            return row["id"] if row else None


def add_tracks_batch(tracks_list: List[Dict[str, Any]], batch_num: Optional[int] = None,
                     db_path: Optional[str] = None) -> int:
    conn = get_db(db_path)
    inserted = 0
    default_batch = int(batch_num) if batch_num is not None else 1
    with conn:
        for t in tracks_list:
            try:
                conn.execute("""
                    INSERT INTO tracks (artist, title, year, chart_peak, chart_region, is_wildcard, genre, facts_json, batch_num)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    t["artist"].strip(),
                    t["title"].strip(),
                    int(t["year"]),
                    t.get("chart_peak"),
                    t.get("chart_region", "UK"),
                    int(t.get("is_wildcard", 0)),
                    t.get("genre", "Pop/Rock"),
                    t.get("facts_json"),
                    int(t.get("batch_num", default_batch)),
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
    cur = conn.execute(
        "SELECT * FROM tracks ORDER BY batch_num DESC, year ASC, chart_peak ASC LIMIT ? OFFSET ?",
        (limit, offset),
    )
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
        AND status IN ('posted', 'rendered', 'scheduled', 'draft', 'tts_pending', 'rendering')
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
            SELECT DISTINCT track_id FROM posts WHERE track_id IS NOT NULL AND status IN ('posted', 'rendered', 'scheduled', 'draft', 'tts_pending', 'rendering')
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
                scheduled_for: Optional[datetime] = None,
                hook_text: Optional[str] = None, hook_template_id: Optional[str] = None,
                voice_region: Optional[str] = None, tts_audio_path: Optional[str] = None,
                tts_enabled: int = 0, auto_queue: int = 0, publish_after_render: int = 0,
                db_path: Optional[str] = None) -> int:
    conn = get_db(db_path)
    with conn:
        cur = conn.execute("""
            INSERT INTO posts (
                track_id, artist, title, year, chart_info, template_used,
                video_path, audio_url, artwork_path, status, scheduled_for,
                hook_text, hook_template_id, voice_region, tts_audio_path,
                tts_enabled, auto_queue, publish_after_render
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            track_id, artist.strip(), title.strip(), year, chart_info, template_used,
            video_path, audio_url, artwork_path, status,
            scheduled_for.isoformat() if scheduled_for else None,
            hook_text, hook_template_id, voice_region, tts_audio_path,
            int(bool(tts_enabled)), int(bool(auto_queue)), int(bool(publish_after_render)),
        ))
        return cur.lastrowid



def _compact_queue_positions(conn: sqlite3.Connection) -> None:
    rows = conn.execute("""
        SELECT id FROM posts
        WHERE status = 'scheduled'
        ORDER BY COALESCE(queue_position, 999999) ASC, id ASC
    """).fetchall()
    for idx, row in enumerate(rows, start=1):
        conn.execute("UPDATE posts SET queue_position = ? WHERE id = ?", (idx, row["id"]))


def update_post_status(post_id: int, status: str, tiktok_id: Optional[str] = None,
                       error_message: Optional[str] = None, db_path: Optional[str] = None) -> bool:
    conn = get_db(db_path)
    with conn:
        posted_at = datetime.utcnow().isoformat() if status == "posted" else None
        clear_queue_pos = status != "scheduled"
        if clear_queue_pos:
            cur = conn.execute("""
                UPDATE posts 
                SET status = ?, tiktok_id = COALESCE(?, tiktok_id),
                    error_message = ?, posted_at = COALESCE(?, posted_at),
                    queue_position = NULL
                WHERE id = ?
            """, (status, tiktok_id, error_message, posted_at, post_id))
            _compact_queue_positions(conn)
        else:
            cur = conn.execute("""
                UPDATE posts 
                SET status = ?, tiktok_id = COALESCE(?, tiktok_id),
                    error_message = ?, posted_at = COALESCE(?, posted_at)
                WHERE id = ?
            """, (status, tiktok_id, error_message, posted_at, post_id))
        return cur.rowcount > 0


def queue_post(post_id: int, front: bool = False, db_path: Optional[str] = None) -> Optional[int]:
    """Add a post to the scheduled post queue (or move to front if front=True)."""
    conn = get_db(db_path)
    with conn:
        row = conn.execute("SELECT id, status, queue_position FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            return None

        if row["status"] == "scheduled" and not front:
            _compact_queue_positions(conn)
            updated = conn.execute("SELECT queue_position FROM posts WHERE id = ?", (post_id,)).fetchone()
            return updated["queue_position"] if updated else 1

        if front:
            conn.execute("""
                UPDATE posts SET queue_position = COALESCE(queue_position, 0) + 1
                WHERE status = 'scheduled' AND id != ?
            """, (post_id,))
            conn.execute("""
                UPDATE posts SET status = 'scheduled', queue_position = 1, error_message = NULL
                WHERE id = ?
            """, (post_id,))
        else:
            max_row = conn.execute("""
                SELECT COALESCE(MAX(queue_position), 0) AS max_pos FROM posts WHERE status = 'scheduled'
            """).fetchone()
            next_pos = (max_row["max_pos"] if max_row else 0) + 1
            conn.execute("""
                UPDATE posts SET status = 'scheduled', queue_position = ?, error_message = NULL
                WHERE id = ?
            """, (next_pos, post_id))

        _compact_queue_positions(conn)
        updated = conn.execute("SELECT queue_position FROM posts WHERE id = ?", (post_id,)).fetchone()
        return updated["queue_position"] if updated else None


def get_queued_posts(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    cur = conn.execute("""
        SELECT * FROM posts
        WHERE status = 'scheduled'
        ORDER BY COALESCE(queue_position, 999999) ASC, id ASC
    """)
    return [dict(r) for r in cur.fetchall()]


def pop_next_queued_post(db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the first post in the scheduled queue (without removing it yet)."""
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT * FROM posts
        WHERE status = 'scheduled'
        ORDER BY COALESCE(queue_position, 999999) ASC, id ASC
        LIMIT 1
    """).fetchone()
    return dict(row) if row else None


def remove_from_queue(post_id: int, db_path: Optional[str] = None) -> bool:
    """Remove a post from the queue, reverting its status to 'rendered'."""
    conn = get_db(db_path)
    with conn:
        cur = conn.execute("""
            UPDATE posts SET status = 'rendered', queue_position = NULL
            WHERE id = ? AND status = 'scheduled'
        """, (post_id,))
        if cur.rowcount > 0:
            _compact_queue_positions(conn)
            return True
        return False


def reorder_queue(post_ids: List[int], db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Reorder queued posts according to post_ids; any other queued posts remain at the end."""
    conn = get_db(db_path)
    with conn:
        existing_rows = conn.execute("""
            SELECT id FROM posts
            WHERE status = 'scheduled'
            ORDER BY COALESCE(queue_position, 999999) ASC, id ASC
        """).fetchall()
        existing_ids = [r["id"] for r in existing_rows]
        existing_set = set(existing_ids)

        ordered: List[int] = []
        seen = set()
        for pid in post_ids:
            if pid in existing_set and pid not in seen:
                ordered.append(pid)
                seen.add(pid)
        for pid in existing_ids:
            if pid not in seen:
                ordered.append(pid)

        for idx, pid in enumerate(ordered, start=1):
            conn.execute("UPDATE posts SET queue_position = ? WHERE id = ?", (idx, pid))

    return get_queued_posts(db_path=db_path)


def clear_queue(db_path: Optional[str] = None) -> int:
    """Remove all posts from the scheduled queue, reverting them to 'rendered'."""
    conn = get_db(db_path)
    with conn:
        cur = conn.execute("""
            UPDATE posts SET status = 'rendered', queue_position = NULL
            WHERE status = 'scheduled'
        """)
        return cur.rowcount


def get_recent_posts(limit: int = 50, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    cur = conn.execute("""
        SELECT * FROM posts 
        ORDER BY created_at DESC, id DESC 
        LIMIT ?
    """, (limit,))
    return [dict(r) for r in cur.fetchall()]


def get_recent_templates(limit: int = 5, db_path: Optional[str] = None) -> List[str]:
    """Return the most recently used template/style names (newest first)."""
    conn = get_db(db_path)
    cur = conn.execute("""
        SELECT template_used FROM posts
        WHERE template_used IS NOT NULL AND template_used != ''
        ORDER BY created_at DESC, id DESC
        LIMIT ?
    """, (limit,))
    return [row["template_used"] for row in cur.fetchall()]


def get_post_by_id(post_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return dict(row) if row else None


def delete_post(post_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Permanently remove a post/render entry from the posts table, compacting
    scheduled queue positions if needed. Deleting a post automatically unlocks
    its track in deduplication checks so it can be re-rendered.
    Returns the deleted post dict, or None if not found.
    """
    conn = get_db(db_path)
    with conn:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            return None
        deleted = dict(row)
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        if deleted.get("status") == "scheduled":
            _compact_queue_positions(conn)
        return deleted


_ALLOWED_POST_UPDATE_FIELDS = {
    "template_used", "video_path", "audio_url", "artwork_path", "status",
    "error_message", "hook_text", "hook_template_id", "voice_region",
    "tts_audio_path", "tts_enabled", "auto_queue", "publish_after_render",
}


def update_post_fields(post_id: int, db_path: Optional[str] = None, **fields: Any) -> bool:
    clean = {k: v for k, v in fields.items() if k in _ALLOWED_POST_UPDATE_FIELDS}
    if not clean:
        return False
    conn = get_db(db_path)
    cols_sql = ", ".join(f"{k} = ?" for k in clean.keys())
    vals = list(clean.values()) + [post_id]
    with conn:
        cur = conn.execute(f"UPDATE posts SET {cols_sql} WHERE id = ?", vals)
        return cur.rowcount > 0


def get_recent_hook_templates(limit: int = 12, db_path: Optional[str] = None) -> List[str]:
    """Return the most recently used DJ hook template IDs (newest first)."""
    conn = get_db(db_path)
    cur = conn.execute("""
        SELECT hook_template_id FROM posts
        WHERE hook_template_id IS NOT NULL AND hook_template_id != '' AND hook_template_id != 'custom'
        ORDER BY created_at DESC, id DESC
        LIMIT ?
    """, (limit,))
    return [row["hook_template_id"] for row in cur.fetchall()]


def get_pending_tts_posts(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return posts waiting for async TTS generation / rendering (oldest first)."""
    conn = get_db(db_path)
    cur = conn.execute("""
        SELECT * FROM posts
        WHERE status IN ('tts_pending', 'rendering')
        ORDER BY id ASC
    """)
    return [dict(r) for r in cur.fetchall()]



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


# --- Post Analytics & Daily AI Reports ---

def _compute_analytics_scores(
    views: int,
    likes: int,
    comments: int,
    shares: int,
    saves: int,
    completion_rate: float = 0.0,
) -> tuple:
    """Compute weighted engagement rate (%) and composite performance score."""
    import math

    v = max(0, int(views or 0))
    l = max(0, int(likes or 0))
    c = max(0, int(comments or 0))
    sh = max(0, int(shares or 0))
    sv = max(0, int(saves or 0))
    comp = max(0.0, min(100.0, float(completion_rate or 0.0)))

    if v > 0:
        weighted_eng = l + (c * 2.0) + (sh * 3.0) + (sv * 2.0)
        engagement_rate = round((weighted_eng / float(v)) * 100.0, 2)
    else:
        engagement_rate = 0.0

    # Composite score balancing reach (log-scaled views), engagement, and retention
    reach_component = math.log10(v + 1) * 18.0 if v > 0 else 0.0
    eng_component = min(engagement_rate, 40.0) * 1.4
    comp_component = comp * 0.45
    performance_score = round(reach_component + eng_component + comp_component, 2)
    return engagement_rate, performance_score


def upsert_post_analytics(
    post_id: int,
    views: int = 0,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    saves: int = 0,
    avg_watch_time_sec: float = 0.0,
    completion_rate: float = 0.0,
    tiktok_url: Optional[str] = None,
    source: str = "manual",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert or update analytics metrics for an existing post."""
    v = max(0, int(views or 0))
    l = max(0, int(likes or 0))
    c = max(0, int(comments or 0))
    sh = max(0, int(shares or 0))
    sv = max(0, int(saves or 0))
    awt = max(0.0, float(avg_watch_time_sec or 0.0))
    comp = max(0.0, min(100.0, float(completion_rate or 0.0)))
    from datetime import timezone
    eng_rate, perf_score = _compute_analytics_scores(v, l, c, sh, sv, comp)
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = get_db(db_path)
    with conn:
        conn.execute("""
            INSERT INTO post_analytics (
                post_id, views, likes, comments, shares, saves,
                avg_watch_time_sec, completion_rate, engagement_rate,
                performance_score, tiktok_url, source, fetched_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                views = excluded.views,
                likes = excluded.likes,
                comments = excluded.comments,
                shares = excluded.shares,
                saves = excluded.saves,
                avg_watch_time_sec = excluded.avg_watch_time_sec,
                completion_rate = excluded.completion_rate,
                engagement_rate = excluded.engagement_rate,
                performance_score = excluded.performance_score,
                tiktok_url = COALESCE(excluded.tiktok_url, post_analytics.tiktok_url),
                source = excluded.source,
                updated_at = excluded.updated_at
        """, (
            post_id, v, l, c, sh, sv,
            round(awt, 2), round(comp, 2), eng_rate, perf_score,
            tiktok_url, source, now_iso, now_iso,
        ))
        # Also record a daily snapshot so weekly/monthly velocity can be tracked over time
        snap_date = now_iso[:10]
        conn.execute("""
            INSERT INTO analytics_snapshots (
                post_id, snapshot_date, views, likes, comments, shares, saves,
                engagement_rate, completion_rate, performance_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id, snapshot_date) DO UPDATE SET
                views = excluded.views,
                likes = excluded.likes,
                comments = excluded.comments,
                shares = excluded.shares,
                saves = excluded.saves,
                engagement_rate = excluded.engagement_rate,
                completion_rate = excluded.completion_rate,
                performance_score = excluded.performance_score
        """, (post_id, snap_date, v, l, c, sh, sv, eng_rate, round(comp, 2), perf_score))
        row = conn.execute("SELECT * FROM post_analytics WHERE post_id = ?", (post_id,)).fetchone()
        return dict(row) if row else {}


def get_post_analytics_rows(limit: int = 200, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return existing posts joined with their track metadata and analytics metrics."""
    conn = get_db(db_path)
    cur = conn.execute("""
        SELECT
            p.id AS post_id,
            p.track_id,
            p.artist,
            p.title,
            p.year,
            p.chart_info,
            p.template_used,
            p.status,
            p.posted_at,
            p.created_at,
            p.tiktok_id,
            p.tts_enabled,
            p.voice_region,
            p.hook_template_id,
            COALESCE(t.genre, 'Pop/Rock') AS genre,
            t.chart_peak,
            COALESCE(t.chart_region, 'UK') AS chart_region,
            COALESCE(t.is_wildcard, 0) AS is_wildcard,
            CASE WHEN pa.id IS NOT NULL THEN 1 ELSE 0 END AS has_metrics,
            COALESCE(pa.views, 0) AS views,
            COALESCE(pa.likes, 0) AS likes,
            COALESCE(pa.comments, 0) AS comments,
            COALESCE(pa.shares, 0) AS shares,
            COALESCE(pa.saves, 0) AS saves,
            COALESCE(pa.avg_watch_time_sec, 0.0) AS avg_watch_time_sec,
            COALESCE(pa.completion_rate, 0.0) AS completion_rate,
            COALESCE(pa.engagement_rate, 0.0) AS engagement_rate,
            COALESCE(pa.performance_score, 0.0) AS performance_score,
            pa.tiktok_url,
            pa.source AS analytics_source,
            pa.updated_at AS analytics_updated_at
        FROM posts p
        LEFT JOIN tracks t ON t.id = p.track_id
        LEFT JOIN post_analytics pa ON pa.post_id = p.id
        ORDER BY COALESCE(pa.performance_score, 0.0) DESC, p.created_at DESC, p.id DESC
        LIMIT ?
    """, (limit,))
    return [dict(r) for r in cur.fetchall()]


def save_analytics_report(
    report_date: str,
    model_used: str,
    summary_markdown: str,
    top_posts: Optional[List[Dict[str, Any]]] = None,
    improvements: Optional[List[str]] = None,
    feedback_weights: Optional[Dict[str, Any]] = None,
    posts_analyzed: int = 0,
    elapsed_sec: float = 0.0,
    status: str = "completed",
    error_message: Optional[str] = None,
    applied_to_feedback: bool = False,
    period_type: str = "daily",
    period_comparison: Optional[Dict[str, Any]] = None,
    lessons_learned: Optional[List[str]] = None,
    outcome_evaluation: Optional[Dict[str, Any]] = None,
    db_path: Optional[str] = None,
) -> int:
    """Persist a daily, weekly, or monthly AI analytics report."""
    import json

    period = period_type if period_type in ("daily", "weekly", "monthly") else "daily"
    conn = get_db(db_path)
    with conn:
        if applied_to_feedback:
            conn.execute("UPDATE analytics_reports SET applied_to_feedback = 0")
        cur = conn.execute("""
            INSERT INTO analytics_reports (
                report_date, period_type, model_used, summary_markdown,
                top_posts_json, improvements_json, feedback_weights_json,
                period_comparison_json, lessons_learned_json, outcome_evaluation_json,
                posts_analyzed, elapsed_sec, status, error_message, applied_to_feedback
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report_date,
            period,
            model_used,
            summary_markdown,
            json.dumps(top_posts or []),
            json.dumps(improvements or []),
            json.dumps(feedback_weights or {}),
            json.dumps(period_comparison or {}),
            json.dumps(lessons_learned or []),
            json.dumps(outcome_evaluation) if outcome_evaluation else None,
            int(posts_analyzed),
            round(float(elapsed_sec or 0.0), 2),
            status,
            error_message,
            1 if applied_to_feedback else 0,
        ))
        return cur.lastrowid


def _hydrate_report_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    import json

    if not row:
        return None
    d = dict(row)
    d["period_type"] = d.get("period_type") or "daily"
    for json_col, out_key, default in (
        ("top_posts_json", "top_posts", []),
        ("improvements_json", "improvements", []),
        ("feedback_weights_json", "feedback_weights", {}),
        ("period_comparison_json", "period_comparison", {}),
        ("lessons_learned_json", "lessons_learned", []),
        ("outcome_evaluation_json", "outcome_evaluation", None),
    ):
        raw = d.get(json_col)
        try:
            d[out_key] = json.loads(raw) if raw else default
        except Exception:
            d[out_key] = default
    return d


def get_analytics_reports(
    limit: int = 30,
    period_type: Optional[str] = None,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    conn = get_db(db_path)
    if period_type in ("daily", "weekly", "monthly"):
        cur = conn.execute("""
            SELECT * FROM analytics_reports
            WHERE COALESCE(period_type, 'daily') = ?
            ORDER BY id DESC
            LIMIT ?
        """, (period_type, limit))
    else:
        cur = conn.execute("""
            SELECT * FROM analytics_reports
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
    return [_hydrate_report_row(r) for r in cur.fetchall()]


def get_analytics_report_by_id(report_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM analytics_reports WHERE id = ?", (report_id,)).fetchone()
    return _hydrate_report_row(row)


def get_latest_analytics_report(
    period_type: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    conn = get_db(db_path)
    if period_type in ("daily", "weekly", "monthly"):
        row = conn.execute("""
            SELECT * FROM analytics_reports
            WHERE COALESCE(period_type, 'daily') = ?
            ORDER BY id DESC LIMIT 1
        """, (period_type,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM analytics_reports ORDER BY id DESC LIMIT 1").fetchone()
    return _hydrate_report_row(row)


def update_report_outcome_evaluation(
    report_id: int,
    evaluation: Dict[str, Any],
    db_path: Optional[str] = None,
) -> bool:
    """Persist the measured post-hoc evaluation of a past report's suggestions."""
    import json

    conn = get_db(db_path)
    with conn:
        cur = conn.execute(
            "UPDATE analytics_reports SET outcome_evaluation_json = ? WHERE id = ?",
            (json.dumps(evaluation), report_id),
        )
        return cur.rowcount > 0


def get_evaluated_learning_history(limit: int = 8, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return past reports that have measured suggestion outcomes (newest first)."""
    conn = get_db(db_path)
    cur = conn.execute("""
        SELECT * FROM analytics_reports
        WHERE outcome_evaluation_json IS NOT NULL AND outcome_evaluation_json != ''
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    return [_hydrate_report_row(r) for r in cur.fetchall()]


def mark_report_applied(report_id: int, db_path: Optional[str] = None) -> bool:
    """Mark a specific report as the active feedback profile for future track selection."""
    conn = get_db(db_path)
    with conn:
        exists = conn.execute("SELECT id FROM analytics_reports WHERE id = ?", (report_id,)).fetchone()
        if not exists:
            return False
        conn.execute("UPDATE analytics_reports SET applied_to_feedback = 0")
        conn.execute("UPDATE analytics_reports SET applied_to_feedback = 1 WHERE id = ?", (report_id,))
        return True


def get_active_feedback_weights(db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the active feedback weights dict from the applied (or latest) analytics report."""
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT * FROM analytics_reports
        WHERE applied_to_feedback = 1 AND feedback_weights_json IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()
    if not row:
        return None
    hydrated = _hydrate_report_row(row)
    weights = hydrated.get("feedback_weights") if hydrated else None
    return weights if isinstance(weights, dict) and weights else None


