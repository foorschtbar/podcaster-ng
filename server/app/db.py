"""The single SQLite connection helper: WAL, busy timeout, foreign keys."""

import sqlite3
from pathlib import Path


def connect(path: Path | str) -> sqlite3.Connection:
    # isolation_level=None: autocommit, transactions are managed explicitly.
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
