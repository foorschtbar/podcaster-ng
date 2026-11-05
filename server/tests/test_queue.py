import sqlite3
import threading
from datetime import timedelta

import pytest

from app import episodes, queue
from app.db import connect

from qhelpers import (NOW, add_download, add_podcast, add_worker, attempts, claim, defaults, make_db, minutes, task)


@pytest.fixture
def conn(tmp_path):
    c = connect(make_db(tmp_path))
    yield c
    c.close()


def path_of(conn):
    return conn.execute("PRAGMA database_list").fetchone()[2]


# --- scan creation / scheduling ---------------------------------------------------------------------

def test_create_scan_once(conn):
    add_podcast(conn)
    first = queue.create_scan(conn, "abgt", NOW)
    assert first and task(conn, first)["state"] == "pending" and task(conn, first)["type"] == "scan"
    assert queue.create_scan(conn, "abgt", NOW) is None          # already open
    with pytest.raises(queue.TaskNotFound):
        queue.create_scan(conn, "nope", NOW)


def test_schedule_creates_scan_for_new_podcast_and_respects_interval(conn):
    add_podcast(conn)
    assert len(queue.schedule_scans(conn, 60, NOW)) == 1
    assert queue.schedule_scans(conn, 60, NOW + minutes(10)) == []      # a scan is open
    w = add_worker(conn)
    t = claim(conn, w, now=NOW + minutes(1)).task
    queue.complete_scan(conn, t["id"], w["id"], [], NOW + minutes(2))
    assert queue.schedule_scans(conn, 60, NOW + minutes(61)) == []      # 60 min after the scan finished at +2
    assert len(queue.schedule_scans(conn, 60, NOW + minutes(62))) == 1


def test_schedule_uses_podcast_interval_and_skips_inactive(conn):
    add_podcast(conn, "a", scan_interval_min=10)
    add_podcast(conn, "b")
    add_podcast(conn, "c", active=0)
    created = queue.schedule_scans(conn, 360, NOW)
    assert {task(conn, i)["podcast_id"] for i in created} == {"a", "b"}   # the paused podcast is not scanned
    w = add_worker(conn)
    for _ in range(2):
        t = claim(conn, w, now=NOW + minutes(1)).task
        queue.complete_scan(conn, t["id"], w["id"], [], NOW + minutes(1))
    due = queue.schedule_scans(conn, 360, NOW + minutes(12))
    assert [task(conn, i)["podcast_id"] for i in due] == ["a"]            # only the 10-minute podcast is due again


def test_scan_depth_default_and_one_open_scan_is_enforced_by_the_database(conn):
    add_podcast(conn)
    queue.create_scan(conn, "abgt", NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tasks (type, podcast_id, created_at, updated_at) VALUES ('scan', 'abgt', 'x', 'x')")


# --- claiming ------------------------------------------------------------------------------------------

def test_claim_sets_owner_lease_and_attempt(conn):
    add_podcast(conn)
    w = add_worker(conn)
    tid = queue.create_scan(conn, "abgt", NOW)
    result = claim(conn, w, lease=300)
    assert result.task["id"] == tid and result.attempt == 1
    row = task(conn, tid)
    assert (row["state"], row["claimed_by"], row["lease_until"]) == ("claimed", w["id"], queue.ts(NOW + minutes(5)))
    (a,) = attempts(conn, tid)
    assert (a["worker_id"], a["worker_name"], a["started_at"], a["result"], a["ended_at"]) == (w["id"], "w1", queue.ts(NOW), None, None)


def test_claim_returns_hint_when_nothing_to_do(conn):
    w = add_worker(conn)
    r = claim(conn, w)
    assert r.task is None and r.retry_after == queue.IDLE_HINT


def test_scans_before_downloads_oldest_first(conn):
    add_podcast(conn)
    w = add_worker(conn, downloads_per_hour=100, pause_min_s=0)
    d1 = add_download(conn, "abgt", "v1")
    d2 = add_download(conn, "abgt", "v2")
    scan = queue.create_scan(conn, "abgt", NOW)
    order = []
    for _ in range(3):
        t = claim(conn, w, now=NOW).task
        order.append(t["id"])
        if t["type"] == "scan":
            queue.complete_scan(conn, t["id"], w["id"], [], NOW)
        else:
            queue.close(conn, t["id"], NOW)
    assert order == [scan, d1, d2]


def test_one_task_at_a_time_per_worker(conn):
    add_podcast(conn)
    w = add_worker(conn)
    add_download(conn, "abgt", "v1")
    add_download(conn, "abgt", "v2")
    first = claim(conn, w).task
    assert first and claim(conn, w).task is None            # still holds the first
    queue.close(conn, first["id"], NOW)
    assert claim(conn, w, now=NOW + minutes(5)).task["video_id"] == "v2"


def test_types_filter(conn):
    add_podcast(conn)
    w = add_worker(conn)
    add_download(conn, "abgt", "v1")
    assert claim(conn, w, types=("scan",)).task is None
    queue.create_scan(conn, "abgt", NOW)
    assert claim(conn, w, types=("download",)).task["type"] == "download"


def test_not_before_and_inactive_podcast(conn):
    add_podcast(conn)
    add_podcast(conn, "paused", active=0)
    w = add_worker(conn, pause_min_s=0)
    add_download(conn, "abgt", "later", not_before=queue.ts(NOW + minutes(30)))
    add_download(conn, "paused", "p1")
    assert claim(conn, w, now=NOW).task is None                     # premiere not yet, podcast paused
    assert claim(conn, w, now=NOW + minutes(31)).task["video_id"] == "later"


def test_claimed_and_done_tasks_are_not_offered_again(conn):
    add_podcast(conn)
    w1, w2 = add_worker(conn, "w1"), add_worker(conn, "w2")
    queue.create_scan(conn, "abgt", NOW)
    assert claim(conn, w1).task and claim(conn, w2).task is None


# --- concurrency: two workers never hold the same task -------------------------------------------------

def run_threads(target, n):
    barrier = threading.Barrier(n)
    results, errors = [None] * n, []

    def wrapper(i):
        try:
            barrier.wait()
            results[i] = target(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    return results


def test_many_workers_racing_for_one_task_exactly_one_wins(conn):
    add_podcast(conn)
    n = 12
    workers_ = [add_worker(conn, f"w{i}") for i in range(n)]
    queue.create_scan(conn, "abgt", NOW)
    db = path_of(conn)

    def target(i):
        c = connect(db)
        try:
            return claim(c, workers_[i]).task
        finally:
            c.close()

    winners = [r for r in run_threads(target, n) if r]
    assert len(winners) == 1
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


def test_no_task_is_ever_claimed_twice_under_load(conn):
    add_podcast(conn)
    for i in range(30):
        add_download(conn, "abgt", f"v{i}")
    n = 8
    workers_ = [add_worker(conn, f"w{i}", downloads_per_hour=1000, pause_min_s=0) for i in range(n)]
    claimed = [[] for _ in range(n)]
    db = path_of(conn)

    def target(i):
        c = connect(db)
        try:
            for _ in range(12):
                got = claim(c, workers_[i], now=NOW).task        # one shared clock: no artificial lease expiry
                if got:
                    claimed[i].append(got["id"])
                    queue.close(c, got["id"], NOW)               # holding a task blocks further claims: finish it
        finally:
            c.close()

    run_threads(target, n)
    all_ids = [t for per in claimed for t in per]
    assert len(all_ids) == len(set(all_ids)) == 30           # every task exactly once, none lost
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 30


def test_a_worker_never_holds_two_tasks_even_when_asking_concurrently(conn):
    add_podcast(conn)
    for i in range(5):
        add_download(conn, "abgt", f"v{i}")
    w = add_worker(conn, downloads_per_hour=1000, pause_min_s=0)
    db = path_of(conn)

    def target(i):
        c = connect(db)
        try:
            return claim(c, w).task
        finally:
            c.close()

    got = [r for r in run_threads(target, 6) if r]
    assert len(got) == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE state = 'claimed'").fetchone()[0] == 1


# --- lease expiry ---------------------------------------------------------------------------------------

def test_expired_lease_returns_task_and_records_attempt(conn):
    add_podcast(conn)
    w1, w2 = add_worker(conn, "w1"), add_worker(conn, "w2")
    tid = queue.create_scan(conn, "abgt", NOW)
    claim(conn, w1, lease=300)
    assert queue.expire_leases(conn, NOW + minutes(4)) == 0                         # not yet
    assert task(conn, tid)["state"] == "claimed"
    assert queue.expire_leases(conn, NOW + minutes(6)) == 1
    row = task(conn, tid)
    assert (row["state"], row["claimed_by"], row["lease_until"], row["failures"]) == ("pending", None, None, 1)
    (a,) = attempts(conn, tid)
    assert a["result"] == "lease_expired" and a["ended_at"] == queue.ts(NOW + minutes(6)) and a["error_class"] == "timeout"
    # another worker can claim it, and the first one has lost it
    again = claim(conn, w2, now=NOW + minutes(7))
    assert again.task["id"] == tid and again.attempt == 2
    with pytest.raises(queue.NotOwner):
        queue.heartbeat(conn, tid, w1["id"], 300, NOW + minutes(8))
    with pytest.raises(queue.NotOwner):
        queue.complete_scan(conn, tid, w1["id"], [], NOW + minutes(8))
    with pytest.raises(queue.NotOwner):
        queue.fail(conn, tid, w1["id"], "internal", "late", now=NOW + minutes(8))


def test_claim_itself_expires_stale_leases(conn):
    add_podcast(conn)
    w1, w2 = add_worker(conn, "w1"), add_worker(conn, "w2")
    tid = queue.create_scan(conn, "abgt", NOW)
    claim(conn, w1, lease=60)
    got = claim(conn, w2, now=NOW + minutes(2))            # no scheduler tick in between
    assert got.task["id"] == tid and got.task["claimed_by"] == w2["id"]


def test_expiry_only_touches_expired_tasks(conn):
    add_podcast(conn)
    w1, w2 = add_worker(conn, "w1", pause_min_s=0), add_worker(conn, "w2", pause_min_s=0)
    add_download(conn, "abgt", "a")
    add_download(conn, "abgt", "b")
    t1 = claim(conn, w1, lease=60).task
    t2 = claim(conn, w2, lease=3600).task
    assert queue.expire_leases(conn, NOW + minutes(5)) == 1
    assert task(conn, t1["id"])["state"] == "pending" and task(conn, t2["id"])["state"] == "claimed"


# --- heartbeat ---------------------------------------------------------------------------------------------

def test_heartbeat_extends_lease(conn):
    add_podcast(conn)
    w = add_worker(conn)
    tid = queue.create_scan(conn, "abgt", NOW)
    claim(conn, w, lease=300)
    until = queue.heartbeat(conn, tid, w["id"], 300, NOW + minutes(4))
    assert until == queue.ts(NOW + minutes(9)) and task(conn, tid)["lease_until"] == until
    assert queue.expire_leases(conn, NOW + minutes(8)) == 0           # the extension kept it alive


def test_heartbeat_errors(conn):
    add_podcast(conn)
    w1, w2 = add_worker(conn, "w1"), add_worker(conn, "w2")
    tid = queue.create_scan(conn, "abgt", NOW)
    with pytest.raises(queue.NotOwner):
        queue.heartbeat(conn, tid, w1["id"], 300, NOW)                # pending
    claim(conn, w1)
    with pytest.raises(queue.NotOwner):
        queue.heartbeat(conn, tid, w2["id"], 300, NOW)                # someone else's
    with pytest.raises(queue.TaskNotFound):
        queue.heartbeat(conn, 999, w1["id"], 300, NOW)


# --- scan results ----------------------------------------------------------------------------------------------

def vid(i, **kw):
    return {"id": f"v{i}", "title": f"Video {i}", "upload_date": None, "live_status": None, "release_timestamp": None, **kw}


def run_scan(conn, videos, now=NOW, **podcast):
    add_podcast(conn, **podcast)
    w = add_worker(conn)
    tid = queue.create_scan(conn, "abgt", now)
    claim(conn, w, now=now)
    return tid, w, queue.complete_scan(conn, tid, w["id"], videos, now)


def downloads(conn):
    return [(r["video_id"], r["state"], r["not_before"]) for r in
            conn.execute("SELECT * FROM tasks WHERE type = 'download' ORDER BY id")]


def test_scan_creates_download_tasks_for_unknown_videos_only(conn):
    add_podcast(conn)
    for i, state in ((1, "available"), (2, "hidden"), (3, "deleted")):
        episodes.insert(conn, podcast_id="abgt", video_id=f"v{i}", title="x", audio_file=None if state == "deleted" else "a.m4a", state=state)
    add_download(conn, "abgt", "v4")                                  # an open download task exists
    add_download(conn, "abgt", "v5", state="closed")                  # a closed one too: not queued again
    w = add_worker(conn)
    tid = queue.create_scan(conn, "abgt", NOW)
    claim(conn, w)
    result = queue.complete_scan(conn, tid, w["id"], [vid(i) for i in range(1, 9)], NOW)
    assert result == {"created": 3, "known": 5, "skipped": 0}
    assert [d[0] for d in downloads(conn)] == ["v4", "v5", "v6", "v7", "v8"]
    assert all(d[1] == "pending" for d in downloads(conn) if d[0] in ("v6", "v7", "v8"))
    row = task(conn, tid)
    assert row["state"] == "done" and row["finished_at"] == queue.ts(NOW) and row["claimed_by"] is None
    (a,) = attempts(conn, tid)
    assert a["result"] == "ok" and "3 new, 5 known" in a["note"]


def test_scan_depth_limits_the_considered_videos(conn):
    tid, w, result = run_scan(conn, [vid(i) for i in range(10)], scan_depth=4)
    assert result == {"created": 4, "known": 0, "skipped": 6}
    assert [d[0] for d in downloads(conn)] == ["v0", "v1", "v2", "v3"]   # the first ones, in playlist order


def test_repeated_scan_creates_nothing_new(conn):
    add_podcast(conn)
    w = add_worker(conn)
    videos = [vid(i) for i in range(5)]
    created = []
    for n in range(3):
        tid = queue.create_scan(conn, "abgt", NOW + minutes(n))
        claim(conn, w, now=NOW + minutes(n))
        created.append(queue.complete_scan(conn, tid, w["id"], videos, NOW + minutes(n))["created"])
    assert created == [5, 0, 0] and len(downloads(conn)) == 5


def test_duplicate_ids_in_one_result_and_premieres(conn):
    release = int((NOW + timedelta(hours=2)).timestamp())
    videos = [vid(1), vid(1), vid(2, live_status="is_upcoming", release_timestamp=release),
              vid(3, live_status="is_live"), vid(4, live_status="not_live")]
    _, _, result = run_scan(conn, videos)
    assert result["created"] == 4
    rows = {d[0]: d for d in downloads(conn)}
    assert rows["v1"][2] is None and rows["v4"][2] is None
    assert rows["v2"][2] == queue.ts(NOW + timedelta(hours=2, minutes=30))       # announced start + margin
    assert rows["v3"][2] == queue.ts(NOW + minutes(30))                          # live now: wait a bit


def test_premiere_task_waits_and_is_not_claimed_early(conn):
    release = int((NOW + timedelta(hours=1)).timestamp())
    _, _, _ = run_scan(conn, [vid(1, live_status="is_upcoming", release_timestamp=release)])
    w = add_worker(conn, "dl", pause_min_s=0)
    assert claim(conn, w, now=NOW + minutes(30)).task is None
    assert claim(conn, w, now=NOW + timedelta(hours=2)).task["video_id"] == "v1"


def test_scan_result_only_by_the_owner_and_only_once(conn):
    add_podcast(conn)
    w1, w2 = add_worker(conn, "w1"), add_worker(conn, "w2")
    tid = queue.create_scan(conn, "abgt", NOW)
    with pytest.raises(queue.NotOwner):
        queue.complete_scan(conn, tid, w1["id"], [], NOW)
    claim(conn, w1)
    with pytest.raises(queue.NotOwner):
        queue.complete_scan(conn, tid, w2["id"], [vid(1)], NOW)
    queue.complete_scan(conn, tid, w1["id"], [vid(1)], NOW)
    with pytest.raises(queue.NotOwner):
        queue.complete_scan(conn, tid, w1["id"], [vid(2)], NOW)
    assert [d[0] for d in downloads(conn)] == ["v1"]


def test_scan_result_for_a_download_task_is_rejected(conn):
    add_podcast(conn)
    w = add_worker(conn)
    add_download(conn, "abgt", "v1")
    t = claim(conn, w).task
    with pytest.raises(queue.BadState):
        queue.complete_scan(conn, t["id"], w["id"], [vid(2)], NOW)
    assert downloads(conn) == [("v1", "claimed", None)]


def test_scan_result_is_atomic(conn, monkeypatch):
    add_podcast(conn)
    w = add_worker(conn)
    tid = queue.create_scan(conn, "abgt", NOW)
    claim(conn, w)
    videos = [vid(1), vid(2)]
    real = queue.clean
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(queue, "clean", boom)
    with pytest.raises(RuntimeError):
        queue.complete_scan(conn, tid, w["id"], videos, NOW)
    assert downloads(conn) == [] and task(conn, tid)["state"] == "claimed"     # nothing half-written


# --- failures --------------------------------------------------------------------------------------------------

def claimed(conn, **worker_kw):
    add_podcast(conn)
    w = add_worker(conn, **worker_kw)
    tid = queue.create_scan(conn, "abgt", NOW)
    claim(conn, w)
    return tid, w


def test_retryable_failure_backs_off_and_grows(conn):
    tid, w = claimed(conn)
    t = NOW
    waits = []
    for n in range(1, 7):
        state, not_before = queue.fail(conn, tid, w["id"], "bot_check", "Sign in to confirm you're not a bot", now=t)
        assert state == "pending"
        waits.append(int((queue.parse(not_before) - t).total_seconds()))
        assert task(conn, tid)["failures"] == n
        t = queue.parse(not_before)
        assert claim(conn, w, now=t - timedelta(seconds=1)).task is None       # not due yet
        assert claim(conn, w, now=t).task["id"] == tid
    assert waits == [60, 120, 240, 480, 960, 1800]                              # doubling, capped at 30 min
    attempts_ = attempts(conn, tid)
    assert [a["result"] for a in attempts_[:-1]] == ["failed"] * 6 and attempts_[-1]["result"] is None
    assert attempts_[0]["error_class"] == "bot_check" and "not a bot" in attempts_[0]["error_text"]


@pytest.mark.parametrize("error_class", ["network", "timeout", "internal", "incomplete", "bot_check"])
def test_all_retryable_classes_go_back_to_pending(conn, error_class):
    tid, w = claimed(conn)
    state, _ = queue.fail(conn, tid, w["id"], error_class, "x", now=NOW)
    assert state == "pending" and task(conn, tid)["claimed_by"] is None


@pytest.mark.parametrize("error_class", ["private", "removed"])
def test_private_and_removed_close_the_task(conn, error_class):
    add_podcast(conn)
    w = add_worker(conn)
    add_download(conn, "abgt", "v1")
    t = claim(conn, w).task
    state, not_before = queue.fail(conn, t["id"], w["id"], error_class, "This video is private", now=NOW)
    assert (state, not_before) == ("closed", None)
    row = task(conn, t["id"])
    assert row["state"] == "closed" and row["finished_at"] == queue.ts(NOW)
    assert claim(conn, w, now=NOW + timedelta(days=1)).task is None              # no escalation, no retry
    assert attempts(conn, t["id"])[0]["error_class"] == error_class


def test_not_yet_available_defers_without_counting_as_failure(conn):
    add_podcast(conn)
    w = add_worker(conn, pause_min_s=0)
    add_download(conn, "abgt", "v1")
    t = claim(conn, w).task
    state, not_before = queue.fail(conn, t["id"], w["id"], "not_yet_available", "Premieres in 2 hours", retry_after=7200, now=NOW)
    assert state == "pending" and not_before == queue.ts(NOW + timedelta(hours=2))
    assert task(conn, t["id"])["failures"] == 0
    assert attempts(conn, t["id"])[0]["result"] == "deferred"
    assert claim(conn, w, now=NOW + minutes(30)).task is None
    assert claim(conn, w, now=NOW + timedelta(hours=2)).task["id"] == t["id"]


def test_not_yet_available_default_and_bounds(conn):
    add_podcast(conn)
    w = add_worker(conn, pause_min_s=0)
    add_download(conn, "abgt", "v1")
    t = claim(conn, w).task
    _, nb = queue.fail(conn, t["id"], w["id"], "not_yet_available", "", now=NOW)
    assert nb == queue.ts(NOW + timedelta(seconds=queue.DEFAULT_NOT_YET_AVAILABLE))
    t = claim(conn, w, now=NOW + timedelta(hours=1)).task
    _, nb = queue.fail(conn, t["id"], w["id"], "not_yet_available", "", retry_after=5, now=NOW)
    assert nb == queue.ts(NOW + minutes(1))                                       # at least a minute


def test_not_yet_available_for_a_scan_counts_as_internal_error(conn):
    tid, w = claimed(conn)
    state, _ = queue.fail(conn, tid, w["id"], "not_yet_available", "weird", now=NOW)
    assert state == "pending" and task(conn, tid)["failures"] == 1
    assert attempts(conn, tid)[0]["error_class"] == "internal"


def test_error_text_is_scrubbed_and_trimmed(conn):
    tid, w = claimed(conn)
    secret = "pnw_" + "A" * 43
    text = f"failed with Authorization: Bearer {secret} and https://user:hunter2@proxy.example:3128/ cookie=abc123 " + "x" * 2000
    queue.fail(conn, tid, w["id"], "internal", text, now=NOW, extra_secrets=("telegram-secret",))
    stored = attempts(conn, tid)[0]["error_text"]
    assert secret not in stored and "hunter2" not in stored and "abc123" not in stored
    assert "***" in stored and len(stored) <= 500


def test_fail_only_by_the_owner(conn):
    tid, w = claimed(conn)
    other = add_worker(conn, "other")
    with pytest.raises(queue.NotOwner):
        queue.fail(conn, tid, other["id"], "internal", "x", now=NOW)
    with pytest.raises(queue.TaskNotFound):
        queue.fail(conn, 999, w["id"], "internal", "x", now=NOW)


# --- pacing ---------------------------------------------------------------------------------------------------------

def test_downloads_per_hour_limit_and_hint(conn):
    add_podcast(conn)
    w = add_worker(conn, downloads_per_hour=2, pause_min_s=0)
    for i in range(4):
        add_download(conn, "abgt", f"v{i}")
    t = NOW
    for n in range(2):
        got = claim(conn, w, now=t).task
        assert got
        queue.fail(conn, got["id"], w["id"], "private", "x", now=t)       # finishes quickly
        t += minutes(5)
    blocked = claim(conn, w, now=t)
    assert blocked.task is None
    assert blocked.retry_after == queue.MAX_HINT or queue.MIN_HINT <= blocked.retry_after <= queue.MAX_HINT
    # the hour slides: 3600 s after the first attempt a slot is free again
    assert claim(conn, w, now=NOW + timedelta(hours=1, seconds=1)).task is not None


def test_pause_between_downloads(conn):
    add_podcast(conn)
    w = add_worker(conn, pause_min_s=60, downloads_per_hour=100)
    add_download(conn, "abgt", "a")
    add_download(conn, "abgt", "b")
    first = claim(conn, w, now=NOW).task
    queue.fail(conn, first["id"], w["id"], "private", "x", now=NOW + timedelta(seconds=10))
    r = claim(conn, w, now=NOW + timedelta(seconds=30))
    assert r.task is None and 5 <= r.retry_after <= 61
    assert claim(conn, w, now=NOW + timedelta(seconds=71)).task["video_id"] == "b"


def test_worker_limit_caps_the_podcast_limit_and_vice_versa(conn):
    add_podcast(conn, "loose", downloads_per_hour=50)
    add_podcast(conn, "tight", downloads_per_hour=1)
    capped = add_worker(conn, "capped", downloads_per_hour=1, pause_min_s=0)
    free = add_worker(conn, "free", downloads_per_hour=100, pause_min_s=0)
    for i in range(3):
        add_download(conn, "loose", f"l{i}")
    for i in range(3):
        add_download(conn, "tight", f"t{i}")
    # worker cap 1/h beats the podcast's 50/h
    t = claim(conn, capped, now=NOW).task
    queue.fail(conn, t["id"], capped["id"], "private", "x", now=NOW)
    assert claim(conn, capped, now=NOW + minutes(1)).task is None
    # the podcast's 1/h beats the worker's 100/h, but only for that podcast
    got = []
    for n in range(4):
        r = claim(conn, free, now=NOW + minutes(n)).task
        if r:
            got.append(r["podcast_id"])
            queue.fail(conn, r["id"], free["id"], "private", "x", now=NOW + minutes(n))
    assert got.count("tight") == 1 and got.count("loose") >= 2


def test_pacing_uses_the_larger_pause_of_worker_and_podcast(conn):
    add_podcast(conn, pause_min_s=120)
    w = add_worker(conn, pause_min_s=10, downloads_per_hour=100)
    add_download(conn, "abgt", "a")
    add_download(conn, "abgt", "b")
    t = claim(conn, w, now=NOW).task
    queue.fail(conn, t["id"], w["id"], "private", "x", now=NOW)
    assert claim(conn, w, now=NOW + timedelta(seconds=100)).task is None
    assert claim(conn, w, now=NOW + timedelta(seconds=121)).task is not None


def test_deferred_and_released_attempts_do_not_use_up_the_quota(conn):
    add_podcast(conn)
    w = add_worker(conn, downloads_per_hour=1, pause_min_s=0)
    add_download(conn, "abgt", "a")
    t = claim(conn, w, now=NOW).task
    queue.fail(conn, t["id"], w["id"], "not_yet_available", "", retry_after=60, now=NOW)
    assert claim(conn, w, now=NOW + minutes(2)).task is not None              # the premiere wait cost nothing


def test_scans_are_not_paced(conn):
    add_podcast(conn, "a")
    add_podcast(conn, "b")
    w = add_worker(conn, downloads_per_hour=1, pause_min_s=3600)
    for pid in ("a", "b"):
        queue.create_scan(conn, pid, NOW)
    t = claim(conn, w).task
    queue.complete_scan(conn, t["id"], w["id"], [], NOW)
    assert claim(conn, w, now=NOW).task is not None


# --- manual actions ----------------------------------------------------------------------------------------------------

def test_release_retry_close(conn):
    tid, w = claimed(conn)
    queue.release(conn, tid, NOW + minutes(1))
    row = task(conn, tid)
    assert (row["state"], row["claimed_by"], row["failures"]) == ("pending", None, 0)
    a = attempts(conn, tid)[0]
    assert a["result"] == "released" and a["note"] == "released manually"
    with pytest.raises(queue.BadState):
        queue.release(conn, tid, NOW)                                 # not claimed any more
    queue.close(conn, tid, NOW + minutes(2))
    assert task(conn, tid)["state"] == "closed" and task(conn, tid)["finished_at"] is not None
    with pytest.raises(queue.BadState):
        queue.close(conn, tid, NOW)
    queue.retry(conn, tid, NOW + minutes(3))
    row = task(conn, tid)
    assert (row["state"], row["not_before"], row["finished_at"]) == ("pending", None, None)


def test_retry_clears_backoff(conn):
    tid, w = claimed(conn)
    queue.fail(conn, tid, w["id"], "network", "x", now=NOW)
    assert claim(conn, w, now=NOW + minutes(1) - timedelta(seconds=1)).task is None
    queue.retry(conn, tid, NOW)
    assert claim(conn, w, now=NOW).task["id"] == tid


def test_retry_of_a_closed_scan_conflicts_with_a_newer_open_one(conn):
    add_podcast(conn)
    first = queue.create_scan(conn, "abgt", NOW)
    queue.close(conn, first, NOW)
    queue.create_scan(conn, "abgt", NOW)
    with pytest.raises(queue.BadState, match="already open"):
        queue.retry(conn, first, NOW)
    assert task(conn, first)["state"] == "closed"


def test_cannot_retry_done_or_claimed(conn):
    tid, w = claimed(conn)
    with pytest.raises(queue.BadState):
        queue.retry(conn, tid, NOW)
    queue.complete_scan(conn, tid, w["id"], [], NOW)
    with pytest.raises(queue.BadState):
        queue.retry(conn, tid, NOW)
    for fn in (queue.release, queue.retry, queue.close):
        with pytest.raises(queue.TaskNotFound):
            fn(conn, 999)


def test_closing_a_claimed_task_frees_the_worker(conn):
    tid, w = claimed(conn)
    queue.close(conn, tid, NOW)
    assert attempts(conn, tid)[0]["result"] == "released"
    add_download(conn, "abgt", "v1")
    assert claim(conn, w, now=NOW + minutes(5)).task is not None


# --- reading / housekeeping ----------------------------------------------------------------------------------------------

def test_listing_filters_and_counts(conn):
    add_podcast(conn, "a")
    add_podcast(conn, "b")
    w = add_worker(conn)
    queue.create_scan(conn, "a", NOW)
    add_download(conn, "a", "v1")
    add_download(conn, "b", "v2", state="closed")
    add_download(conn, "b", "v3", state="paused")
    assert queue.count_tasks(conn) == 4
    assert [t["video_id"] for t in queue.list_tasks(conn, state="closed")] == ["v2"]
    assert {t["podcast_id"] for t in queue.list_tasks(conn, podcast_id="b")} == {"b"}
    assert [t["type"] for t in queue.list_tasks(conn, type_="scan")] == ["scan"]
    assert queue.count_tasks(conn, state="pending", podcast_id="a") == 2
    assert queue.list_tasks(conn, limit=2, offset=0)[0]["id"] > queue.list_tasks(conn, limit=2, offset=2)[0]["id"]
    c = queue.counts(conn)
    assert (c["pending"], c["closed"], c["paused"], c["open_downloads"]) == (2, 1, 1, 2)
    claim(conn, w, types=("scan",))
    row = queue.list_tasks(conn, state="claimed")[0]
    assert row["worker_name"] == "w1" and row["attempt_count"] == 1 and row["podcast_name"] == "A"


def test_recent_errors(conn):
    tid, w = claimed(conn)
    queue.fail(conn, tid, w["id"], "bot_check", "blocked", now=NOW)
    errs = queue.recent_errors(conn)
    assert len(errs) == 1 and errs[0]["error_class"] == "bot_check" and errs[0]["podcast_id"] == "abgt"


def test_prune_removes_only_old_finished_scans(conn):
    add_podcast(conn)
    w = add_worker(conn)
    old = queue.create_scan(conn, "abgt", NOW - timedelta(days=30))
    claim(conn, w, now=NOW - timedelta(days=30))
    queue.complete_scan(conn, old, w["id"], [vid(1)], NOW - timedelta(days=30))
    recent = queue.create_scan(conn, "abgt", NOW - timedelta(days=2))
    claim(conn, w, now=NOW - timedelta(days=2))
    queue.complete_scan(conn, recent, w["id"], [], NOW - timedelta(days=2))
    open_scan = queue.create_scan(conn, "abgt", NOW)
    assert queue.prune(conn, NOW) == 1
    ids = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE type = 'scan'")]
    assert old not in ids and recent in ids and open_scan in ids
    assert len(downloads(conn)) == 1                                       # download history stays
    assert conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id = ?", (old,)).fetchone()[0] == 0   # cascade


def test_deleting_a_podcast_removes_its_tasks_and_deleting_a_worker_keeps_attempts(conn):
    tid, w = claimed(conn)
    queue.fail(conn, tid, w["id"], "network", "x", now=NOW)
    from app import workers
    workers.delete(conn, w["id"])
    a = attempts(conn, tid)[0]
    assert a["worker_id"] is None and a["worker_name"] == "w1"           # history survives
    conn.execute("DELETE FROM podcasts WHERE id = 'abgt'")
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
