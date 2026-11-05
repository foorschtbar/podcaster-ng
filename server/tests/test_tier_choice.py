"""Handing over to the next tier at once, choosing the tier of a retry, pausing a blocked worker, dropped tasks."""

from datetime import timedelta

import pytest

from app import notifications, queue, settings
from app.db import connect

from qhelpers import NOW, add_download, add_podcast, add_worker, attempts, claim, defaults, make_db, minutes, task


@pytest.fixture
def conn(tmp_path):
    c = connect(make_db(tmp_path))
    add_podcast(c, pause_min_s=0, pause_max_s=0, downloads_per_hour=1000)
    yield c
    c.close()


def policy(conn, **over):
    return queue.Policy.from_settings({**defaults(conn), **over})


def fail_bot(conn, w, t, now, **kw):
    return queue.fail(conn, t["id"], w["id"], "bot_check", "Sign in to confirm you're not a bot", now=now, policy=policy(conn), **kw)


# --- A: the hand-over to the next tier has no waiting time ----------------------------------------------------------

def test_a_failure_hands_the_task_to_the_next_tier_without_the_backoff(conn):
    local, home = add_worker(conn, "local", tier=1), add_worker(conn, "home", tier=2)
    add_download(conn, "abgt", "v1")
    t = claim(conn, local).task
    state, not_before = fail_bot(conn, local, t, NOW)
    assert state == "pending" and not_before is None
    row = task(conn, t["id"])
    assert row["tier"] == 2 and row["not_before"] is None
    got = claim(conn, home, now=NOW + timedelta(seconds=1)).task                    # the home worker gets it at once
    assert got and got["id"] == t["id"]


def test_without_a_higher_tier_the_backoff_still_applies(conn):
    local = add_worker(conn, "local", tier=1)
    add_download(conn, "abgt", "v1")
    t = claim(conn, local).task
    state, not_before = fail_bot(conn, local, t, NOW)
    assert state == "pending" and not_before is not None and task(conn, t["id"])["tier"] == 1


def test_network_errors_keep_the_backoff_and_stay_at_their_tier(conn):
    local, home = add_worker(conn, "local", tier=1), add_worker(conn, "home", tier=2)
    add_download(conn, "abgt", "v1")
    t = claim(conn, local).task
    _, not_before = queue.fail(conn, t["id"], local["id"], "network", "reset", now=NOW, policy=policy(conn))
    assert not_before is not None and task(conn, t["id"])["tier"] == 1


# --- B: retry with a chosen tier ------------------------------------------------------------------------------------

def make_paused(conn):
    local, home = add_worker(conn, "local", tier=1), add_worker(conn, "home", tier=2)
    add_download(conn, "abgt", "v1")
    t = claim(conn, local).task
    fail_bot(conn, local, t, NOW)
    return local, home, t["id"]


def test_the_suggestion_is_the_tier_above_the_one_that_got_the_bot_check(conn):
    local, home, tid = make_paused(conn)
    assert queue.suggested_tier(conn, tid, NOW + minutes(1)) == 2
    assert queue.suggested_tier(conn, tid, NOW + timedelta(hours=25)) == 1           # old news: try the cheap tier again


def test_no_suggestion_without_a_higher_active_tier_or_failures(conn):
    local = add_worker(conn, "local", tier=1)
    add_download(conn, "abgt", "v1")
    t = claim(conn, local).task
    assert queue.suggested_tier(conn, t["id"], NOW) == 1
    fail_bot(conn, local, t, NOW)
    assert queue.suggested_tier(conn, t["id"], NOW) == 1                              # nobody above is active


def test_retry_starts_at_the_suggested_tier_and_tier_1_does_not_see_it(conn):
    local, home, tid = make_paused(conn)
    conn.execute("UPDATE tasks SET tier = 1 WHERE id = ?", (tid,))                    # as after an automatic reopen
    assert queue.retry(conn, tid, NOW + minutes(1)) == 2
    row = task(conn, tid)
    assert (row["tier"], row["min_tier"], row["not_before"]) == (2, 2, None)
    assert claim(conn, local, now=NOW + minutes(2)).task is None                      # the blocked tier is skipped
    assert claim(conn, home, now=NOW + minutes(2)).task["id"] == tid


def test_retry_with_an_explicit_tier_and_back_to_tier_1(conn):
    local, home, tid = make_paused(conn)
    assert queue.retry(conn, tid, NOW + minutes(1), tier=1) == 1
    assert (task(conn, tid)["tier"], task(conn, tid)["min_tier"]) == (1, 1)
    assert claim(conn, local, now=NOW + minutes(2)).task["id"] == tid


def test_retry_refuses_a_tier_nobody_serves(conn):
    local, home, tid = make_paused(conn)
    with pytest.raises(queue.BadState, match="no active worker has tier 3"):
        queue.retry(conn, tid, NOW, tier=3)


def test_retry_of_a_paused_task_starts_at_the_chosen_tier_and_logs_it(conn):
    local, home, tid = make_paused(conn)
    conn.execute("UPDATE tasks SET state = 'paused', pause_reason = 'x' WHERE id = ?", (tid,))
    assert queue.retry(conn, tid, NOW + minutes(1), tier=2) == 2
    assert task(conn, tid)["state"] == "pending" and task(conn, tid)["min_tier"] == 2
    assert "starting at tier 2" in queue.events_of(conn, tid)[0]["detail"] or any("starting at tier 2" in e["detail"] for e in queue.events_of(conn, tid))


def test_automatic_reopen_and_release_start_at_tier_1_again(conn):
    local, home, tid = make_paused(conn)
    conn.execute("UPDATE tasks SET state = 'paused', tier = 2, min_tier = 2, reopen_at = ? WHERE id = ?", (queue.ts(NOW), tid))
    queue.reopen_due(conn, NOW + minutes(1))
    assert (task(conn, tid)["tier"], task(conn, tid)["min_tier"]) == (1, 1)


def test_scans_ignore_the_tier_choice(conn):
    local, home = add_worker(conn, "local", tier=1), add_worker(conn, "home", tier=2)
    sid = queue.create_scan(conn, "abgt", NOW)
    queue.close(conn, sid, NOW)
    assert queue.retry(conn, sid, NOW + minutes(1)) == 1


# --- C: a blocked worker pauses its downloads ---------------------------------------------------------------------

def blocked(conn, w):
    return conn.execute("SELECT download_blocked_until FROM workers WHERE id = ?", (w["id"],)).fetchone()[0]


def test_bot_checks_on_two_different_videos_pause_the_workers_downloads(conn):
    local, home = add_worker(conn, "local", tier=1), add_worker(conn, "home", tier=2)
    add_download(conn, "abgt", "v1")
    add_download(conn, "abgt", "v2")
    t1 = claim(conn, local, now=NOW).task
    fail_bot(conn, local, t1, NOW)
    assert blocked(conn, local) is None                                               # one video could just be that video
    t2 = claim(conn, local, now=NOW + minutes(1)).task
    assert t2["video_id"] == "v2"
    fail_bot(conn, local, t2, NOW + minutes(1))
    assert blocked(conn, local) == queue.ts(NOW + minutes(61))
    msgs = [m for m in notifications.recent(conn, 10) if m["kind"] == "worker_blocked"]
    assert len(msgs) == 1 and "local" in msgs[0]["text"] and "60 min" in msgs[0]["text"]
    # no downloads for it (scans still work), its pending tasks are at tier 2 for the home worker
    assert queue.create_scan(conn, "abgt", NOW + minutes(2))
    got = claim(conn, local, now=NOW + minutes(3)).task
    assert got["type"] == "scan"
    assert all(task(conn, i)["tier"] == 2 for i in (t1["id"], t2["id"]))
    assert claim(conn, home, now=NOW + minutes(3)).task["type"] in ("download", "scan")


def test_the_same_video_failing_repeatedly_does_not_block_the_worker(conn):
    local = add_worker(conn, "local", tier=1)
    add_download(conn, "abgt", "v1")
    for i in range(3):
        t = claim(conn, local, now=NOW + minutes(11 * i)).task
        fail_bot(conn, local, t, NOW + minutes(11 * i))
    assert blocked(conn, local) is None


def test_the_pause_ends(conn):
    local = add_worker(conn, "local", tier=1)
    add_download(conn, "abgt", "v1")
    conn.execute("UPDATE workers SET download_blocked_until = ? WHERE id = ?", (queue.ts(NOW + minutes(30)), local["id"]))
    assert claim(conn, local, now=NOW + minutes(10)).task is None
    assert claim(conn, local, now=NOW + minutes(31)).task["type"] == "download"


def test_the_pause_can_be_switched_off(conn):
    local = add_worker(conn, "local", tier=1)
    add_download(conn, "abgt", "v1")
    add_download(conn, "abgt", "v2")
    off = policy(conn, download_block_minutes=0)
    for i in range(2):
        t = claim(conn, local, now=NOW + minutes(11 * i)).task
        queue.fail(conn, t["id"], local["id"], "bot_check", "x", now=NOW + minutes(11 * i), policy=off)
    assert blocked(conn, local) is None


def test_a_blocked_tier_hands_tasks_up_even_when_they_arrive_later(conn):
    local, home = add_worker(conn, "local", tier=1), add_worker(conn, "home", tier=2)
    conn.execute("UPDATE workers SET download_blocked_until = ? WHERE id = ?", (queue.ts(NOW + minutes(30)), local["id"]))
    tid = add_download(conn, "abgt", "v9")
    queue.escalate_overdue(conn, NOW + minutes(1))
    assert task(conn, tid)["tier"] == 2
    assert claim(conn, home, now=NOW + minutes(2)).task["id"] == tid


def test_without_a_higher_tier_nothing_moves(conn):
    local = add_worker(conn, "local", tier=1)
    conn.execute("UPDATE workers SET download_blocked_until = ? WHERE id = ?", (queue.ts(NOW + minutes(30)), local["id"]))
    tid = add_download(conn, "abgt", "v9")
    queue.escalate_overdue(conn, NOW + minutes(1))
    assert task(conn, tid)["tier"] == 1


# --- dropped tasks --------------------------------------------------------------------------------------------------

def test_a_task_the_worker_dropped_is_taken_back_with_its_next_call(conn):
    w = add_worker(conn, "home", tier=2)
    tid = add_download(conn, "abgt", "v1", tier=2)
    first = claim(conn, w).task
    assert first["id"] == tid and claim(conn, w, now=NOW + minutes(1)).task is None  # still holds it
    again = queue.claim(conn, w, ["scan", "download"], defaults(conn), 300, NOW + minutes(1), dropped=[tid])
    assert again.task["id"] == tid
    a = attempts(conn, tid)
    assert [x["result"] for x in a] == ["released", None] and "dropped this task" in a[0]["note"]
    assert task(conn, tid)["failures"] == 0


def test_dropped_ids_of_other_workers_or_finished_tasks_are_ignored(conn):
    a, b = add_worker(conn, "a", tier=1), add_worker(conn, "b", tier=1)
    add_download(conn, "abgt", "v1")
    t = claim(conn, a).task
    queue.claim(conn, b, ["download"], defaults(conn), 300, NOW + minutes(1), dropped=[t["id"], 999])
    assert task(conn, t["id"])["claimed_by"] == a["id"]                               # b cannot drop a's task
