import pytest
from fastapi.testclient import TestClient

from app import events, notifications, queue
from app.db import connect
from app.main import create_app

from helpers import do_login, make_config
from qhelpers import NOW, add_download, add_podcast, add_worker, claim


@pytest.fixture
def c(tmp_path):
    config = make_config(tmp_path)
    with TestClient(create_app(config), base_url="https://testserver", follow_redirects=False) as client:
        do_login(client)
        conn = connect(config.db_path)
        add_podcast(conn, "abgt")
        add_podcast(conn, "asot")
        w = add_worker(conn, "homesrv01")
        add_download(conn, "abgt", "vid1", title="Episode One")
        add_download(conn, "asot", "vid2", title="Episode Two")
        w2 = add_worker(conn, "second")
        first, second = claim(conn, w), claim(conn, w2)                    # one task each
        assert (first.task["video_id"], second.task["video_id"]) == ("vid1", "vid2")
        queue.fail(conn, first.task["id"], w["id"], "bot_check", "Sign in to confirm you are not a bot", now=NOW)
        queue.fail(conn, second.task["id"], w2["id"], "bot_check", "again", now=NOW.replace(minute=2))
        conn.execute("INSERT INTO retention_log (at, podcast_id, video_id, title, reason, files, bytes) VALUES "
                     "('2026-11-05T13:00:00Z', 'abgt', 'old1', 'Old Episode', 'keep_last', 5, 1234)")
        notifications.enqueue(conn, "task_paused", "k1", "Task paused\nsecond line", NOW)
        client.conn = conn
        yield client
        conn.close()


def test_the_list_combines_all_sources_newest_first(c):
    rows = events.list_events(c.conn)
    kinds = {r["kind"] for r in rows}
    assert {"task", "retention", "message"} <= kinds
    assert [r["at"] for r in rows] == sorted((r["at"] for r in rows), reverse=True)
    page = c.get("/events").text
    assert "homesrv01" in page and "second" in page and "bot_check" in page and "Sign in to confirm you are not a bot" in page
    assert "files deleted (keep_last)" in page and "Old Episode" in page and "Task paused second line" in page
    assert "/tasks/" in page and "/episodes/abgt/old1" in page


def test_filters_by_kind_and_podcast(c):
    only_retention = c.get("/events?kind=retention").text
    assert "Old Episode" in only_retention and "homesrv01" not in only_retention
    assert events.count(c.conn, "retention") == 1 and events.count(c.conn, "message") == 1
    page = c.get("/events?podcast=asot").text
    assert "Episode Two" in page and "Episode One" not in page and "Old Episode" not in page
    assert "No events match the filter" in c.get("/events?kind=message&podcast=asot").text
    assert c.get("/events?kind=junk&podcast=Bad%20Slug&page=-4").status_code == 200       # junk means "no filter"


def test_paging(c):
    for n in range(120):
        c.conn.execute("INSERT INTO retention_log (at, podcast_id, video_id, title, reason) VALUES (?, 'abgt', ?, ?, 'manual')",
                       (f"2026-10-{n % 28 + 1:02d}T10:00:00Z", f"v{n}", f"Many {n}"))
    first = c.get("/events").text
    assert "Page 1 of" in first and first.count('class="card item"') == 50 and "Older →" in first
    last = c.get("/events?page=999").text
    assert "No events" in last or "Page" in last


def test_needs_login(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="https://testserver", follow_redirects=False) as anon:
        assert anon.get("/events").status_code == 303
