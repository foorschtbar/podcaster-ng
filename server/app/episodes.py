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


# --- management ----------------------------------------------------------------------------------

STATES = ("available", "hidden", "deleted")
FILE_COLUMNS = ("audio_file", "thumb_file", "square_file", "chapters_file", "info_file")
_ORDER = "COALESCE(e.published_at, substr(e.created_at, 1, 10)) DESC, e.created_at DESC, e.video_id"


def get(conn: sqlite3.Connection, podcast_id: str, video_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM episodes WHERE podcast_id = ? AND video_id = ?", (podcast_id, video_id)).fetchone()


def _where(podcast_id: str | None, state: str | None) -> tuple[str, list]:
    return ("WHERE (? IS NULL OR e.podcast_id = ?) AND (? IS NULL OR e.state = ?)",
            [podcast_id, podcast_id, state, state])


def list_filtered(conn: sqlite3.Connection, podcast_id: str | None, state: str | None, limit: int, offset: int) -> list[sqlite3.Row]:
    where, args = _where(podcast_id, state)
    return conn.execute(f"SELECT e.*, p.name AS podcast_name FROM episodes e JOIN podcasts p ON p.id = e.podcast_id "
                        f"{where} ORDER BY {_ORDER} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()


def count_filtered(conn: sqlite3.Connection, podcast_id: str | None, state: str | None) -> int:
    where, args = _where(podcast_id, state)
    return conn.execute(f"SELECT COUNT(*) FROM episodes e {where}", args).fetchone()[0]


def totals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Per podcast: episodes by state and the disk space of the ones that still have files."""
    return conn.execute(
        "SELECT p.id, p.name, COUNT(e.video_id) AS total, COALESCE(SUM(e.state = 'available'), 0) AS available, "
        "COALESCE(SUM(e.state = 'hidden'), 0) AS hidden, COALESCE(SUM(e.state = 'deleted'), 0) AS deleted, "
        "COALESCE(SUM(CASE WHEN e.state != 'deleted' THEN e.size_bytes END), 0) AS size "
        "FROM podcasts p LEFT JOIN episodes e ON e.podcast_id = p.id GROUP BY p.id ORDER BY p.name COLLATE NOCASE").fetchall()


def set_hidden(conn: sqlite3.Connection, podcast_id: str, video_id: str, hidden: bool) -> bool:
    """available <-> hidden. Returns False if the episode is missing or deleted (nothing to hide)."""
    before, after = ("available", "hidden") if hidden else ("hidden", "available")
    cur = conn.execute("UPDATE episodes SET state = ?, updated_at = ? WHERE podcast_id = ? AND video_id = ? AND state = ?",
                       (after, _now(), podcast_id, video_id, before))
    return cur.rowcount == 1
