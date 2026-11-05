"""The acceptance criteria of the escalation step, played through the worker API and the scheduler."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import queue, scheduler, settings, workers
from app.db import connect
from app.main import create_app

from helpers import make_config
from pkghelpers import manifest, package, upload_all
from qhelpers import add_download, add_podcast, attempts, task
from tghelpers import FakeTelegram, configure

BOT = "ERROR: [youtube] zPMoD167_14: Sign in to confirm you’re not a bot. Use --cookies-from-browser"
LONG_AGO = datetime.now(timezone.utc) - timedelta(days=1)


@pytest.fixture
def env(tmp_path):
    fake = FakeTelegram()
    config = make_config(tmp_path)
    with TestClient(create_app(config, telegram_transport=fake.transport), base_url="https://pod.example.org") as client:
        conn = connect(config.db_path)
        add_podcast(conn, pause_min_s=0, pause_max_s=0, downloads_per_hour=1000)
        configure(conn)
        conn.close()
        client.db, client.fake, client.config = config.db_path, fake, config
        yield client


def q(c, fn):
    conn = connect(c.db)
    try:
        return fn(conn)
    finally:
        conn.close()


def new_worker(c, name, tier):
    def make(conn):
        wid, token = workers.create(conn, {"name": name, "tier": tier, "active": 1, "pause_min_s": 0, "pause_max_s": 0,
                                           "downloads_per_hour": 1000})
        return wid, {"Authorization": f"Bearer {token}"}
    return q(c, make)[1]


def make_task(c, video, **kw):
    """A download task created 'now' (the API runs on the real clock)."""
    return q(c, lambda conn: add_download(conn, "abgt", video, now=datetime.now(timezone.utc), **kw))


def claim(c, h):
    return c.post("/api/worker/claim", json={"types": ["download"]}, headers=h).json()["task"]


def fail(c, h, tid, cls="bot_check", text=BOT):
    r = c.post(f"/api/worker/tasks/{tid}/fail", json={"error_class": cls, "message": text}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def time_passes(c, hours=1):
    """Let backoffs and cool-downs run out (the API uses the real clock)."""
    shift = f"-{hours} hours"
    q(c, lambda conn: conn.execute("UPDATE tasks SET not_before = NULL, tier_since = strftime('%Y-%m-%dT%H:%M:%SZ', datetime(COALESCE(tier_since, created_at), ?))", (shift,)))
    q(c, lambda conn: conn.execute("UPDATE attempts SET started_at = strftime('%Y-%m-%dT%H:%M:%SZ', datetime(started_at, ?)), "
                                   "ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', datetime(ended_at, ?)) WHERE ended_at IS NOT NULL", (shift, shift)))


def tick(c):
    return scheduler.tick(c.db, transport=c.fake.transport, started_at=LONG_AGO)


def episodes(c):
    return q(c, lambda conn: conn.execute("SELECT * FROM episodes").fetchall())


# --- 1. tier 1 fails with bot_check, tier 2 succeeds: the episode appears and nobody is alerted --------------------------------

def test_tier_one_blocked_tier_two_delivers_and_no_alert(env):
    local, home = new_worker(env, "local", 1), new_worker(env, "home", 2)
    tid = make_task(env, "aaaaaaaaaaa", title="Group Therapy 701")

    assert claim(env, home) is None                                     # tier 2 does not even see the task yet
    got = claim(env, local)
    assert got["id"] == tid
    assert fail(env, local, tid)["state"] == "pending"                 # the blocked IP: bot_check
    assert q(env, lambda c: task(c, tid))["tier"] == 2
    assert claim(env, local) is None                                    # tier 1 does not get it straight back

    time_passes(env, hours=0)                                           # the backoff is over
    q(env, lambda c: c.execute("UPDATE tasks SET not_before = NULL"))
    got = claim(env, home)
    assert got["id"] == tid and got["attempt"] == 2
    files = package(title="Group Therapy 701")
    upload_all(env, home, tid, files)
    assert env.post(f"/api/worker/tasks/{tid}/complete", json=manifest(files), headers=home).status_code == 200

    assert len(episodes(env)) == 1 and "Group Therapy 701" in env.get("/abgt.xml").text            # the episode appears in the feed
    assert q(env, lambda c: task(c, tid))["state"] == "done"
    tick(env)
    assert env.fake.calls == [] and q(env, lambda c: c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]) == 0   # no alert
    assert [e["kind"] for e in q(env, lambda c: queue.events_of(c, tid))] == ["escalated"]


# --- 2. both tiers fail: paused after N failures, exactly one message, release makes workers pick it up again --------------------

def test_both_tiers_fail_one_message_and_release(env):
    local, home = new_worker(env, "local", 1), new_worker(env, "home", 2)
    tid = make_task(env, "zPMoD167_14", title="Group Therapy 701 with Above & Beyond")
    for n, h in enumerate([local, home, local, home]):
        assert claim(env, h)["id"] == tid, n
        state = fail(env, h, tid)["state"]
        assert state == ("paused" if n == 3 else "pending")
        time_passes(env)
    row = q(env, lambda c: task(c, tid))
    assert row["state"] == "paused" and "4 failed attempts from 2 worker(s)" in row["pause_reason"]
    assert claim(env, local) is None and claim(env, home) is None       # nobody gets a paused task

    tick(env)
    assert len(env.fake.calls) == 1                                     # exactly one Telegram message ...
    text = env.fake.texts[0]
    assert "paused" in text and "zPMoD167_14" in text and "local ×2" in text and "home ×2" in text and "not a bot" in text
    for _ in range(3):
        tick(env)
    assert len(env.fake.calls) == 1                                     # ... however often the scheduler runs

    q(env, lambda c: queue.release(c, tid))                             # "release" in the UI
    assert q(env, lambda c: task(c, tid))["state"] == "pending"
    assert claim(env, local)["id"] == tid                               # workers pick it up again, from tier 1
    assert len(env.fake.calls) == 1


def test_a_failing_telegram_does_not_lose_or_duplicate_the_pause_message(env):
    calls = {"n": 0}

    def flaky(call):
        calls["n"] += 1
        import httpx
        return httpx.Response(502) if calls["n"] <= 2 else httpx.Response(200, json={"ok": True})

    env.fake.respond = flaky
    only = new_worker(env, "only", 1)
    tid = make_task(env, "aaaaaaaaaaa", title="x")
    for _ in range(4):
        claim(env, only)
        fail(env, only, tid)
        time_passes(env)
    assert q(env, lambda c: task(c, tid))["state"] == "paused"          # a single active worker is enough to alarm
    now = datetime.now(timezone.utc)
    for minutes in (0, 2, 5, 9):          # within the 15 minutes after which the worker watchdog would also speak up
        scheduler.tick(env.db, transport=env.fake.transport, now=now + timedelta(minutes=minutes), started_at=LONG_AGO)
    assert calls["n"] == 3                                              # two failures, one success, then silence
    assert q(env, lambda c: c.execute("SELECT status FROM notifications").fetchone()[0]) == "sent"


def test_a_paused_task_reopens_by_itself_and_can_pause_again(env):
    local, home = new_worker(env, "local", 1), new_worker(env, "home", 2)
    tid = make_task(env, "aaaaaaaaaaa", title="x")
    for h in (local, home, local, home):
        claim(env, h)
        fail(env, h, tid)
        time_passes(env)
    assert q(env, lambda c: task(c, tid))["state"] == "paused"
    later = datetime.now(timezone.utc) + timedelta(hours=7)
    scheduler.tick(env.db, transport=env.fake.transport, now=later, started_at=LONG_AGO)
    assert q(env, lambda c: task(c, tid))["state"] == "pending"
    assert q(env, lambda c: [e["kind"] for e in queue.events_of(c, tid)][::-1])[-1] == "reopened"


# --- 3. tier 1 offline: the task moves to tier 2 after the deadline -----------------------------------------------------------------

def test_with_tier_one_offline_the_task_moves_to_tier_two_after_the_deadline(env):
    new_worker(env, "local", 1)                                         # registered, but never polls: it is offline
    home = new_worker(env, "home", 2)
    q(env, lambda c: settings.save(c, {"escalation_deadline_min": 5}))
    tid = make_task(env, "aaaaaaaaaaa", title="x")
    assert claim(env, home) is None
    q(env, lambda c: c.execute("UPDATE tasks SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', datetime('now', '-4 minutes')), tier_since = NULL"))
    assert claim(env, home) is None                                     # four minutes: within the deadline
    q(env, lambda c: c.execute("UPDATE tasks SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', datetime('now', '-6 minutes')), tier_since = NULL"))
    tick(env)                                                           # the scheduler escalates ...
    assert q(env, lambda c: task(c, tid))["tier"] == 2
    assert claim(env, home)["id"] == tid                                # ... and tier 2 takes it
    assert "within 5 min" in q(env, lambda c: queue.events_of(c, tid)[0]["detail"])
    assert env.fake.calls == []                                         # moving up is not an alarm


# --- 4. a worker going offline and returning: one message each --------------------------------------------------------------------------

def test_worker_offline_and_back_one_message_each(env):
    home = new_worker(env, "home", 2)
    env.get("/api/worker/ping", headers=home)                           # it was seen
    assert tick(env)["alerts"] == {"offline": 0, "online": 0}
    q(env, lambda c: c.execute("UPDATE workers SET last_seen = strftime('%Y-%m-%dT%H:%M:%SZ', datetime('now', '-20 minutes'))"))
    assert tick(env)["alerts"]["offline"] == 1
    for _ in range(3):
        assert tick(env)["alerts"] == {"offline": 0, "online": 0}
    assert len(env.fake.calls) == 1 and "offline" in env.fake.texts[0] and "'home'" in env.fake.texts[0]
    env.get("/api/worker/ping", headers=home)                           # it returns
    assert tick(env)["alerts"]["online"] == 1
    for _ in range(3):
        tick(env)
    assert len(env.fake.calls) == 2 and "back online" in env.fake.texts[1]


# --- optional message for new episodes ---------------------------------------------------------------------------------------------------

def test_new_episode_message_when_enabled_in_the_settings(env):
    only = new_worker(env, "only", 1)
    q(env, lambda c: settings.save(c, {"telegram_new_episode": 1}))
    tid = make_task(env, "aaaaaaaaaaa", title="Episode")
    claim(env, only)
    files = package()
    upload_all(env, only, tid, files)
    env.post(f"/api/worker/tasks/{tid}/complete", json=manifest(files), headers=only)
    tick(env)
    assert len(env.fake.calls) == 1 and "New episode: ABGT" in env.fake.texts[0] and "Group Therapy 701" in env.fake.texts[0]
