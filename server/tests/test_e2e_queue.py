"""Scheduler + server + real worker, with a fake yt-dlp: the acceptance criteria of the queue step."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import episodes, podcasts, queue, workers
from app.db import connect
from app.migrations import migrate
from pn_worker.config import Config
from pn_worker.main import run

from fake_ytdlp import environment, install
from helpers import make_config
from test_worker_integration import Server, free_port, wait_for

ROOT = Path(__file__).resolve().parents[2]
ENTRIES = [{"id": f"vvvvvvvvv{i:02d}", "title": f"Video {i}"} for i in range(8)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    install(tmp_path / "bin", ENTRIES)
    for k, v in environment(tmp_path / "bin").items():
        monkeypatch.setenv(k, v)
    config = make_config(tmp_path, run_scheduler=True, scheduler_tick_seconds=0.2)
    migrate(config.db_path, config.backup_dir)
    conn = connect(config.db_path)
    podcasts.create(conn, {"id": "abgt", "name": "ABGT", "playlist_url": "https://www.youtube.com/playlist?list=ok"})
    wid, token = workers.create(conn, {"name": "w1", "tier": 1, "active": 1, "pause_min_s": None, "pause_max_s": None,
                                       "downloads_per_hour": None})
    conn.close()
    port = free_port()

    def db():
        return connect(config.db_path)

    return config, port, token, wid, db


def thread_worker(port, token, tmp_path, name="a"):
    stop = threading.Event()
    cfg = Config(server_url=f"http://127.0.0.1:{port}", token=token, poll_interval=0.2, backoff_start=0.1, backoff_max=0.4,
                 alive_file=str(tmp_path / f"alive-{name}"), heartbeat_interval=0.3)
    t = threading.Thread(target=run, args=(cfg, stop), kwargs={"found": {"ytdlp": "fake"}}, daemon=True)
    t.start()
    return stop, t


def test_scheduled_scan_runs_and_creates_exactly_the_missing_download_tasks(env, tmp_path):
    config, port, token, wid, db = env
    conn = db()
    for i in (0, 1):                                              # two videos are already episodes (one deleted)
        episodes.insert(conn, podcast_id="abgt", video_id=f"vvvvvvvvv{i:02d}", title="x", audio_file="a.m4a" if i == 0 else None,
                        state="available" if i == 0 else "deleted")
    conn.close()
    server = Server(config, port).start()
    stop, thread = thread_worker(port, token, tmp_path)
    try:
        def scan_done():
            c = db()
            try:
                return c.execute("SELECT COUNT(*) FROM tasks WHERE type = 'scan' AND state = 'done'").fetchone()[0] >= 1
            finally:
                c.close()

        assert wait_for(scan_done, 15), "the scheduled scan was not executed"
        c = db()
        ids = sorted(r["video_id"] for r in c.execute("SELECT video_id FROM tasks WHERE type = 'download'"))
        assert ids == [f"vvvvvvvvv{i:02d}" for i in range(2, 8)]                 # exactly the six unknown videos
        assert {r["state"] for r in c.execute("SELECT state FROM tasks WHERE type = 'download'")} == {"pending"}
        a = c.execute("SELECT * FROM attempts WHERE result = 'ok'").fetchone()
        assert a["worker_name"] == "w1" and "6 new, 2 known" in a["note"]
        c.close()
        # downloads stay untouched: the worker only does scans for now, even after many seconds
        time.sleep(1.5)
        c = db()
        assert c.execute("SELECT COUNT(*) FROM tasks WHERE type = 'download' AND (claimed_by IS NOT NULL OR failures > 0)").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM tasks WHERE type = 'download'").fetchone()[0] == 6
        c.close()
    finally:
        stop.set()
        thread.join(10)
        server.stop()


def test_killing_the_worker_mid_scan_returns_the_task_after_the_lease_expires(env, tmp_path):
    config, port, token, wid, db = env
    c = db()
    c.execute("UPDATE podcasts SET playlist_url = 'https://www.youtube.com/playlist?list=hang'")
    c.close()
    server = Server(config, port).start()
    worker_env = {**os.environ, "SERVER_URL": f"http://127.0.0.1:{port}", "WORKER_TOKEN": token, "POLL_INTERVAL": "0.2",
                  "PYTHONPATH": os.pathsep.join([str(ROOT / "worker"), str(ROOT / "shared")]), "SCAN_TIMEOUT": "120"}
    worker_env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"
    proc = subprocess.Popen([sys.executable, "-m", "pn_worker"], env=worker_env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def claimed():
            conn = db()
            try:
                return conn.execute("SELECT COUNT(*) FROM tasks WHERE state = 'claimed'").fetchone()[0] == 1
            finally:
                conn.close()

        assert wait_for(claimed, 20), "the worker never claimed the scan"
        os.killpg(proc.pid, signal.SIGKILL)                            # the worker AND its yt-dlp die, no goodbye
        proc.wait(10)
        time.sleep(0.6)
        conn = db()
        assert conn.execute("SELECT state FROM tasks WHERE type = 'scan'").fetchone()["state"] == "claimed"   # lease still valid
        conn.execute("UPDATE tasks SET lease_until = '2000-01-01T00:00:00Z' WHERE state = 'claimed'")        # time passes
        conn.execute("UPDATE podcasts SET playlist_url = 'https://www.youtube.com/playlist?list=ok'")        # the playlist works now
        conn.close()
        assert wait_for(lambda: _scan_state(db) == "pending" or _scan_state(db) == "done", 10), "the scheduler did not expire the lease"
        # a second worker takes over and finishes the job
        stop, thread = thread_worker(port, token, tmp_path, "b")
        try:
            assert wait_for(lambda: _scan_state(db) == "done", 15)
        finally:
            stop.set()
            thread.join(10)
        conn = db()
        results = [r["result"] for r in conn.execute("SELECT result FROM attempts ORDER BY id")]
        assert results[0] == "lease_expired" and results[-1] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE type = 'download'").fetchone()[0] == 8
        conn.close()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        server.stop()


def _scan_state(db):
    conn = db()
    try:
        row = conn.execute("SELECT state FROM tasks WHERE type = 'scan' ORDER BY id LIMIT 1").fetchone()
        return row["state"] if row else None
    finally:
        conn.close()


def test_two_workers_never_hold_the_same_task_end_to_end(env, tmp_path):
    config, port, token, wid, db = env
    conn = db()
    other_id, other_token = workers.create(conn, {"name": "w2", "tier": 1, "active": 1, "pause_min_s": None,
                                                  "pause_max_s": None, "downloads_per_hour": None})
    for i in range(5):                                            # five more podcasts: five scans at once
        podcasts.create(conn, {"id": f"p{i}", "name": f"P{i}", "playlist_url": "https://www.youtube.com/playlist?list=ok"})
    conn.close()
    server = Server(config, port).start()
    s1, t1 = thread_worker(port, token, tmp_path, "a")
    s2, t2 = thread_worker(port, other_token, tmp_path, "b")
    try:
        def all_done():
            c = db()
            try:
                return c.execute("SELECT COUNT(*) FROM tasks WHERE type = 'scan' AND state = 'done'").fetchone()[0] == 6
            finally:
                c.close()

        assert wait_for(all_done, 30)
        c = db()
        per_task = c.execute("SELECT task_id, COUNT(*) AS n FROM attempts WHERE result = 'ok' GROUP BY task_id").fetchall()
        assert len(per_task) == 6 and all(r["n"] == 1 for r in per_task)          # each scan ran exactly once
        assert c.execute("SELECT COUNT(*) FROM attempts WHERE result IS NULL").fetchone()[0] == 0
        c.close()
    finally:
        for s, t in ((s1, t1), (s2, t2)):
            s.set()
            t.join(10)
        server.stop()
