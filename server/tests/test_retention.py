import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import episodes, queue, retention, scheduler, settings, stats
from app.db import connect
from app.main import create_app

from helpers import csrf_of, do_login, make_config, settings_form
from qhelpers import NOW, add_podcast, make_db

TODAY = date(2026, 11, 5)
DAY0 = date(2026, 1, 1)


def rows(*days, size=100):
    """Episode-like dicts, one per day offset, ids e0, e1, ... in the order given."""
    return [{"video_id": f"e{i}", "title": f"t{i}", "published_at": (DAY0 + timedelta(days=d)).isoformat() if d is not None else None,
             "created_at": "2026-01-01T00:00:00Z", "size_bytes": size} for i, d in enumerate(days)]


def ids(candidates):
    return [c.video_id for c in candidates]


# --- selection ----------------------------------------------------------------------------------------------------

def test_keep_last_selects_everything_beyond_the_newest_n_oldest_first():
    picked, capped = retention.select(rows(*range(20)), 10, None, TODAY)
    assert ids(picked) == [f"e{i}" for i in range(10)] and capped == 0
    assert {c.reason for c in picked} == {"keep_last"}


def test_fewer_episodes_than_n_deletes_nothing():
    assert retention.select(rows(*range(5)), 10, None, TODAY) == ([], 0)
    assert retention.select(rows(*range(10)), 10, None, TODAY) == ([], 0)


def test_no_rule_deletes_nothing():
    assert retention.select(rows(*range(50)), None, None, TODAY) == ([], 0)


def test_max_age_selects_older_episodes_but_never_the_newest():
    old = (TODAY - DAY0).days
    picked, _ = retention.select(rows(old - 1, old - 400, old - 500), None, 365, TODAY)
    assert ids(picked) == ["e2", "e1"] and {c.reason for c in picked} == {"max_age"}
    # everything is old: the newest one still stays
    picked, _ = retention.select(rows(10, 20, 30), None, 30, TODAY)
    assert ids(picked) == ["e0", "e1"]                       # e2 is the newest (day 30) and stays


def test_both_rules_combine_and_keep_last_names_the_reason():
    old = (TODAY - DAY0).days
    data = rows(old, old - 1, old - 2, old - 3, old - 400)    # 5 episodes, the last is old
    picked, _ = retention.select(data, 3, 365, TODAY)
    assert ids(picked) == ["e4", "e3"]
    assert [c.reason for c in picked] == ["keep_last", "keep_last"]
    picked, _ = retention.select(data, 4, 365, TODAY)
    assert [(c.video_id, c.reason) for c in picked] == [("e4", "keep_last")]
    picked, _ = retention.select(data, 10, 365, TODAY)
    assert [(c.video_id, c.reason) for c in picked] == [("e4", "max_age")]


def test_same_dates_are_ordered_stably_by_creation_time_then_id():
    data = [{"video_id": v, "title": v, "published_at": "2026-02-01", "created_at": c, "size_bytes": 1}
            for v, c in (("a", "2026-02-01T10:00:00Z"), ("b", "2026-02-01T11:00:00Z"), ("c", "2026-02-01T11:00:00Z"))]
    picked, _ = retention.select(data, 1, None, TODAY)
    assert ids(picked) == ["a", "b"]                          # c is newest (same time as b, larger id); oldest goes first
    assert ids(retention.select(list(reversed(data)), 1, None, TODAY)[0]) == ["a", "b"]


def test_a_missing_publication_date_falls_back_to_the_creation_date():
    data = rows(None, None)
    data[0]["created_at"], data[1]["created_at"] = "2026-03-01T00:00:00Z", "2026-02-01T00:00:00Z"
    assert ids(retention.select(data, 1, None, TODAY)[0]) == ["e1"]


def test_the_per_run_limit_keeps_the_oldest_and_counts_the_rest():
    picked, capped = retention.select(rows(*range(20)), 5, None, TODAY, limit=4)
    assert ids(picked) == ["e0", "e1", "e2", "e3"] and capped == 11
    assert retention.select(rows(*range(20)), 5, None, TODAY, limit=0) == ([], 15)


# --- plan and run on a real database -----------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    db_path = make_db(tmp_path)
    conn = connect(db_path)
    media = tmp_path / "media"
    media.mkdir()
    yield conn, media, tmp_path, db_path
    conn.close()


def add_episode(conn, media, pid, n, day, state="available", files=True):
    vid = f"vid{n:03d}"
    name = f"Episode {n}"
    cols = {}
    if files:
        (media / pid).mkdir(parents=True, exist_ok=True)
        for col, ext in (("audio_file", ".m4a"), ("thumb_file", ".webp"), ("square_file", ".square.jpg"),
                         ("chapters_file", ".chapters.json"), ("info_file", ".info.json")):
            (media / pid / f"{name}{ext}").write_bytes(b"x" * 10)
            cols[col] = f"{name}{ext}"
    episodes.insert(conn, podcast_id=pid, video_id=vid, title=name, published_at=day, size_bytes=50 if files else 0,
                    state=state, **cols)
    return vid


def twenty(conn, media, pid="abgt"):
    add_podcast(conn, pid)
    for n in range(20):
        add_episode(conn, media, pid, n, (DAY0 + timedelta(days=n)).isoformat())


def defaults_of(conn):
    return settings.get_all(conn)


def test_acceptance_keep_last_10_of_20(env):
    conn, media, _, _ = env
    twenty(conn, media)
    conn.execute("UPDATE podcasts SET keep_last = 10")
    (p,) = retention.plan(conn, defaults_of(conn))
    assert len(p.candidates) == 10 and p.bytes == 500
    assert ids(p.candidates) == [f"vid{n:03d}" for n in range(10)]
    result = retention.run(conn, media, defaults_of(conn))
    assert result == {"deleted": 10, "bytes": 500, "capped": 0}
    states = dict(conn.execute("SELECT video_id, state FROM episodes").fetchall())
    assert [v for v, s in states.items() if s == "deleted"] == [f"vid{n:03d}" for n in range(10)]
    assert all(states[f"vid{n:03d}"] == "available" for n in range(10, 20))
    left = sorted(p.name for p in (media / "abgt").iterdir())
    assert len(left) == 50 and not any(f"Episode {n}." in name for n in range(10) for name in left)
    deleted = conn.execute("SELECT * FROM episodes WHERE state = 'deleted'").fetchall()
    assert all(r[c] is None for r in deleted for c in episodes.FILE_COLUMNS) and all(r["size_bytes"] == 0 for r in deleted)
    log = retention.recent_log(conn)
    assert len(log) == 10 and {r["reason"] for r in log} == {"keep_last"} and all(r["files"] == 5 and r["bytes"] == 50 for r in log)
    assert retention.run(conn, media, defaults_of(conn))["deleted"] == 0           # nothing left to do


def test_a_scan_does_not_download_deleted_episodes_again(env):
    conn, media, _, _ = env
    twenty(conn, media)
    conn.execute("UPDATE podcasts SET keep_last = 10, scan_depth = 30")
    retention.run(conn, media, defaults_of(conn))
    worker = conn.execute("INSERT INTO workers (name, token_hash, created_at, updated_at) VALUES ('w', 'h', 'x', 'x')").lastrowid
    task_id = queue.create_scan(conn, "abgt", NOW)
    conn.execute("UPDATE tasks SET state = 'claimed', claimed_by = ? WHERE id = ?", (worker, task_id))
    videos = [{"id": f"vid{n:03d}", "title": f"Episode {n}"} for n in range(25)]
    out = queue.complete_scan(conn, task_id, worker, videos, NOW)
    assert out["known"] == 20 and out["created"] == 5                     # only the five really new ones


def test_default_rules_from_settings_apply_when_the_podcast_has_none(env):
    conn, media, _, _ = env
    twenty(conn, media)
    settings.save(conn, {"keep_last": 15})
    (p,) = retention.plan(conn, defaults_of(conn))
    assert p.keep_last == 15 and len(p.candidates) == 5
    conn.execute("UPDATE podcasts SET keep_last = 18")                    # own rule wins
    assert len(retention.plan(conn, defaults_of(conn))[0].candidates) == 2


def test_the_per_run_limit_is_shared_by_all_podcasts(env):
    conn, media, _, _ = env
    for pid in ("aaa", "bbb"):
        add_podcast(conn, pid, keep_last=2)
        for n in range(6):
            add_episode(conn, media, pid, n, (DAY0 + timedelta(days=n)).isoformat())
    settings.save(conn, {"retention_max_per_run": 6})
    plans = retention.plan(conn, defaults_of(conn))
    assert [len(p.candidates) for p in plans] == [4, 2] and [p.capped for p in plans] == [0, 2]
    result = retention.run(conn, media, defaults_of(conn))
    assert result["deleted"] == 6 and result["capped"] == 2
    assert retention.run(conn, media, defaults_of(conn))["deleted"] == 2     # the next run continues


def test_the_newest_episode_survives_even_with_an_absurd_rule(env):
    conn, media, _, _ = env
    twenty(conn, media)
    conn.execute("UPDATE podcasts SET max_age_days = 1")
    retention.run(conn, media, defaults_of(conn))
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE state = 'available'").fetchone()[0] == 1
    assert conn.execute("SELECT video_id FROM episodes WHERE state = 'available'").fetchone()[0] == "vid019"


def test_hidden_episodes_count_and_deleted_ones_do_not(env):
    conn, media, _, _ = env
    add_podcast(conn, "abgt", keep_last=2)
    add_episode(conn, media, "abgt", 1, "2026-01-01")
    add_episode(conn, media, "abgt", 2, "2026-01-02", state="hidden")
    add_episode(conn, media, "abgt", 3, "2026-01-03")
    add_episode(conn, media, "abgt", 4, "2025-12-01", state="deleted", files=False)
    (p,) = retention.plan(conn, defaults_of(conn))
    assert p.total == 3 and ids(p.candidates) == ["vid001"]


def test_a_missing_file_or_a_symlink_does_not_break_the_run(env):
    conn, media, tmp, _ = env
    twenty(conn, media)
    conn.execute("UPDATE podcasts SET keep_last = 19")
    (media / "abgt" / "Episode 0.m4a").unlink()
    outside = tmp / "outside.txt"
    outside.write_text("keep me")
    (media / "abgt" / "Episode 0.webp").unlink()
    (media / "abgt" / "Episode 0.webp").symlink_to(outside)
    result = retention.run(conn, media, defaults_of(conn))
    assert result["deleted"] == 1 and outside.read_text() == "keep me"
    assert retention.recent_log(conn)[0]["files"] == 3                    # the missing file and the symlink are not counted


def test_a_failing_unlink_is_logged_and_the_row_is_still_deleted(env, monkeypatch):
    conn, media, _, _ = env
    add_podcast(conn, "abgt")
    vid = add_episode(conn, media, "abgt", 1, "2026-01-01")
    monkeypatch.setattr("pathlib.Path.unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("nope")))
    assert retention.delete_files(conn, media, "abgt", vid, "manual") == (5, 50)
    assert episodes.get(conn, "abgt", vid)["state"] == "deleted"


def test_path_tricks_in_file_names_never_leave_the_podcast_directory(env):
    conn, media, tmp, _ = env
    add_podcast(conn, "abgt")
    secret = tmp / "secret.txt"
    secret.write_text("s")
    vid = add_episode(conn, media, "abgt", 1, "2026-01-01")
    conn.execute("UPDATE episodes SET info_file = '../../secret.txt', chapters_file = '.hidden'")
    retention.delete_files(conn, media, "abgt", vid, "manual")
    assert secret.exists()


def test_stats_history_survives_a_deletion(env):
    conn, media, _, _ = env
    add_podcast(conn, "abgt")
    add_episode(conn, media, "abgt", 1, "2026-01-01")
    add_episode(conn, media, "abgt", 2, "2026-01-02")
    stats.record_hit(conn, day=date.today(), podcast_id="abgt", episode_id="vid001", kind="file", app_group="Other", count=7, nbytes=70)
    retention.delete_files(conn, media, "abgt", "vid001", "manual")
    (top,) = stats.top_episodes(conn, 30)
    assert top["title"] == "Episode 1" and top["count"] == 7


def test_redownload_removes_the_files_and_queues_a_task_once(env):
    conn, media, _, _ = env
    add_podcast(conn, "abgt")
    vid = add_episode(conn, media, "abgt", 1, "2026-01-01")
    task_id = retention.redownload(conn, media, "abgt", vid, NOW)
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert t["state"] == "pending" and t["type"] == "download" and t["video_id"] == vid and t["title"] == "Episode 1"
    assert episodes.get(conn, "abgt", vid)["state"] == "deleted" and not list((media / "abgt").iterdir())
    with pytest.raises(queue.BadState):
        retention.redownload(conn, media, "abgt", vid, NOW)                # already queued
    with pytest.raises(queue.BadState):
        retention.redownload(conn, media, "abgt", "unknown", NOW)


def test_redownload_reuses_a_finished_task(env):
    conn, media, _, _ = env
    add_podcast(conn, "abgt")
    vid = add_episode(conn, media, "abgt", 1, "2026-01-01")
    old = conn.execute("INSERT INTO tasks (type, podcast_id, video_id, state, failures, created_at, updated_at, finished_at) "
                       "VALUES ('download', 'abgt', ?, 'done', 3, 'x', 'x', 'x')", (vid,)).lastrowid
    assert retention.redownload(conn, media, "abgt", vid, NOW) == old
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (old,)).fetchone()
    assert (t["state"], t["failures"], t["finished_at"], t["tier"]) == ("pending", 0, None, 1)


# --- nightly run -------------------------------------------------------------------------------------------------

def test_nightly_runs_once_a_day_after_the_configured_time_and_only_when_enabled(env):
    conn, media, _, db_path = env
    twenty(conn, media)
    conn.execute("UPDATE podcasts SET keep_last = 10")
    at = lambda h, m=0, d=5: datetime(2026, 11, d, h, m, tzinfo=timezone.utc)
    assert retention.run_nightly(db_path, media, None, at(4)) is None                  # switched off by default
    settings.save(conn, {"retention_enabled": 1, "retention_time": "03:45"})
    assert retention.run_nightly(db_path, media, None, at(3, 44)) is None              # too early
    assert retention.run_nightly(db_path, media, None, at(3, 45))["deleted"] == 10
    assert retention.run_nightly(db_path, media, None, at(23)) is None                 # once per day
    add_episode(conn, media, "abgt", 99, "2026-06-01")
    assert retention.run_nightly(db_path, media, None, at(3, 50, 6))["deleted"] == 1   # next day again


def test_the_scheduler_tick_runs_the_nightly_cleanup(env):
    conn, media, _, db_path = env
    twenty(conn, media)
    conn.execute("UPDATE podcasts SET keep_last = 10")
    settings.save(conn, {"retention_enabled": 1, "retention_time": "00:00"})
    out = scheduler.tick(db_path, now=NOW, media_dir=media)
    assert out["retention"]["deleted"] == 10
    assert scheduler.tick(db_path, now=NOW, media_dir=media)["retention"] is None


def test_a_crash_in_the_cleanup_does_not_stop_the_tick(env, monkeypatch):
    conn, media, _, db_path = env
    monkeypatch.setattr(retention, "run_nightly", lambda *a, **k: 1 / 0)
    assert scheduler.tick(db_path, now=NOW, media_dir=media)["retention"] is None


# --- settings ----------------------------------------------------------------------------------------------------

def test_retention_settings_are_validated():
    base = settings_form()
    assert settings.validate(base)[1] == {}
    assert "retention_time" in settings.validate({**base, "retention_time": "25:00"})[1]
    assert "retention_time" in settings.validate({**base, "retention_time": "3:45"})[1]
    assert "retention_max_per_run" in settings.validate({**base, "retention_max_per_run": "0"})[1]
    changes, _ = settings.validate({**base, "retention_enabled": "1", "retention_time": "04:10"})
    assert changes["retention_enabled"] == 1 and changes["retention_time"] == "04:10"
