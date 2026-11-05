import pytest
from fastapi.testclient import TestClient

from app import queue, workers
from app.db import connect
from app.main import create_app

from helpers import make_config
from qhelpers import NOW, add_download, add_podcast, attempts, task

LONG_AGO = "2000-01-01T00:00:00Z"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_config(tmp_path))) as c:
        c.db = c.app.state.config.db_path
        conn = connect(c.db)
        add_podcast(conn)
        conn.close()
        yield c


def worker(client, name="w1", **over):
    conn = connect(client.db)
    data = {"name": name, "tier": 1, "active": 1, "pause_min_s": None, "pause_max_s": None, "downloads_per_hour": None, **over}
    wid, token = workers.create(conn, data)
    conn.close()
    return wid, {"Authorization": f"Bearer {token}"}


def q(client, fn):
    conn = connect(client.db)
    try:
        return fn(conn)
    finally:
        conn.close()


def scan_task(client):
    return q(client, lambda c: queue.create_scan(c, "abgt"))


V = [{"id": f"vvvvvvvvv{i:02d}", "title": f"Video {i}"} for i in range(5)]


# --- claim -------------------------------------------------------------------------------------------------

def test_claim_returns_the_task_with_everything_a_worker_needs(client):
    wid, h = worker(client, pause_min_s=7)
    tid = scan_task(client)
    r = client.post("/api/worker/claim", json={"types": ["scan"]}, headers=h)
    assert r.status_code == 200
    t = r.json()["task"]
    assert t["id"] == tid and t["type"] == "scan" and t["attempt"] == 1 and t["lease_seconds"] == 300
    assert t["podcast"] == {"id": "abgt", "name": "ABGT", "playlist_url": "https://www.youtube.com/playlist?list=abgt", "scan_depth": 20}
    assert t["pacing"]["pause_min_s"] == 7 and t["lease_until"].endswith("Z")
    assert q(client, lambda c: task(c, tid))["claimed_by"] == wid


def test_claim_without_work_gives_a_retry_hint(client):
    _, h = worker(client)
    r = client.post("/api/worker/claim", json={}, headers=h)
    assert r.status_code == 200 and r.json() == {"task": None, "retry_after_seconds": 15}


def test_claim_default_types_and_validation(client):
    _, h = worker(client)
    add = q(client, lambda c: add_download(c, "abgt", "dl1"))
    assert client.post("/api/worker/claim", json={}, headers=h).json()["task"] is None      # default: scan only
    for bad in ({"types": []}, {"types": ["upload"]}, {"types": "scan"}, {"types": ["scan", "download", "scan"]}):
        assert client.post("/api/worker/claim", json=bad, headers=h).status_code == 422, bad
    assert client.post("/api/worker/claim", json={"types": ["download"]}, headers=h).json()["task"]["id"] == add


def test_only_one_of_two_workers_gets_the_task(client):
    _, h1 = worker(client, "w1")
    _, h2 = worker(client, "w2")
    scan_task(client)
    got = [client.post("/api/worker/claim", json={"types": ["scan"]}, headers=h).json()["task"] for h in (h1, h2)]
    assert sum(1 for t in got if t) == 1


def test_claim_requires_a_valid_active_worker(client):
    assert client.post("/api/worker/claim", json={}).status_code == 401
    assert client.post("/api/worker/claim", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    wid, h = worker(client, active=0)
    assert client.post("/api/worker/claim", json={}, headers=h).status_code == 403


def test_claim_updates_last_seen(client):
    wid, h = worker(client)
    client.post("/api/worker/claim", json={}, headers=h)
    assert q(client, lambda c: workers.get(c, wid))["last_seen"] is not None


def test_download_pacing_is_applied_through_the_api(client):
    _, h = worker(client, downloads_per_hour=1, pause_min_s=0)
    q(client, lambda c: [add_download(c, "abgt", f"d{i}") for i in range(3)])
    first = client.post("/api/worker/claim", json={"types": ["download"]}, headers=h).json()["task"]
    assert first and first["pacing"]["downloads_per_hour"] == 1
    client.post(f"/api/worker/tasks/{first['id']}/fail", json={"error_class": "private", "message": "x"}, headers=h)
    r = client.post("/api/worker/claim", json={"types": ["download"]}, headers=h).json()
    assert r["task"] is None and 5 <= r["retry_after_seconds"] <= 300


# --- heartbeat ----------------------------------------------------------------------------------------------------

def test_heartbeat_extends_the_lease(client):
    _, h = worker(client)
    tid = scan_task(client)
    client.post("/api/worker/claim", json={}, headers=h)
    q(client, lambda c: c.execute("UPDATE tasks SET lease_until = '2000-01-01T00:00:00Z'"))
    r = client.post(f"/api/worker/tasks/{tid}/heartbeat", json={"progress": "halfway"}, headers=h)
    assert r.status_code == 200 and r.json()["lease_until"] > "2020"
    assert q(client, lambda c: task(c, tid))["lease_until"] == r.json()["lease_until"]
    assert client.post(f"/api/worker/tasks/{tid}/heartbeat", json={}, headers=h).status_code == 200
    assert client.post(f"/api/worker/tasks/{tid}/heartbeat", json={"progress": "x" * 201}, headers=h).status_code == 422


def test_heartbeat_conflicts_and_unknown_task(client):
    _, h1 = worker(client, "w1")
    _, h2 = worker(client, "w2")
    tid = scan_task(client)
    assert client.post(f"/api/worker/tasks/{tid}/heartbeat", json={}, headers=h1).status_code == 409   # not claimed
    client.post("/api/worker/claim", json={}, headers=h1)
    assert client.post(f"/api/worker/tasks/{tid}/heartbeat", json={}, headers=h2).status_code == 409   # not yours
    assert client.post("/api/worker/tasks/9999/heartbeat", json={}, headers=h1).status_code == 404
    assert client.post("/api/worker/tasks/abc/heartbeat", json={}, headers=h1).status_code == 422


# --- scan result ----------------------------------------------------------------------------------------------------

def test_scan_result_creates_download_tasks_for_new_videos_only(client):
    _, h = worker(client)
    q(client, lambda c: c.execute("INSERT INTO episodes (podcast_id, video_id, title, state, created_at, updated_at) "
                                  "VALUES ('abgt', 'vvvvvvvvv00', 'old', 'deleted', 'x', 'x')"))
    tid = scan_task(client)
    client.post("/api/worker/claim", json={}, headers=h)
    r = client.post(f"/api/worker/tasks/{tid}/scan-result", json={"videos": V}, headers=h)
    assert r.status_code == 200 and r.json() == {"created": 4, "known": 1, "skipped": 0}
    rows = q(client, lambda c: [(t["video_id"], t["state"], t["title"]) for t in queue.list_tasks(c, type_="download")])
    assert sorted(rows) == [(f"vvvvvvvvv{i:02d}", "pending", f"Video {i}") for i in range(1, 5)]
    assert q(client, lambda c: task(c, tid))["state"] == "done"
    # a second submission is refused, nothing is created twice
    assert client.post(f"/api/worker/tasks/{tid}/scan-result", json={"videos": V}, headers=h).status_code == 409


@pytest.mark.parametrize("body", [
    {"videos": [{"id": ""}]}, {"videos": [{"id": "has space"}]}, {"videos": [{"id": "x" * 65}]}, {"videos": [{"title": "no id"}]},
    {"videos": [{"id": "ok", "title": "x" * 501}]}, {"videos": [{"id": "ok"}] * 1001}, {"videos": "none"},
])
def test_invalid_scan_results_are_rejected_without_side_effects(client, body):
    _, h = worker(client)
    tid = scan_task(client)
    client.post("/api/worker/claim", json={}, headers=h)
    assert client.post(f"/api/worker/tasks/{tid}/scan-result", json=body, headers=h).status_code == 422
    assert q(client, lambda c: task(c, tid))["state"] == "claimed" and q(client, lambda c: queue.count_tasks(c, type_="download")) == 0


def test_scan_result_from_the_wrong_worker_or_for_the_wrong_task(client):
    _, h1 = worker(client, "w1")
    _, h2 = worker(client, "w2")
    scan = scan_task(client)
    dl = q(client, lambda c: add_download(c, "abgt", "dl1", state="pending"))
    client.post("/api/worker/claim", json={}, headers=h1)
    assert client.post(f"/api/worker/tasks/{scan}/scan-result", json={"videos": V}, headers=h2).status_code == 409
    got = client.post("/api/worker/claim", json={"types": ["download"]}, headers=h2).json()["task"]
    assert got["id"] == dl
    assert client.post(f"/api/worker/tasks/{dl}/scan-result", json={"videos": V}, headers=h2).status_code == 409
    assert client.post("/api/worker/tasks/9999/scan-result", json={"videos": V}, headers=h1).status_code == 404


# --- fail ---------------------------------------------------------------------------------------------------------------------

def test_fail_records_the_attempt_and_schedules_a_retry(client):
    _, h = worker(client)
    tid = scan_task(client)
    client.post("/api/worker/claim", json={}, headers=h)
    r = client.post(f"/api/worker/tasks/{tid}/fail", json={"error_class": "bot_check", "message": "Sign in to confirm you're not a bot"}, headers=h)
    assert r.status_code == 200 and r.json()["state"] == "pending" and r.json()["not_before"].endswith("Z")
    a = q(client, lambda c: attempts(c, tid))[0]
    assert (a["result"], a["error_class"], a["worker_name"]) == ("failed", "bot_check", "w1") and "not a bot" in a["error_text"]
    # backoff: the task is not offered straight away
    assert client.post("/api/worker/claim", json={}, headers=h).json()["task"] is None


def test_fail_scrubs_secrets_including_the_configured_telegram_token(client):
    _, h = worker(client)
    q(client, lambda c: c.execute("INSERT INTO settings (key, value) VALUES ('telegram_token', '123456:ABCdefGhIJKlmNoPQRsTUVwxyz1')"))
    tid = scan_task(client)
    client.post("/api/worker/claim", json={}, headers=h)
    text = "proxy http://user:hunter2@vpn:3128 Authorization: Bearer pnw_AAAAAAAAAAAAAAAAAA token=123456:ABCdefGhIJKlmNoPQRsTUVwxyz1 " + "z" * 3000
    client.post(f"/api/worker/tasks/{tid}/fail", json={"error_class": "internal", "message": text[:4000]}, headers=h)
    stored = q(client, lambda c: attempts(c, tid))[0]["error_text"]
    for secret in ("hunter2", "pnw_AAAAAAAAAAAAAAAAAA", "ABCdefGhIJKlmNoPQRsTUVwxyz1"):
        assert secret not in stored
    assert len(stored) <= 500


def test_fail_validation_and_ownership(client):
    _, h1 = worker(client, "w1")
    _, h2 = worker(client, "w2")
    tid = scan_task(client)
    client.post("/api/worker/claim", json={}, headers=h1)
    for bad in ({"error_class": "exploded"}, {"message": "x"}, {"error_class": "internal", "message": "x" * 4001},
                {"error_class": "internal", "retry_after_seconds": -1}, {"error_class": "internal", "retry_after_seconds": 10**9}):
        assert client.post(f"/api/worker/tasks/{tid}/fail", json=bad, headers=h1).status_code == 422, bad
    assert client.post(f"/api/worker/tasks/{tid}/fail", json={"error_class": "internal"}, headers=h2).status_code == 409
    assert client.post("/api/worker/tasks/9999/fail", json={"error_class": "internal"}, headers=h1).status_code == 404
    assert q(client, lambda c: task(c, tid))["state"] == "claimed"


def test_not_yet_available_and_private_via_the_api(client):
    _, h = worker(client, pause_min_s=0)
    dl = q(client, lambda c: add_download(c, "abgt", "dl1"))
    client.post("/api/worker/claim", json={"types": ["download"]}, headers=h)
    r = client.post(f"/api/worker/tasks/{dl}/fail", json={"error_class": "not_yet_available", "message": "Premieres in 1 hour", "retry_after_seconds": 3600}, headers=h)
    assert r.json()["state"] == "pending"
    assert q(client, lambda c: task(c, dl))["failures"] == 0
    q(client, lambda c: c.execute("UPDATE tasks SET not_before = NULL"))
    client.post("/api/worker/claim", json={"types": ["download"]}, headers=h)
    r = client.post(f"/api/worker/tasks/{dl}/fail", json={"error_class": "private", "message": "private"}, headers=h)
    assert r.json() == {"state": "closed", "not_before": None}


# --- lease expiry end to end ---------------------------------------------------------------------------------------------

def test_a_dead_worker_loses_the_task_and_another_one_gets_it(client):
    _, dead = worker(client, "dead")
    _, alive = worker(client, "alive")
    tid = scan_task(client)
    assert client.post("/api/worker/claim", json={}, headers=dead).json()["task"]["id"] == tid
    assert client.post("/api/worker/claim", json={}, headers=alive).json()["task"] is None      # held by "dead"
    q(client, lambda c: c.execute("UPDATE tasks SET lease_until = ?", (LONG_AGO,)))               # time passes, no heartbeat
    got = client.post("/api/worker/claim", json={}, headers=alive).json()["task"]
    assert got["id"] == tid and got["attempt"] == 2
    results = [a["result"] for a in q(client, lambda c: attempts(c, tid))]
    assert results == ["lease_expired", None]
    # the dead worker comes back and finds out it lost the task
    assert client.post(f"/api/worker/tasks/{tid}/heartbeat", json={}, headers=dead).status_code == 409
    assert client.post(f"/api/worker/tasks/{tid}/scan-result", json={"videos": V}, headers=dead).status_code == 409
