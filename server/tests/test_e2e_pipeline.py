"""A new video on a playlist becomes an episode in the feed, without any manual action."""

import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest

from app import podcasts, queue, workers
from app.db import connect
from app.migrations import migrate
from pn_worker.config import Config
from pn_worker.main import run

from fake_ytdlp import environment, install
from helpers import make_config
from test_worker_integration import Server, free_port, wait_for

NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "podcast": "https://podcastindex.org/namespace/1.0"}
TOOLS = {"ytdlp": "fake", "ffmpeg": "fake", "ffprobe": "fake"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    soon = int((datetime.now(timezone.utc) + timedelta(hours=5)).timestamp())
    entries = [{"id": "vid00000001", "title": "ignored in a flat playlist"},
               {"id": "premiere0001", "title": "Announced premiere", "live_status": "is_upcoming", "release_timestamp": soon},
               {"id": "premiere0002", "title": "Premiere nobody announced", "live_status": "not_live"},
               {"id": "bot00000001", "title": "Blocked video"},
               {"id": "private0001", "title": "Turned private"}]
    install(tmp_path / "bin", entries)
    for k, v in environment(tmp_path / "bin").items():
        monkeypatch.setenv(k, v)
    config = make_config(tmp_path, run_scheduler=True, scheduler_tick_seconds=0.2)
    migrate(config.db_path, config.backup_dir)
    conn = connect(config.db_path)
    podcasts.create(conn, {"id": "abgt", "name": "ABGT", "playlist_url": "https://www.youtube.com/playlist?list=ok",
                           "pause_min_s": 0, "pause_max_s": 0, "downloads_per_hour": 100})
    _, token = workers.create(conn, {"name": "local", "tier": 1, "active": 1, "pause_min_s": 0, "pause_max_s": 0, "downloads_per_hour": 100})
    conn.close()
    return config, free_port(), token


def start_worker(port, token, tmp_path):
    stop = threading.Event()
    cfg = Config(server_url=f"http://127.0.0.1:{port}", token=token, poll_interval=0.2, backoff_start=0.1, backoff_max=0.4,
                 alive_file=str(tmp_path / "alive"), heartbeat_interval=0.3, work_dir=str(tmp_path / "work"))
    thread = threading.Thread(target=run, args=(cfg, stop), kwargs={"found": TOOLS}, daemon=True)
    thread.start()
    return stop, thread


def test_new_videos_become_episodes_and_problem_videos_are_handled(env, tmp_path):
    config, port, token = env
    server = Server(config, port).start()
    stop, thread = start_worker(port, token, tmp_path)

    def db():
        return connect(config.db_path)

    def state_of(video):
        c = db()
        try:
            r = c.execute("SELECT * FROM tasks WHERE video_id = ?", (video,)).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    try:
        assert wait_for(lambda: (state_of("vid00000001") or {}).get("state") == "done", 30), "the video was not downloaded"
        # the playlist order is kept; everything else was classified as it should be
        assert wait_for(lambda: (state_of("bot00000001") or {}).get("failures") == 1, 15)
        assert wait_for(lambda: (state_of("private0001") or {}).get("state") == "closed", 15)
        assert wait_for(lambda: (state_of("premiere0002") or {}).get("not_before") is not None, 15)

        # --- the episode: in the feed, with the right enclosure, artwork and chapters
        import httpx
        feed = httpx.get(f"http://127.0.0.1:{port}/abgt.xml")
        items = ET.fromstring(feed.text).findall("channel/item")
        assert len(items) == 1
        item = items[0]
        enc = item.find("enclosure")
        assert enc.get("url") == f"http://127.0.0.1:{port}/files/abgt/Episode%EF%BC%9A%20vid00000001.m4a"
        assert enc.get("type") == "audio/x-m4a" and int(enc.get("length")) > 2000
        assert item.find("itunes:image", NS).get("href").endswith("/Episode%EF%BC%9A%20vid00000001.square.jpg")
        assert item.find("podcast:chapters", NS).get("url").endswith(".chapters.json")
        assert item.findtext("itunes:duration", namespaces=NS) == "3600" and item.findtext("title") == "Episode: vid00000001"
        for url in (enc.get("url"), item.find("itunes:image", NS).get("href"), item.find("podcast:chapters", NS).get("url")):
            assert httpx.get(url).status_code == 200
        assert len(httpx.get(enc.get("url")).content) == int(enc.get("length"))
        assert httpx.get(enc.get("url"), headers={"Range": "bytes=10-19"}).status_code == 206
        assert not list((config.staging_dir).glob("*")) if config.staging_dir.exists() else True
        assert not list((tmp_path / "work").glob("task-*"))

        c = db()
        # --- a premiere announced by the scan waits for its start, without failing
        t = state_of("premiere0001")
        assert t["state"] == "pending" and t["failures"] == 0 and t["not_before"] and t["claimed_by"] is None
        # --- a premiere nobody announced is deferred when the download finds out; not counted as a failure
        t = state_of("premiere0002")
        assert t["state"] == "pending" and t["failures"] == 0
        assert queue.parse(t["not_before"]) > datetime.now(timezone.utc) + timedelta(hours=3)
        a = c.execute("SELECT * FROM attempts WHERE task_id = ?", (t["id"],)).fetchall()
        assert [(x["result"], x["error_class"]) for x in a] == [("deferred", "not_yet_available")]
        # --- a blocked video is a counted failure with a backoff and the real message in the history
        t = state_of("bot00000001")
        assert t["state"] == "pending" and t["failures"] == 1 and t["not_before"]
        a = c.execute("SELECT * FROM attempts WHERE task_id = ?", (t["id"],)).fetchall()
        assert (a[0]["result"], a[0]["error_class"]) == ("failed", "bot_check") and "not a bot" in a[0]["error_text"]
        # --- a private video is closed for good
        assert c.execute("SELECT error_class FROM attempts WHERE task_id = ?", (state_of("private0001")["id"],)).fetchone()[0] == "private"
        # --- the episode row and its task agree; nothing else became an episode
        rows = c.execute("SELECT video_id, state, audio_file, thumb_file, square_file, chapters_file, info_file FROM episodes").fetchall()
        assert len(rows) == 1 and rows[0]["state"] == "available" and all(rows[0][k] for k in rows[0].keys())
        ok = c.execute("SELECT note FROM attempts WHERE result = 'ok' AND task_id = ?", (state_of("vid00000001")["id"],)).fetchone()
        assert "published" in ok["note"]
        c.close()

        # --- a later scan finds nothing new: no duplicate tasks, no second episode
        before = db().execute("SELECT COUNT(*) FROM tasks WHERE type = 'download'").fetchone()[0]
        c = db()
        queue.create_scan(c, "abgt")
        c.close()
        assert wait_for(lambda: db().execute("SELECT COUNT(*) FROM tasks WHERE type = 'scan' AND state = 'done'").fetchone()[0] >= 2, 15)
        assert db().execute("SELECT COUNT(*) FROM tasks WHERE type = 'download'").fetchone()[0] == before == 5
    finally:
        stop.set()
        thread.join(15)
        server.stop()
