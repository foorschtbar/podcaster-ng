#!/usr/bin/env python3
"""Check or restore a PodcasterNG database backup (no dependencies besides Python 3.10+).

  restore_db.py --check  data/backups/daily/podcaster-20261105.db
  restore_db.py          data/backups/daily/podcaster-20261105.db  data/        # STOP THE SERVER FIRST

--check opens the copy read-only and reports its schema version and row counts. A restore verifies the copy,
moves the current podcaster.db (and its -wal/-shm files) aside as podcaster.db.before-restore-<time>, and puts
the copy in place. Nothing is deleted. Start the server afterwards; it applies newer migrations by itself
(after copying the database to data/backups/).
"""

import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def check(path: Path) -> dict:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit(f"{path}: integrity check failed")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'").fetchone():
            raise SystemExit(f"{path}: not a PodcasterNG database")
        info = {"version": conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]}
        for table in ("podcasts", "episodes", "tasks", "workers", "hits_daily"):
            info[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return info
    finally:
        conn.close()


def restore(backup: Path, data_dir: Path) -> Path | None:
    check(backup)
    live = data_dir / "podcaster.db"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    moved = None
    for suffix in ("", "-wal", "-shm"):
        old = Path(str(live) + suffix)
        if old.exists():
            target = Path(str(live) + f".before-restore-{stamp}" + suffix)
            old.rename(target)
            moved = moved or target
    shutil.copy2(backup, live)
    check(live)
    return moved


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--check"]
    if "--check" in argv and len(args) == 1:
        print(check(Path(args[0])))
        return 0
    if len(args) != 2:
        print(__doc__)
        return 2
    backup, data_dir = Path(args[0]), Path(args[1])
    if not data_dir.is_dir():
        raise SystemExit(f"{data_dir} is not a directory")
    moved = restore(backup, data_dir)
    print(f"Restored {backup} to {data_dir / 'podcaster.db'}" + (f"; the previous database is now {moved}" if moved else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
