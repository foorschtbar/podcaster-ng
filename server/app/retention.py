"""Removing the files of old episodes.

Only files go: the episode row stays with state ``deleted`` so a scan never downloads the video again, and the
hit statistics keep their history. ``plan`` decides (and is what the UI previews), ``run`` does exactly that.
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import episodes, queue, settings
from .db import connect
from .workers import TIME_FORMAT

log = logging.getLogger(__name__)
LAST_RUN_KEY = "retention_last_run"          # raw settings row (a date), not an editable setting
REASONS = {"keep_last": "beyond the last N", "max_age": "too old", "manual": "deleted by hand", "redownload": "re-download"}


@dataclass
class Candidate:
    video_id: str
    title: str
    reason: str
    size_bytes: int
    day: str


@dataclass
class PodcastPlan:
    podcast_id: str
    name: str
    keep_last: int | None
    max_age_days: int | None
    total: int                                   # episodes that still have files
    candidates: list[Candidate] = field(default_factory=list)
    capped: int = 0                              # selected by the rules but over the per-run limit

    @property
    def bytes(self) -> int:
        return sum(c.size_bytes for c in self.candidates)


def _day(row) -> str:
    return (row["published_at"] or row["created_at"] or "")[:10]


def effective_rules(podcast, defaults: dict) -> tuple[int | None, int | None]:
    """The podcast's own rule, else the global default; None means that rule is off."""
    keep = podcast["keep_last"] if podcast["keep_last"] is not None else defaults.get("keep_last")
    age = podcast["max_age_days"] if podcast["max_age_days"] is not None else defaults.get("max_age_days")
    return keep, age


def select(rows: list, keep_last: int | None, max_age_days: int | None, today: date,
           limit: int | None = None) -> tuple[list[Candidate], int]:
    """Pick the episodes to delete. Returns ``(candidates oldest first, number held back by the limit)``.

    Newest first, ties broken by creation time and video id so the result is stable. The newest episode is never
    selected, and when ``limit`` cuts the list the oldest ones go first.
    """
    ordered = sorted(rows, key=lambda r: (_day(r), r["created_at"], r["video_id"]), reverse=True)
    cutoff = (today - timedelta(days=max_age_days)).isoformat() if max_age_days else None
    picked: list[Candidate] = []
    for index, row in enumerate(ordered):
        if index == 0:
            continue
        reason = None
        if keep_last and index >= keep_last:
            reason = "keep_last"
        elif cutoff and _day(row) and _day(row) < cutoff:
            reason = "max_age"
        if reason:
            picked.append(Candidate(row["video_id"], row["title"], reason, row["size_bytes"], _day(row)))
    picked.reverse()
    if limit is not None and len(picked) > limit:
        return picked[:max(0, limit)], len(picked) - max(0, limit)
    return picked, 0


def plan(conn: sqlite3.Connection, defaults: dict, podcast_id: str | None = None,
         today: date | None = None) -> list[PodcastPlan]:
    """What a run would delete now. The per-run limit is shared by all podcasts, in name order."""
    today = today or datetime.now(timezone.utc).date()
    budget = defaults["retention_max_per_run"]
    out = []
    for p in conn.execute("SELECT * FROM podcasts WHERE (? IS NULL OR id = ?) ORDER BY name COLLATE NOCASE",
                          (podcast_id, podcast_id)):
        keep, age = effective_rules(p, defaults)
        rows = conn.execute("SELECT * FROM episodes WHERE podcast_id = ? AND state != 'deleted'", (p["id"],)).fetchall()
        item = PodcastPlan(p["id"], p["name"], keep, age, len(rows))
        if keep or age:
            item.candidates, item.capped = select(rows, keep, age, today, budget)
            budget -= len(item.candidates)
        out.append(item)
    return out


def _file_names(row) -> list[str]:
    return [row[c] for c in episodes.FILE_COLUMNS if row[c]]


def _file_path(media_dir: Path, podcast_id: str, name: str) -> Path | None:
    """A regular file directly inside media/<podcast>; anything else (path tricks, symlinks) is left alone."""
    if name != Path(name).name or name.startswith("."):
        return None
    root = media_dir / podcast_id
    path = root / name
    if root.is_symlink() or path.is_symlink() or not path.is_file():
        return None
    return path


def delete_files(conn: sqlite3.Connection, media_dir: Path, podcast_id: str, video_id: str, reason: str,
                 now: datetime | None = None) -> tuple[int, int] | None:
    """Mark the episode ``deleted``, log it, then remove its files. Returns ``(files, bytes)`` or None if
    there was nothing to delete (missing or already deleted).

    The row changes first: if removing a file then fails, the feed is already correct and only a stray file
    is left (a later re-download overwrites it).
    """
    now = now or queue.utcnow()
    with queue.transaction(conn):
        row = episodes.get(conn, podcast_id, video_id)
        if row is None or row["state"] == "deleted":
            return None
        paths = [p for p in (_file_path(media_dir, podcast_id, n) for n in _file_names(row)) if p]
        freed = sum(p.stat().st_size for p in paths)
        sets = ", ".join(f"{c} = NULL" for c in episodes.FILE_COLUMNS)
        conn.execute(f"UPDATE episodes SET state = 'deleted', {sets}, size_bytes = 0, updated_at = ? "
                     "WHERE podcast_id = ? AND video_id = ?", (now.strftime(TIME_FORMAT), podcast_id, video_id))
        conn.execute("INSERT INTO retention_log (at, podcast_id, video_id, title, reason, files, bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (now.strftime(TIME_FORMAT), podcast_id, video_id, row["title"], reason, len(paths), freed))
    for path in paths:
        try:
            path.unlink()
        except OSError as exc:
            log.warning("Could not remove %s: %s", path, exc)
    return len(paths), freed


def run(conn: sqlite3.Connection, media_dir: Path, defaults: dict, podcast_id: str | None = None,
        now: datetime | None = None) -> dict:
    """Carry out the plan. Returns the totals."""
    now = now or queue.utcnow()
    deleted = freed = capped = 0
    for item in plan(conn, defaults, podcast_id, now.date()):
        capped += item.capped
        for c in item.candidates:
            result = delete_files(conn, media_dir, item.podcast_id, c.video_id, c.reason, now)
            if result:
                deleted += 1
                freed += result[1]
    if deleted or capped:
        log.info("Retention: deleted the files of %d episode(s), freed %d bytes, %d held back by the limit", deleted, freed, capped)
    return {"deleted": deleted, "bytes": freed, "capped": capped}


def redownload(conn: sqlite3.Connection, media_dir: Path, podcast_id: str, video_id: str,
               now: datetime | None = None) -> int:
    """Remove the files and queue a new download. Returns the task id; BadState if one is already open."""
    row = episodes.get(conn, podcast_id, video_id)
    open_task = conn.execute("SELECT 1 FROM tasks WHERE type = 'download' AND podcast_id = ? AND video_id = ? "
                             "AND state IN ('pending', 'claimed', 'paused')", (podcast_id, video_id)).fetchone()
    if row is None:
        raise queue.BadState("no such episode")
    if open_task:
        raise queue.BadState("a download of this video is already queued or running")
    delete_files(conn, media_dir, podcast_id, video_id, "redownload", now)
    return queue.requeue_download(conn, podcast_id, video_id, row["title"], now)


def recent_log(conn: sqlite3.Connection, podcast_id: str | None = None, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute("SELECT l.*, p.name AS podcast_name FROM retention_log l LEFT JOIN podcasts p ON p.id = l.podcast_id "
                        "WHERE (? IS NULL OR l.podcast_id = ?) ORDER BY l.id DESC LIMIT ?",
                        (podcast_id, podcast_id, limit)).fetchall()


# --- nightly -------------------------------------------------------------------------------------

def nightly_due(conn: sqlite3.Connection, defaults: dict, now: datetime) -> bool:
    if not defaults["retention_enabled"]:
        return False
    hour, minute = (int(x) for x in defaults["retention_time"].split(":"))
    if (now.hour, now.minute) < (hour, minute):
        return False
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (LAST_RUN_KEY,)).fetchone()
    return not row or row[0] != now.date().isoformat()


def run_nightly(db_path: Path, media_dir: Path, defaults: dict | None = None, now: datetime | None = None) -> dict | None:
    """Run once per day after the configured time (called from the scheduler tick)."""
    now = now or queue.utcnow()
    conn = connect(db_path)
    try:
        values = settings.get_all(conn, defaults)
        if not nightly_due(conn, values, now):
            return None
        settings.save(conn, {LAST_RUN_KEY: now.date().isoformat()})   # first: a crash must not loop all day
        return run(conn, media_dir, values, None, now)
    finally:
        conn.close()
