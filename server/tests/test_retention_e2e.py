"""A deleted episode comes back through the real worker pipeline with the same file name."""

import xml.etree.ElementTree as ET

import httpx

from app import retention
from app.db import connect

from test_e2e_pipeline import env, start_worker  # noqa: F401 (env is a fixture)
from test_worker_integration import Server, wait_for


def test_redownload_restores_the_episode_with_the_same_file_name(env, tmp_path):  # noqa: F811
    config, port, token = env
    server = Server(config, port).start()
    stop, thread = start_worker(port, token, tmp_path)

    def one(sql, *args):
        c = connect(config.db_path)
        try:
            r = c.execute(sql, args).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def feed_titles():
        root = ET.fromstring(httpx.get(f"http://127.0.0.1:{port}/abgt.xml").text)
        return [i.findtext("title") for i in root.findall("channel/item")]

    try:
        assert wait_for(lambda: (one("SELECT state FROM tasks WHERE video_id = 'vid00000001'") or {}).get("state") == "done", 30)
        before = one("SELECT * FROM episodes WHERE video_id = 'vid00000001'")
        assert feed_titles() == ["Episode: vid00000001"]
        audio = config.media_dir / "abgt" / before["audio_file"]
        assert audio.exists()

        conn = connect(config.db_path)
        task_id = retention.redownload(conn, config.media_dir, "abgt", "vid00000001")
        conn.close()
        assert feed_titles() == [] and not audio.exists()                             # gone at once
        assert httpx.get(f"http://127.0.0.1:{port}/files/abgt/" + before["audio_file"]).status_code == 404

        assert wait_for(lambda: (one("SELECT state FROM tasks WHERE id = ?", task_id) or {}).get("state") == "done", 30)
        after = one("SELECT * FROM episodes WHERE video_id = 'vid00000001'")
        assert after["state"] == "available" and after["audio_file"] == before["audio_file"]
        assert (config.media_dir / "abgt" / after["audio_file"]).exists()
        assert feed_titles() == ["Episode: vid00000001"]
        assert one("SELECT COUNT(*) AS n FROM episodes")["n"] == 1
    finally:
        stop.set()
        thread.join(15)
        server.stop()
