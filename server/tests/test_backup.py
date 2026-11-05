import importlib.util
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import backup, notifications, scheduler, settings
from app.db import connect
from app.main import create_app

from helpers import make_config
from qhelpers import add_podcast, make_db

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "restore_db.py"


def load_script():
    spec = importlib.util.spec_from_file_location("restore_db", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path):
    path = make_db(tmp_path)
    conn = connect(path)
    add_podcast(conn, "abgt")
    conn.close()
    return path, tmp_path / "backups"


def rows(path, sql):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_a_backup_is_a_consistent_verified_copy(db):
    path, bdir = db
    target = backup.create(path, bdir, NOW)
    assert target == bdir / "daily" / "podcaster-20261105.db" and not list((bdir / "daily").glob(".*"))
    assert backup.verify(target) >= 9
    assert rows(target, "SELECT id FROM podcasts") == [("abgt",)]


def test_a_backup_taken_while_the_server_writes_is_consistent(db):
    path, bdir = db
    stop = threading.Event()

    def writer():
        conn = connect(path)
        n = 0
        while not stop.is_set():
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO settings (key, value) VALUES (?, 'x') ON CONFLICT(key) DO UPDATE SET value = value || 'x'", (f"k{n % 50}",))
            conn.execute("INSERT INTO settings (key, value) VALUES ('mirror', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(n),))
            conn.execute("COMMIT")
            n += 1
        conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for day in range(5):
            target = backup.create(path, bdir, NOW + timedelta(days=day))
            assert backup.verify(target) >= 9
    finally:
        stop.set()
        thread.join()


def test_verify_rejects_damaged_and_foreign_files(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database" * 100)
    with pytest.raises(backup.BackupError):
        backup.verify(junk)
    other = tmp_path / "other.db"
    sqlite3.connect(str(other)).execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(backup.BackupError, match="not a PodcasterNG database"):
        backup.verify(other)
    with pytest.raises(backup.BackupError):
        backup.verify(tmp_path / "missing.db")


def test_prune_keeps_the_newest_copies_and_always_at_least_one(db):
    path, bdir = db
    for day in range(6):
        backup.create(path, bdir, NOW + timedelta(days=day))
    (bdir / "daily" / "notes.txt").write_text("not a backup")
    removed = backup.prune(bdir, 3)
    assert [p.name for p in removed] == ["podcaster-20261107.db", "podcaster-20261106.db", "podcaster-20261105.db"]
    assert [p.name for p in backup.listing(bdir)] == ["podcaster-20261110.db", "podcaster-20261109.db", "podcaster-20261108.db"]
    assert (bdir / "daily" / "notes.txt").exists()
    backup.prune(bdir, 0)
    assert len(backup.listing(bdir)) == 1


def test_the_daily_run_happens_once_a_day_after_the_configured_time(db):
    path, bdir = db
    at = lambda h, m=0, d=5: datetime(2026, 11, d, h, m, tzinfo=timezone.utc)
    assert backup.run_daily(path, bdir, None, at(2, 29)) is None                     # before 02:30
    assert backup.run_daily(path, bdir, None, at(2, 30)).name == "podcaster-20261105.db"
    assert backup.run_daily(path, bdir, None, at(23)) is None                        # already done today
    assert backup.run_daily(path, bdir, None, at(2, 31, 6)).name == "podcaster-20261106.db"


def test_the_retention_setting_limits_the_number_of_copies(db):
    path, bdir = db
    conn = connect(path)
    settings.save(conn, {"backup_keep": 2})
    conn.close()
    for day in range(1, 5):
        backup.run_daily(path, bdir, None, datetime(2026, 11, day, 5, 0, tzinfo=timezone.utc))
    assert [p.name for p in backup.listing(bdir)] == ["podcaster-20261104.db", "podcaster-20261103.db"]


def test_the_scheduler_makes_the_backup_and_a_failure_is_alerted_once(db, monkeypatch):
    path, bdir = db
    assert scheduler.tick(path, now=NOW, backup_dir=bdir)["backup"].name == "podcaster-20261105.db"
    monkeypatch.setattr(backup, "create", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    later = NOW + timedelta(days=1)
    assert scheduler.tick(path, now=later, backup_dir=bdir)["backup"] is None
    scheduler.tick(path, now=later + timedelta(minutes=5), backup_dir=bdir)           # not retried every tick
    conn = connect(path)
    (msg,) = [r for r in notifications.recent(conn, 20) if r["kind"] == "backup_failed"]
    assert "backup failed" in msg["text"]
    conn.close()


def test_restore_round_trip(tmp_path):
    from app.migrations import migrate
    live = tmp_path / "podcaster.db"
    migrate(live, tmp_path / "backups")
    conn = connect(live)
    add_podcast(conn, "abgt")
    conn.close()
    script = load_script()
    target = backup.create(live, tmp_path / "backups", NOW)
    conn = connect(live)
    conn.execute("DELETE FROM podcasts")                                             # the disaster
    conn.close()
    assert rows(live, "SELECT COUNT(*) FROM podcasts") == [(0,)]
    info = script.check(target)
    assert info["podcasts"] == 1 and info["version"] >= 9
    moved = script.restore(target, tmp_path)
    assert rows(live, "SELECT id FROM podcasts") == [("abgt",)]
    assert moved.name.startswith("podcaster.db.before-restore-") and rows(moved, "SELECT COUNT(*) FROM podcasts") == [(0,)]
    # and the app starts on the restored database
    with TestClient(create_app(make_config(tmp_path))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/abgt.xml").status_code == 200


def test_restore_refuses_a_bad_copy_and_leaves_the_database_alone(db, tmp_path):
    path, _ = db
    script = load_script()
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"nope" * 1000)
    with pytest.raises(sqlite3.DatabaseError):
        script.restore(junk, path.parent)
    assert not list(path.parent.glob("*.before-restore-*"))
    assert rows(path, "SELECT id FROM podcasts") == [("abgt",)]


def test_settings_validate_the_backup_fields():
    from helpers import settings_form
    base = settings_form()
    assert settings.validate(base)[1] == {}
    assert "backup_time" in settings.validate({**base, "backup_time": "99:00"})[1]
    assert "backup_keep" in settings.validate({**base, "backup_keep": "0"})[1]
