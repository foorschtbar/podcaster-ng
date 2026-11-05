from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import queue, scheduler, workers
from app.db import connect
from app.main import create_app

from helpers import make_config
from qhelpers import NOW, add_podcast, add_worker, claim, make_db, task


@pytest.fixture
def db(tmp_path):
    return make_db(tmp_path)


def test_tick_creates_scans_for_active_podcasts_only_once(db):
    c = connect(db)
    add_podcast(c, "a")
    add_podcast(c, "b", active=0)
    first, second = scheduler.tick(db), scheduler.tick(db)
    assert (first["expired"], first["scans_created"]) == (0, 1)
    assert second["scans_created"] == 0                                        # one open scan is enough
    assert [(r["podcast_id"], r["state"]) for r in queue.list_tasks(c)] == [("a", "pending")]
    c.close()


def test_tick_expires_leases(db):
    c = connect(db)
    add_podcast(c)
    w = add_worker(c)
    tid = queue.create_scan(c, "abgt", NOW)
    claim(c, w, lease=60)                         # claimed long ago in real time
    assert scheduler.tick(db)["expired"] == 1
    assert task(c, tid)["state"] == "pending" and task(c, tid)["failures"] == 1
    c.close()


def test_tick_uses_the_default_interval_from_settings(db):
    c = connect(db)
    from app import settings
    settings.save(c, {"scan_interval_min": 5})
    add_podcast(c)
    tid = queue.create_scan(c, "abgt", NOW - timedelta(hours=1))
    w = add_worker(c)
    claim(c, w, now=NOW - timedelta(hours=1))
    queue.complete_scan(c, tid, w["id"], [], NOW - timedelta(minutes=10))
    assert scheduler.tick(db)["scans_created"] == 1                           # finished 10 min ago, interval 5 min
    c.close()


def test_scheduler_runs_inside_the_app_when_enabled(tmp_path):
    config = make_config(tmp_path, run_scheduler=True, scheduler_tick_seconds=0.1)
    with TestClient(create_app(config)) as client:
        c = connect(config.db_path)
        add_podcast(c)
        import time
        for _ in range(60):
            if c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]:
                break
            time.sleep(0.1)
        assert c.execute("SELECT type, state FROM tasks").fetchall()[0]["type"] == "scan"
        c.close()


def test_scheduler_is_off_in_test_config_by_default(tmp_path):
    config = make_config(tmp_path)
    assert config.run_scheduler is False
