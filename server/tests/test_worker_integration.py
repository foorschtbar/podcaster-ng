"""A real worker against a real server (uvicorn in a thread)."""

import socket
import threading
import time
from datetime import datetime, timezone

import pytest
import uvicorn

from app import workers
from app.db import connect
from app.main import create_app
from app.migrations import migrate
from pn_worker import client as client_module
from pn_worker.config import Config
from pn_worker.main import run

from helpers import make_config


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, config, port):
        self.config, self.port = config, port
        self.server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise AssertionError("server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


class WorkerThread:
    def __init__(self, cfg):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=run, args=(cfg, self.stop_event), daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        self.thread.join(10)


def wait_for(predicate, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def env(tmp_path):
    config = make_config(tmp_path)
    migrate(config.db_path, config.backup_dir)
    conn = connect(config.db_path)
    worker_id, token = workers.create(conn, {"name": "w1", "tier": 1, "active": 1, "pause_min_s": None,
                                             "pause_max_s": None, "downloads_per_hour": None})
    conn.close()
    port = free_port()

    def cfg(**kw):
        base = dict(server_url=f"http://127.0.0.1:{port}", token=token, ping_interval=0.2, backoff_start=0.1,
                    backoff_max=0.4, alive_file=str(tmp_path / "alive"))
        return Config(**{**base, **kw})

    def row():
        c = connect(config.db_path)
        r = workers.get(c, worker_id)
        c.close()
        return r

    return config, port, worker_id, token, cfg, row


def test_worker_connects_appears_online_and_goes_offline(env):
    config, port, worker_id, token, cfg, row = env
    server = Server(config, port).start()
    worker = WorkerThread(cfg()).start()
    try:
        assert wait_for(lambda: row()["last_seen"] is not None), "worker never connected"
        r = row()
        assert r["version"] and workers.status(r, 120) == "online"
        # it keeps pinging: last_seen advances
        first = r["last_seen"]
        assert wait_for(lambda: row()["last_seen"] > first, timeout=5)
    finally:
        worker.stop()
    # stopped: after the threshold it counts as offline
    time.sleep(2.1)
    assert workers.status(row(), 2) == "offline"
    server.stop()


def test_worker_retries_while_server_is_down_and_reconnects(env):
    config, port, worker_id, token, cfg, row = env
    worker = WorkerThread(cfg()).start()          # the server does not exist yet
    try:
        time.sleep(0.8)
        assert row()["last_seen"] is None and worker.thread.is_alive()   # still trying, not crashed
        server = Server(config, port).start()
        assert wait_for(lambda: row()["last_seen"] is not None), "worker did not connect once the server came up"
        server.stop()                                                      # server goes away again
        seen = row()["last_seen"]
        time.sleep(0.8)
        assert worker.thread.is_alive() and row()["last_seen"] == seen
        server = Server(config, port).start()                              # and comes back
        assert wait_for(lambda: row()["last_seen"] > seen), "worker did not reconnect after the restart"
        server.stop()
    finally:
        worker.stop()


def test_wrong_token_is_rejected_and_worker_keeps_running(env, monkeypatch, caplog):
    config, port, worker_id, token, cfg, row = env
    monkeypatch.setattr("pn_worker.main.AUTH_RETRY_SECONDS", 0.2)
    server = Server(config, port).start()
    worker = WorkerThread(cfg(token="pnw_" + "x" * 43)).start()
    try:
        time.sleep(1.0)
        assert row()["last_seen"] is None and worker.thread.is_alive()
        assert "pnw_xxxx" not in caplog.text
    finally:
        worker.stop()
        server.stop()


def test_revoked_token_stops_the_worker_from_being_seen(env):
    config, port, worker_id, token, cfg, row = env
    server = Server(config, port).start()
    worker = WorkerThread(cfg()).start()
    try:
        assert wait_for(lambda: row()["last_seen"] is not None)
        conn = connect(config.db_path)
        workers.regenerate_token(conn, worker_id)          # revoke from the UI
        conn.close()
        time.sleep(0.3)
        seen = row()["last_seen"]
        time.sleep(1.0)
        assert row()["last_seen"] == seen and worker.thread.is_alive()
    finally:
        worker.stop()
        server.stop()
