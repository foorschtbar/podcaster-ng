import sqlite3

import pytest

from app.db import connect
from app.migrations import DEFAULT_DIR, MigrationError, current_version, discover, migrate

LATEST = discover(DEFAULT_DIR)[-1][0]
ALL = list(range(1, LATEST + 1))


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "podcaster.db", tmp_path / "backups"


@pytest.fixture
def mdir(tmp_path):
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0001_init.sql").write_text(
        (DEFAULT_DIR / "0001_init.sql").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return d


def versions(db):
    conn = connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    finally:
        conn.close()


def test_connection_pragmas(paths):
    conn = connect(paths[0])
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()


def test_fresh_database(paths):
    db, backups = paths
    assert migrate(db, backups) == LATEST
    assert versions(db) == ALL
    conn = connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"schema_migrations", "settings", "podcasts"} <= tables
    assert not backups.exists()  # nothing to back up for a new database


def test_second_start_does_not_reapply(paths):
    db, backups = paths
    migrate(db, backups)
    conn = connect(db)
    applied = conn.execute("SELECT applied_at FROM schema_migrations").fetchone()[0]
    conn.close()
    assert migrate(db, backups) == LATEST
    conn = connect(db)
    assert conn.execute("SELECT applied_at FROM schema_migrations").fetchone()[0] == applied
    conn.close()
    assert versions(db) == ALL
    assert not backups.exists()


def test_pending_migration_applied_once_with_backup(paths, mdir):
    db, backups = paths
    migrate(db, backups, mdir)
    conn = connect(db)
    conn.execute("INSERT INTO settings (key, value) VALUES ('k', 'v')")
    conn.close()

    (mdir / "0002_extra.sql").write_text("CREATE TABLE extra (id INTEGER PRIMARY KEY);")
    assert migrate(db, backups, mdir) == 2
    assert versions(db) == [1, 2]

    copies = list(backups.glob("*.db"))
    assert len(copies) == 1
    old = sqlite3.connect(copies[0])
    assert old.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0] == "v"
    assert old.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
    assert not old.execute("SELECT 1 FROM sqlite_master WHERE name='extra'").fetchone()
    old.close()

    migrate(db, backups, mdir)  # again: nothing pending, no further backup
    assert versions(db) == [1, 2]
    assert len(list(backups.glob("*.db"))) == 1


def test_failed_migration_rolls_back(paths, mdir):
    db, backups = paths
    migrate(db, backups, mdir)
    (mdir / "0002_bad.sql").write_text("CREATE TABLE ok (id INTEGER); INSERT INTO nope VALUES (1);")
    with pytest.raises(MigrationError, match="0002_bad.sql"):
        migrate(db, backups, mdir)
    conn = connect(db)
    assert current_version(conn) == 1
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ok'").fetchone()
    conn.close()


def test_newer_database_is_refused(paths):
    db, backups = paths
    migrate(db, backups)
    conn = connect(db)
    conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (99, 'x')")
    conn.close()
    with pytest.raises(MigrationError, match="newer than this code"):
        migrate(db, backups)
    assert versions(db) == [*ALL, 99]
    assert not backups.exists()
