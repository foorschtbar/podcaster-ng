"""Migration runner.

Numbered files ``NNNN_name.sql`` in the migrations directory are applied in
order, each in its own transaction. Before any pending migration is applied to
an existing database, a consistent copy is written to the backup directory.
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import connect

log = logging.getLogger(__name__)

_NAME = re.compile(r"^(\d{4})_[A-Za-z0-9_\-]+\.sql$")
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "migrations"


class MigrationError(Exception):
    pass


def discover(directory: Path) -> list[tuple[int, Path]]:
    found: dict[int, Path] = {}
    for p in sorted(directory.glob("*.sql")):
        m = _NAME.match(p.name)
        if not m:
            raise MigrationError(f"Invalid migration file name: {p.name}")
        version = int(m.group(1))
        if version in found:
            raise MigrationError(f"Duplicate migration version {version:04d}")
        found[version] = p
    return sorted(found.items())


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]


def _backup(conn: sqlite3.Connection, db_path: Path, backup_dir: Path, version: int) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"{db_path.stem}-v{version:04d}-{stamp}.db"
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def migrate(db_path: Path, backup_dir: Path, migrations_dir: Path = DEFAULT_DIR) -> int:
    """Bring the database up to date and return its version."""
    migrations = discover(migrations_dir)
    code_version = migrations[-1][0] if migrations else 0
    existed = db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path)
    try:
        db_version = current_version(conn)
        if db_version > code_version:
            raise MigrationError(
                f"Database schema version {db_version} is newer than this code "
                f"(supports up to {code_version}). Refusing to start; "
                f"run a newer server version or restore a backup."
            )
        pending = [(v, p) for v, p in migrations if v > db_version]
        if not pending:
            return db_version
        if existed and db_version > 0:
            target = _backup(conn, db_path, backup_dir, db_version)
            log.info("Database backed up to %s", target)
        for version, path in pending:
            _apply(conn, version, path)
            log.info("Applied migration %s", path.name)
        return pending[-1][0]
    finally:
        conn.close()


def _apply(conn: sqlite3.Connection, version: int, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    # executescript() runs outside Python's implicit transaction handling, so
    # the transaction is spelled out in the script itself.
    script = (
        "BEGIN;\n"
        f"{sql}\n;\n"
        "INSERT INTO schema_migrations (version, applied_at) "
        f"VALUES ({int(version)}, strftime('%Y-%m-%dT%H:%M:%SZ','now'));\n"
        "COMMIT;"
    )
    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(f"Migration {path.name} failed: {exc}") from exc
