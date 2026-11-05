"""Episode storage (no web code)."""

import sqlite3
from datetime import datetime, timezone

COLUMNS = ("podcast_id", "video_id", "title", "description", "author", "audio_file", "thumb_file",
           "square_file", "chapters_file", "info_file", "size_bytes", "duration_s", "published_at", "state")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def insert(conn: sqlite3.Connection, **fields) -> None:
    unknown = set(fields) - set(COLUMNS)
    if unknown:
        raise ValueError(f"unknown episode fields: {sorted(unknown)}")
    now = _now()
    cols = [*fields, "created_at", "updated_at"]
    conn.execute(f"INSERT INTO episodes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 [*fields.values(), now, now])


def list_available(conn: sqlite3.Connection, podcast_id: str) -> list[sqlite3.Row]:
    """Episodes shown in the feed, newest first."""
    return conn.execute(
        "SELECT * FROM episodes WHERE podcast_id = ? AND state = 'available' "
        "ORDER BY COALESCE(published_at, substr(created_at, 1, 10)) DESC, title, video_id",
        (podcast_id,)).fetchall()


def count_all(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]


def count_for(conn: sqlite3.Connection, podcast_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM episodes WHERE podcast_id = ?", (podcast_id,)).fetchone()[0]


def stats_by_podcast(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT podcast_id, COUNT(*) AS total, SUM(state = 'available') AS available, "
        "COALESCE(SUM(CASE WHEN state = 'available' THEN size_bytes END), 0) AS size "
        "FROM episodes GROUP BY podcast_id").fetchall()
    return {r["podcast_id"]: r for r in rows}


def recent(conn: sqlite3.Connection, podcast_id: str, limit: int = 10) -> list[sqlite3.Row]:
    """The newest episodes with files (any state), for the podcast page."""
    return conn.execute(
        "SELECT * FROM episodes WHERE podcast_id = ? AND audio_file IS NOT NULL "
        "ORDER BY COALESCE(published_at, substr(created_at, 1, 10)) DESC, created_at DESC LIMIT ?", (podcast_id, limit)).fetchall()
