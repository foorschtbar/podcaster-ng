import logging

import pytest
from fastapi.testclient import TestClient

from app import auth, settings, workers
from app.db import connect
from app.main import create_app
from pn_shared import API_VERSION

from helpers import make_config

DEFAULTS = {"pause_min_s": 5, "pause_max_s": 20}
HELLO = {"worker_version": "1.0.0", "ytdlp_version": "2026.10.1", "ffmpeg_version": "7.0", "deno_version": "2.1",
         "capabilities": ["ytdlp", "ffmpeg", "deno"]}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="https://pod.example.org") as c:
        c.db = c.app.state.config.db_path
        yield c


def add_worker(client, name="home", **over):
    conn = connect(client.db)
    data = {"name": name, "tier": 2, "active": 1, "pause_min_s": None, "pause_max_s": None, "downloads_per_hour": None}
    data.update(over)
    worker_id, token = workers.create(conn, data)
    conn.close()
    return worker_id, token


def row(client, worker_id):
    conn = connect(client.db)
    r = workers.get(conn, worker_id)
    conn.close()
    return r


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# --- hello ------------------------------------------------------------------------------

def test_hello_flow(client):
    worker_id, token = add_worker(client, pause_min_s=7, downloads_per_hour=2)
    r = client.post("/api/worker/hello", json=HELLO, headers=bearer(token))
    assert r.status_code == 200
    body = r.json()
    assert body["api_version"] == API_VERSION == 1
    assert body["worker"] == {"id": worker_id, "name": "home", "tier": 2}
    # effective settings: the worker's own value, otherwise the server default
    assert body["pacing"] == {"pause_min_s": 7, "pause_max_s": 20, "downloads_per_hour": 2}
    assert body["lease_seconds"] == 300 and body["ping_interval_seconds"] == 30
    assert body["server_time"].endswith("Z") and len(body["server_time"]) == 20
    stored = row(client, worker_id)
    assert stored["version"] == "1.0.0" and stored["last_seen"] is not None
    assert workers.capabilities_of(stored) == ["ytdlp", "ffmpeg", "deno"]
    assert workers.tools_of(stored) == {"yt-dlp": "2026.10.1", "ffmpeg": "7.0", "deno": "2.1"}


def test_hello_uses_changed_server_defaults(client):
    _, token = add_worker(client)
    conn = connect(client.db)
    settings.save(conn, {"downloads_per_hour": 9, "lease_seconds": 600})
    conn.close()
    body = client.post("/api/worker/hello", json={"worker_version": "1"}, headers=bearer(token)).json()
    assert body["pacing"]["downloads_per_hour"] == 9 and body["lease_seconds"] == 600


def test_hello_minimal_body_and_validation(client):
    _, token = add_worker(client)
    h = bearer(token)
    assert client.post("/api/worker/hello", json={"worker_version": "1"}, headers=h).status_code == 200
    for bad in ({}, {"worker_version": ""}, {"worker_version": "x" * 51}, {"worker_version": "1", "capabilities": "ytdlp"},
                {"worker_version": "1", "capabilities": ["x"] * 21}, {"worker_version": "1", "capabilities": ["y" * 41]},
                {"worker_version": 1.5}):
        assert client.post("/api/worker/hello", json=bad, headers=h).status_code == 422, bad
    assert client.post("/api/worker/hello", content=b"not json", headers=h).status_code == 422


# --- ping / last seen ----------------------------------------------------------------------

def test_ping_updates_last_seen(client):
    worker_id, token = add_worker(client)
    assert row(client, worker_id)["last_seen"] is None
    r = client.get("/api/worker/ping", headers=bearer(token))
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["server_time"].endswith("Z")
    first = row(client, worker_id)["last_seen"]
    assert first is not None
    conn = connect(client.db)
    conn.execute("UPDATE workers SET last_seen = '2000-01-01T00:00:00Z'")
    conn.close()
    client.get("/api/worker/ping", headers=bearer(token))
    assert row(client, worker_id)["last_seen"] >= first


def test_every_authenticated_call_updates_last_seen(client):
    worker_id, token = add_worker(client)
    conn = connect(client.db)
    conn.execute("UPDATE workers SET last_seen = '2000-01-01T00:00:00Z'")
    conn.close()
    client.post("/api/worker/hello", json={"worker_version": "1"}, headers=bearer(token))
    assert row(client, worker_id)["last_seen"] > "2020"


# --- authentication failures ------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer "}, {"Authorization": "Bearer nonsense"}, {"Authorization": "Basic abc"},
    {"Authorization": "pnw_xyz"}, {"Authorization": "Bearer pnw_" + "A" * 43},
])
def test_missing_or_unknown_token_is_401(client, headers):
    for method, path in (("GET", "/api/worker/ping"), ("POST", "/api/worker/hello")):
        r = client.request(method, path, headers=headers, json={"worker_version": "1"} if method == "POST" else None)
        assert r.status_code == 401, (headers, path)
        assert r.headers["www-authenticate"] == "Bearer"


def test_token_in_wrong_places_is_not_accepted(client):
    _, token = add_worker(client)
    assert client.get(f"/api/worker/ping?token={token}").status_code == 401
    assert client.get("/api/worker/ping", headers={"X-Token": token}).status_code == 401
    client.cookies.set("token", token)
    assert client.get("/api/worker/ping").status_code == 401


def test_revoked_token_is_rejected(client):
    worker_id, token = add_worker(client)
    assert client.get("/api/worker/ping", headers=bearer(token)).status_code == 200
    conn = connect(client.db)
    new = workers.regenerate_token(conn, worker_id)
    conn.close()
    assert client.get("/api/worker/ping", headers=bearer(token)).status_code == 401   # old token revoked
    assert client.get("/api/worker/ping", headers=bearer(new)).status_code == 200
    conn = connect(client.db)
    workers.delete(conn, worker_id)
    conn.close()
    assert client.get("/api/worker/ping", headers=bearer(new)).status_code == 401     # deleted worker


def test_inactive_worker_is_403_and_not_marked_seen(client):
    worker_id, token = add_worker(client, active=0)
    r = client.get("/api/worker/ping", headers=bearer(token))
    assert r.status_code == 403 and "deactivated" in r.json()["detail"]
    assert client.post("/api/worker/hello", json={"worker_version": "1"}, headers=bearer(token)).status_code == 403
    assert row(client, worker_id)["last_seen"] is None
    conn = connect(client.db)
    workers.set_active(conn, worker_id, True)
    conn.close()
    assert client.get("/api/worker/ping", headers=bearer(token)).status_code == 200


# --- rate limiting -------------------------------------------------------------------------------

def test_invalid_tokens_are_rate_limited_per_address(tmp_path):
    app = create_app(make_config(tmp_path))
    app.state.worker_auth_limiter = auth.LoginLimiter(max_failures=3, window=300, lockout=300, delay=0)
    with TestClient(app) as c:
        codes = [c.get("/api/worker/ping", headers=bearer("bad")).status_code for _ in range(5)]
        assert codes == [401, 401, 401, 429, 429]
        r = c.get("/api/worker/ping", headers=bearer("bad"))
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_valid_worker_is_not_locked_out_by_invalid_tokens_on_same_address(tmp_path):
    app = create_app(make_config(tmp_path))
    app.state.worker_auth_limiter = auth.LoginLimiter(max_failures=2, window=300, lockout=300, delay=0)
    with TestClient(app) as c:
        c.db = app.state.config.db_path
        _, token = add_worker(c)
        for _ in range(3):
            c.get("/api/worker/ping", headers=bearer("bad"))
        assert c.get("/api/worker/ping", headers=bearer("bad")).status_code == 429
        assert c.get("/api/worker/ping", headers=bearer(token)).status_code == 200   # a good worker still works


def test_valid_worker_is_rate_limited_per_worker(tmp_path):
    app = create_app(make_config(tmp_path))
    app.state.worker_rate_limiter = auth.RateLimiter(limit=5, window=60)
    with TestClient(app) as c:
        c.db = app.state.config.db_path
        _, token = add_worker(c)
        _, other = add_worker(c, name="other")
        codes = [c.get("/api/worker/ping", headers=bearer(token)).status_code for _ in range(7)]
        assert codes == [200] * 5 + [429] * 2
        assert c.get("/api/worker/ping", headers=bearer(other)).status_code == 200


# --- separation and secrecy ------------------------------------------------------------------------

def test_worker_api_is_separate_from_admin_and_public(client):
    _, token = add_worker(client)
    # a worker token does not open the admin UI or anything else
    assert client.get("/dashboard", headers=bearer(token), follow_redirects=False).status_code == 303
    assert client.get("/workers", headers=bearer(token), follow_redirects=False).status_code == 303
    # the API is not reachable with an admin session cookie either (no session auth on the API)
    assert client.get("/api/worker/ping").status_code == 401
    assert client.get("/api/worker/unknown", headers=bearer(token)).status_code == 404
    assert client.get("/api/worker/hello", headers=bearer(token)).status_code == 405


def test_api_responses_are_not_cached_and_have_security_headers(client):
    _, token = add_worker(client)
    r = client.get("/api/worker/ping", headers=bearer(token))
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_api_calls_are_not_counted_as_hits(client):
    _, token = add_worker(client)
    client.get("/api/worker/ping", headers=bearer(token))
    conn = connect(client.db)
    assert conn.execute("SELECT COUNT(*) FROM hits_daily").fetchone()[0] == 0
    conn.close()


def test_token_never_logged_or_stored(client, caplog):
    caplog.set_level(logging.DEBUG)
    worker_id, token = add_worker(client)
    client.post("/api/worker/hello", json=HELLO, headers=bearer(token))
    client.get("/api/worker/ping", headers=bearer(token))
    wrong = "pnw_wrong-token-value-1234567890"
    client.get("/api/worker/ping", headers=bearer(wrong))
    assert token not in caplog.text and wrong not in caplog.text
    conn = connect(client.db)
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert token not in dump and wrong not in dump
