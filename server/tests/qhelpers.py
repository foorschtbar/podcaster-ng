"""Shared fixtures for queue tests."""

from datetime import datetime, timedelta, timezone

from app import episodes, podcasts, queue, settings, workers
from app.db import connect
from app.migrations import migrate

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def make_db(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    return tmp_path / "p.db"


def add_podcast(conn, pid="abgt", **over):
    data = {"id": pid, "name": pid.upper(), "playlist_url": f"https://www.youtube.com/playlist?list={pid}", **over}
    podcasts.create(conn, data)
    return pid


def add_worker(conn, name="w1", **over):
    data = {"name": name, "tier": 1, "active": 1, "pause_min_s": None, "pause_max_s": None, "downloads_per_hour": None}
    data.update(over)
    worker_id, _ = workers.create(conn, data)
    return dict(workers.get(conn, worker_id))


def defaults(conn):
    return settings.get_all(conn)


def claim(conn, worker, types=("scan", "download"), now=NOW, lease=300):
    return queue.claim(conn, worker, list(types), defaults(conn), lease, now)


def add_download(conn, podcast_id, video_id, now=NOW, **over):
    cols = {"type": "download", "podcast_id": podcast_id, "video_id": video_id, "title": f"title {video_id}",
            "created_at": queue.ts(now), "updated_at": queue.ts(now), **over}
    cur = conn.execute(f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(cols.values()))
    return cur.lastrowid


def task(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def attempts(conn, task_id):
    return conn.execute("SELECT * FROM attempts WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()


def minutes(n):
    return timedelta(minutes=n)
