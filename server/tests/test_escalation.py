from datetime import timedelta

import pytest

from app import notifications, queue
from app.db import connect

from qhelpers import NOW, add_download, add_podcast, add_worker, attempts, claim, make_db, minutes, task

POLICY = queue.Policy()  # N = 4, reopen after 6 h, escalation deadline 10 min


@pytest.fixture
def conn(tmp_path):
    c = connect(make_db(tmp_path))
    add_podcast(c, pause_min_s=0, pause_max_s=0, downloads_per_hour=1000)
    yield c
    c.close()


def worker(conn, name, tier=1):
    return add_worker(conn, name, tier=tier, pause_min_s=0, downloads_per_hour=1000)


def new_task(conn, video="v1", **kw):
    return add_download(conn, "abgt", video, **kw)


def take(conn, w, now=NOW):
    got = claim(conn, w, now=now).task
    return got["id"] if got else None


def fail(conn, tid, w, cls="bot_check", now=NOW, text="boom"):
    return queue.fail(conn, tid, w["id"], cls, text, now=now, policy=POLICY)


def events(conn, tid):
    return [(e["kind"], e["detail"]) for e in queue.events_of(conn, tid)][::-1]


def messages(conn):
    return [r["kind"] for r in conn.execute("SELECT kind FROM notifications ORDER BY id")]


# --- tier visibility ------------------------------------------------------------------------------------

def test_a_new_task_is_visible_to_tier_one_only(conn):
    t1, t2 = worker(conn, "local", 1), worker(conn, "home", 2)
    tid = new_task(conn)
    assert take(conn, t2) is None                          # tier 2 does not see it
    assert take(conn, t1) == tid


def test_lower_tiers_keep_seeing_escalated_tasks(conn):
    t1, t2 = worker(conn, "local", 1), worker(conn, "home", 2)
    tid = new_task(conn)
    conn.execute("UPDATE tasks SET tier = 2")
    assert take(conn, t1) == tid                            # tier 1 may still take it, it is not exclusive to tier 2


def test_tier_two_sees_a_task_once_it_is_escalated(conn):
    t1, t2 = worker(conn, "local", 1), worker(conn, "home", 2)
    tid = new_task(conn)
    assert take(conn, t1) == tid
    fail(conn, tid, t1)                                    # bot_check on tier 1
    assert task(conn, tid)["tier"] == 2
    assert take(conn, t2, NOW + minutes(2)) == tid


def test_workers_of_the_same_tier_compete(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    tid = new_task(conn)
    assert (take(conn, a), take(conn, b)) == (tid, None)


# --- what escalates, what does not ----------------------------------------------------------------------------

@pytest.mark.parametrize("cls", ["bot_check", "internal", "incomplete"])
def test_these_failures_escalate_at_once(conn, cls):
    t1, t2 = worker(conn, "local", 1), worker(conn, "home", 2)
    tid = new_task(conn)
    take(conn, t1)
    fail(conn, tid, t1, cls)
    assert task(conn, tid)["tier"] == 2 and events(conn, tid)[0][0] == "escalated" and cls in events(conn, tid)[0][1]


@pytest.mark.parametrize("cls", ["network", "timeout"])
def test_network_problems_are_retried_on_the_same_tier(conn, cls):
    t1, t2 = worker(conn, "local", 1), worker(conn, "home", 2)
    tid = new_task(conn)
    take(conn, t1)
    fail(conn, tid, t1, cls)
    assert task(conn, tid)["tier"] == 1 and events(conn, tid) == []


@pytest.mark.parametrize("cls", ["private", "removed", "not_yet_available"])
def test_these_classes_never_escalate(conn, cls):
    t1, t2 = worker(conn, "local", 1), worker(conn, "home", 2)
    tid = new_task(conn)
    take(conn, t1)
    fail(conn, tid, t1, cls)
    assert task(conn, tid)["tier"] == 1 and events(conn, tid) == [] and messages(conn) == []


def test_a_failure_of_a_lower_tier_does_not_escalate_an_escalated_task(conn):
    t1, t2, t3 = worker(conn, "a", 1), worker(conn, "b", 2), worker(conn, "c", 3)
    tid = new_task(conn)
    conn.execute("UPDATE tasks SET tier = 2")              # already escalated to tier 2
    take(conn, t1)
    fail(conn, tid, t1)                                    # tier 1 fails it again: that is not "the current tier"
    assert task(conn, tid)["tier"] == 2
    take(conn, t2, NOW + minutes(2))
    fail(conn, tid, t2, now=NOW + minutes(2))              # tier 2 fails: now tier 3
    assert task(conn, tid)["tier"] == 3


def test_tiers_without_workers_are_skipped_and_the_top_tier_stays(conn):
    t1, t5 = worker(conn, "a", 1), worker(conn, "b", 5)
    tid = new_task(conn)
    take(conn, t1)
    fail(conn, tid, t1)
    assert task(conn, tid)["tier"] == 5                    # no tier 2..4 workers
    take(conn, t5, NOW + minutes(2))
    fail(conn, tid, t5, now=NOW + minutes(2))
    assert task(conn, tid)["tier"] == 5                    # nowhere left to go
    assert queue.escalate_overdue(conn, NOW + timedelta(days=1), POLICY) == 0


def test_inactive_workers_do_not_count_as_a_tier(conn):
    t1 = worker(conn, "a", 1)
    worker(conn, "b", 2)
    conn.execute("UPDATE workers SET active = 0 WHERE tier = 2")
    tid = new_task(conn)
    take(conn, t1)
    fail(conn, tid, t1)
    assert task(conn, tid)["tier"] == 1


# --- the deadline --------------------------------------------------------------------------------------------------

def test_an_unclaimed_task_moves_up_after_the_deadline(conn):
    worker(conn, "local", 1)
    t2 = worker(conn, "home", 2)
    tid = new_task(conn)
    assert queue.escalate_overdue(conn, NOW + minutes(9), POLICY) == 0
    assert take(conn, t2, NOW + minutes(9)) is None
    assert queue.escalate_overdue(conn, NOW + minutes(11), POLICY) == 1
    assert task(conn, tid)["tier"] == 2
    assert take(conn, t2, NOW + minutes(11)) == tid
    assert "no tier 1 worker took it within 10 min" in events(conn, tid)[0][1]


def test_claim_applies_the_deadline_itself_without_a_scheduler(conn):
    worker(conn, "local", 1)
    t2 = worker(conn, "home", 2)
    tid = new_task(conn)
    assert take(conn, t2, NOW + minutes(11)) == tid         # the claim of the tier 2 worker did the escalation


def test_the_deadline_counts_from_when_the_task_is_due(conn):
    worker(conn, "local", 1)
    t2 = worker(conn, "home", 2)
    tid = new_task(conn, not_before=queue.ts(NOW + timedelta(hours=2)))     # a premiere
    assert take(conn, t2, NOW + timedelta(hours=1)) is None
    assert take(conn, t2, NOW + timedelta(hours=2, minutes=5)) is None      # due, but the tier 1 worker has had no time yet
    assert take(conn, t2, NOW + timedelta(hours=2, minutes=11)) == tid


def test_the_deadline_counts_from_the_last_time_the_task_was_available(conn):
    t1 = worker(conn, "local", 1)
    t2 = worker(conn, "home", 2)
    tid = new_task(conn, created_at=queue.ts(NOW - timedelta(days=1)))      # old task ...
    assert take(conn, t1, NOW) == tid
    queue.expire_leases(conn, NOW + minutes(30), POLICY)                    # ... whose lease ran out just now
    assert queue.escalate_overdue(conn, NOW + minutes(31), POLICY) == 0     # it only became available again a minute ago
    assert take(conn, t2, NOW + minutes(41)) == tid                         # ten minutes later it moves on


def test_overdue_escalation_repeats_tier_by_tier(conn):
    worker(conn, "a", 1)
    worker(conn, "b", 2)
    worker(conn, "c", 3)
    tid = new_task(conn)
    queue.escalate_overdue(conn, NOW + minutes(11), POLICY)
    assert task(conn, tid)["tier"] == 2
    assert queue.escalate_overdue(conn, NOW + minutes(15), POLICY) == 0
    queue.escalate_overdue(conn, NOW + minutes(22), POLICY)
    assert task(conn, tid)["tier"] == 3


def test_claimed_and_done_tasks_are_not_escalated(conn):
    t1 = worker(conn, "a", 1)
    worker(conn, "b", 2)
    tid = new_task(conn)
    take(conn, t1)
    assert queue.escalate_overdue(conn, NOW + timedelta(hours=1), POLICY) == 0 and task(conn, tid)["tier"] == 1


# --- cool-down -----------------------------------------------------------------------------------------------------

def test_a_worker_does_not_get_back_a_task_it_just_failed(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    tid = new_task(conn)
    take(conn, a)
    fail(conn, tid, a, "network")                                  # same tier, retried with backoff
    assert take(conn, a, NOW + minutes(2)) is None                 # backoff over, but a failed it a minute ago and b could do it
    assert take(conn, b, NOW + minutes(2)) == tid
    queue.fail(conn, tid, b["id"], "network", "x", now=NOW + minutes(3), policy=POLICY)
    assert take(conn, a, NOW + minutes(20)) == tid                 # ten minutes later it may try again


def test_a_lone_worker_retries_after_the_backoff_only(conn):
    a = worker(conn, "a")
    tid = new_task(conn)
    take(conn, a)
    fail(conn, tid, a, "network")
    assert take(conn, a, NOW + minutes(2)) == tid                 # nobody else could take it: no cool-down


def test_an_expired_lease_is_not_held_against_the_worker(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    tid = new_task(conn)
    take(conn, a)
    queue.expire_leases(conn, NOW + minutes(10), POLICY)
    assert take(conn, a, NOW + minutes(10)) == tid


def test_a_manual_retry_resets_the_cool_down(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    tid = new_task(conn)
    take(conn, a)
    fail(conn, tid, a, "network")
    queue.retry(conn, tid, NOW + minutes(1))
    assert take(conn, a, NOW + minutes(1)) == tid


# --- failure accounting and pausing ------------------------------------------------------------------------------------

def run_failures(conn, plan, start=NOW):
    """plan: [(worker, class)]; every worker takes the task and fails it, with the time moving on."""
    t = start
    tid = new_task(conn)
    results = []
    for w, cls in plan:
        t += minutes(45)                                        # beyond every backoff and cool-down
        assert take(conn, w, t) == tid, (w["name"], cls)
        results.append(fail(conn, tid, w, cls, now=t)[0])
    return tid, results


def test_four_bot_checks_from_two_workers_pause_the_task(conn):
    a, b = worker(conn, "local", 1), worker(conn, "home", 2)
    tid, results = run_failures(conn, [(a, "bot_check"), (b, "bot_check"), (a, "bot_check"), (b, "bot_check")])
    assert results == ["pending", "pending", "pending", "paused"]
    row = task(conn, tid)
    assert row["state"] == "paused" and row["pause_count"] == 1 and row["claimed_by"] is None and row["not_before"] is None
    assert "4 failed attempts from 2 worker(s)" in row["pause_reason"]
    assert row["reopen_at"] == queue.ts(queue.parse(attempts(conn, tid)[-1]["ended_at"]) + timedelta(hours=6))
    assert events(conn, tid)[-1][0] == "paused"
    assert messages(conn) == ["task_paused"]


def test_the_pause_message_names_the_task_the_workers_and_the_errors(conn):
    a, b = worker(conn, "local", 1), worker(conn, "home", 2)
    q = queue.Policy(base_url="https://pod.example.org/")
    conn.execute("UPDATE tasks SET title = 'x'")
    tid, _ = run_failures(conn, [(a, "bot_check"), (b, "internal"), (a, "bot_check"), (b, "incomplete")])
    text = conn.execute("SELECT text FROM notifications").fetchone()[0]
    assert f"Task #{tid} paused: ABGT" in text and "Video: v1" in text and "from 2 worker(s)" in text
    assert "local ×2" in text and "home ×2" in text and "(bot_check)" in text and "(incomplete)" in text and "boom" in text
    assert "reopens by itself at" in text


def test_the_pause_message_links_to_the_task(conn):
    a, b = worker(conn, "local", 1), worker(conn, "home", 2)
    tid = new_task(conn)
    t = NOW
    for w in (a, b, a, b):
        t += minutes(45)
        take(conn, w, t)
        queue.fail(conn, tid, w["id"], "bot_check", "x", now=t, policy=queue.Policy(base_url="https://pod.example.org/"))
    assert f"https://pod.example.org/tasks/{tid}" in conn.execute("SELECT text FROM notifications").fetchone()[0]


def test_four_failures_from_one_worker_do_not_pause_while_another_is_active(conn):
    a, b = worker(conn, "local", 1), worker(conn, "home", 2)
    tid, results = run_failures(conn, [(a, "bot_check")] * 4)
    assert results == ["pending"] * 4 and task(conn, tid)["state"] == "pending" and messages(conn) == []


def test_a_single_active_worker_can_pause_a_task_on_its_own(conn):
    a = worker(conn, "local", 1)
    tid, results = run_failures(conn, [(a, "bot_check")] * 4)
    assert results[-1] == "paused" and messages(conn) == ["task_paused"]


def test_network_failures_count_a_quarter_each(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    plan = [(a, "network"), (b, "network")] * 7                        # 14 weak failures = 3.5
    tid, results = run_failures(conn, plan)
    assert "paused" not in results
    t = NOW + minutes(45 * 15)
    take(conn, a, t)
    assert fail(conn, tid, a, "timeout", now=t)[0] == "pending"        # 15 = 3.75
    t += minutes(45)
    take(conn, b, t)
    assert fail(conn, tid, b, "network", now=t)[0] == "paused"         # 16 = 4.0


def test_weak_and_full_failures_add_up(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    tid, results = run_failures(conn, [(a, "bot_check"), (b, "bot_check"), (a, "bot_check")] + [(b, "network")] * 3)
    assert "paused" not in results                                      # 3 + 0.75
    t = NOW + minutes(45 * 7)
    take(conn, a, t)
    assert fail(conn, tid, a, "network", now=t)[0] == "paused"          # 4.0


def test_classes_that_never_count(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    tid = new_task(conn)
    t = NOW
    for _ in range(6):
        t += minutes(45)
        take(conn, a, t)
        assert fail(conn, tid, a, "not_yet_available", now=t)[0] == "pending"
    assert task(conn, tid)["failures"] == 0 and messages(conn) == []
    queue.close(conn, tid, t)                                                  # so that the next claim gets the new task
    for cls in ("private", "removed"):
        other = new_task(conn, f"x{cls}")
        t += minutes(45)
        take(conn, a, t)
        assert fail(conn, other, a, cls, now=t)[0] == "closed"
    assert messages(conn) == []


def test_lease_expiries_count_weakly_and_can_pause(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    tid = new_task(conn)
    t = NOW
    for n in range(16):
        t += minutes(45)
        take(conn, [a, b][n % 2], t)
        queue.expire_leases(conn, t + minutes(10), POLICY)
    assert task(conn, tid)["state"] == "paused" and messages(conn) == ["task_paused"]
    assert all(x["result"] == "lease_expired" for x in attempts(conn, tid))


def test_the_threshold_comes_from_the_settings(conn):
    a, b = worker(conn, "a"), worker(conn, "b")
    tid = new_task(conn)
    take(conn, a)
    assert queue.fail(conn, tid, a["id"], "bot_check", "x", now=NOW, policy=queue.Policy(failure_threshold=1))[0] == "pending"   # one worker, but b is active
    take(conn, b, NOW + minutes(1))
    assert queue.fail(conn, tid, b["id"], "bot_check", "x", now=NOW + minutes(1), policy=queue.Policy(failure_threshold=1))[0] == "paused"


def test_a_paused_task_is_not_offered_and_not_escalated(conn):
    a, b = worker(conn, "a", 1), worker(conn, "b", 2)
    tid, _ = run_failures(conn, [(a, "bot_check"), (b, "bot_check"), (a, "bot_check"), (b, "bot_check")])
    late = NOW + timedelta(days=2)
    assert take(conn, a, late - timedelta(days=1, hours=18)) is None
    assert queue.escalate_overdue(conn, late, POLICY) == 0
    assert task(conn, tid)["state"] == "paused"


# --- reopening --------------------------------------------------------------------------------------------------------------

def paused_task(conn):
    a, b = worker(conn, "local", 1), worker(conn, "home", 2)
    tid, _ = run_failures(conn, [(a, "bot_check"), (b, "bot_check"), (a, "bot_check"), (b, "bot_check")])
    return tid, a, b, queue.parse(task(conn, tid)["reopen_at"])


def test_a_paused_task_reopens_by_itself_after_the_timer(conn):
    tid, a, b, reopen_at = paused_task(conn)
    assert queue.reopen_due(conn, reopen_at - timedelta(seconds=1)) == 0
    assert queue.reopen_due(conn, reopen_at) == 1
    row = task(conn, tid)
    assert (row["state"], row["tier"], row["failures"], row["pause_reason"], row["reopen_at"], row["not_before"]) == ("pending", 1, 0, None, None, None)
    assert events(conn, tid)[-1] == ("reopened", "the reopen timer ran out")
    assert take(conn, a, reopen_at) == tid                      # workers pick it up again, starting at tier 1


def test_after_reopening_the_failure_count_starts_again(conn):
    tid, a, b, reopen_at = paused_task(conn)
    queue.reopen_due(conn, reopen_at)
    t = reopen_at
    for w in (a, b, a):
        t += minutes(45)
        take(conn, w, t)
        assert fail(conn, tid, w, "bot_check", now=t)[0] == "pending"        # three more: the old four do not count
    t += minutes(45)
    take(conn, b, t)
    assert fail(conn, tid, b, "bot_check", now=t)[0] == "paused"             # the fourth since reopening
    assert task(conn, tid)["pause_count"] == 2
    assert messages(conn) == ["task_paused", "task_paused"]                  # a new pause is a new condition: a new message


def test_release_reopens_a_paused_task(conn):
    tid, a, b, _ = paused_task(conn)
    queue.release(conn, tid, NOW + timedelta(days=1))
    assert task(conn, tid)["state"] == "pending" and events(conn, tid)[-1] == ("reopened", "released manually")
    assert take(conn, a, NOW + timedelta(days=1)) == tid


def test_retry_reopens_a_paused_task_and_close_closes_it(conn):
    tid, a, b, _ = paused_task(conn)
    queue.retry(conn, tid, NOW + timedelta(days=1))
    assert task(conn, tid)["state"] == "pending" and task(conn, tid)["tier"] == 1
    c2 = new_task(conn, "v2")
    conn.execute("UPDATE tasks SET state = 'paused' WHERE id = ?", (c2,))
    queue.close(conn, c2, NOW + timedelta(days=1))
    assert task(conn, c2)["state"] == "closed"


def test_release_of_a_pending_task_is_still_refused(conn):
    tid = new_task(conn)
    with pytest.raises(queue.BadState, match="claimed or paused"):
        queue.release(conn, tid, NOW)


# --- the outbox is part of the same transaction ----------------------------------------------------------------------------------

def test_one_message_per_pause_even_if_assessed_repeatedly(conn):
    tid, a, b, _ = paused_task(conn)
    with queue.transaction(conn):
        assert queue._assess(conn, tid, NOW, POLICY) is False          # a paused task is not assessed again
    assert messages(conn) == ["task_paused"]
    assert notifications.enqueue(conn, "task_paused", f"task_paused:{tid}:1", "again") is False


def test_new_episode_message_only_when_enabled(conn):
    a = worker(conn, "a")
    tid = new_task(conn, title="Episode 1")
    take(conn, a)
    fields = {"title": "Episode 1", "description": "", "author": "", "audio_file": "e1.m4a", "thumb_file": None, "square_file": None,
              "chapters_file": None, "info_file": "e1.info.json", "size_bytes": 5, "duration_s": 60, "published_at": "2026-01-01"}
    queue.complete_download(conn, tid, a["id"], fields, NOW)                   # default policy: no message
    assert messages(conn) == []
    tid2 = new_task(conn, "v2", title="Episode 2")
    take(conn, a, NOW + minutes(1))
    queue.complete_download(conn, tid2, a["id"], {**fields, "title": "Episode 2", "audio_file": "e2.m4a"}, NOW + minutes(1),
                            queue.Policy(notify_new_episode=True, base_url="https://pod.example.org"))
    text = conn.execute("SELECT text FROM notifications").fetchone()[0]
    assert messages(conn) == ["episode_new"] and "New episode: ABGT – Episode 2" in text and "https://pod.example.org/abgt.xml" in text
