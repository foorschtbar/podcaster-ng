"""Daily copy of the database with the SQLite backup API (never a raw file copy of a database in use).

Copies go to ``<backup_dir>/daily/podcaster-YYYYMMDD.db``: written under a temporary name, checked, then renamed, so a
half-written copy is never mistaken for a good one. The newest ``backup_keep`` copies are kept.
"""

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import queue, settings
from .db import connect

log = logging.getLogger(__name__)
LAST_RUN_KEY = "backup_last_run"          # raw settings row (a date), not an editable setting
_NAME = re.compile(r"^podcaster-\d{8}\.db$")


class BackupError(Exception):
    pass


def verify(path: Path) -> int:
    """Open a copy read-only and check it. Returns its schema version; raises BackupError if it is not usable."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open {path.name}: {exc}") from None
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError(f"{path.name} failed the integrity check")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'").fetchone():
            raise BackupError(f"{path.name} is not a PodcasterNG database")
        return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    except sqlite3.Error as exc:
        raise BackupError(f"{path.name} is damaged: {exc}") from None
    finally:
        conn.close()


def create(db_path: Path, backup_dir: Path, now: datetime | None = None) -> Path:
    """Write today's copy (replacing an earlier one of the same day) and return its path."""
    now = now or queue.utcnow()
    target_dir = backup_dir / "daily"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"podcaster-{now:%Y%m%d}.db"
    tmp = target_dir / f".{target.name}.tmp"
    tmp.unlink(missing_ok=True)
    source = connect(db_path)
    dest = sqlite3.connect(str(tmp))
    try:
        source.backup(dest)
        # The copy inherits WAL mode; switch it off so the file is self-contained (no -wal/-shm beside it).
        dest.execute("PRAGMA journal_mode=DELETE")
    finally:
        dest.close()
        source.close()
    try:
        verify(tmp)
    except BackupError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(target)
    return target


def prune(backup_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` daily copies (the date is in the name). Returns what was removed."""
    files = sorted((p for p in (backup_dir / "daily").glob("podcaster-*.db") if _NAME.match(p.name)), reverse=True)
    removed = files[max(1, keep):]
    for path in removed:
        path.unlink(missing_ok=True)
    return removed


def listing(backup_dir: Path) -> list[Path]:
    return sorted((p for p in (backup_dir / "daily").glob("podcaster-*.db") if _NAME.match(p.name)), reverse=True)


def due(conn: sqlite3.Connection, defaults: dict, now: datetime) -> bool:
    hour, minute = (int(x) for x in defaults["backup_time"].split(":"))
    if (now.hour, now.minute) < (hour, minute):
        return False
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (LAST_RUN_KEY,)).fetchone()
    return not row or row[0] != now.date().isoformat()


def run_daily(db_path: Path, backup_dir: Path, env_defaults: dict | None = None, now: datetime | None = None) -> Path | None:
    """Make the daily copy once per day after the configured time (called from the scheduler tick)."""
    now = now or queue.utcnow()
    conn = connect(db_path)
    try:
        values = settings.get_all(conn, env_defaults)
        if not due(conn, values, now):
            return None
        settings.save(conn, {LAST_RUN_KEY: now.date().isoformat()})   # first: a failure must not retry every tick
    finally:
        conn.close()
    path = create(db_path, backup_dir, now)
    removed = prune(backup_dir, values["backup_keep"])
    log.info("Database backup written to %s (%d old copies removed)", path.name, len(removed))
    return path
