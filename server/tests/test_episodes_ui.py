import re
import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import episodes, settings
from app.db import connect
from app.main import create_app

from helpers import csrf_of, do_login, make_config, settings_form
from qhelpers import add_podcast
from test_retention import DAY0, add_episode, twenty


@pytest.fixture
def c(tmp_path):
    config = make_config(tmp_path)
    with TestClient(create_app(config), base_url="https://testserver", follow_redirects=False) as client:
        do_login(client)
        client.config = config
        conn = connect(config.db_path)
        twenty(conn, config.media_dir)
        conn.close()
        yield client


def q(c, sql, *args):
    conn = connect(c.config.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def post(c, path, **fields):
    return c.post(path, data={"csrf_token": csrf_of(c), **fields})


def feed_ids(c):
    root = ET.fromstring(c.get("/abgt.xml").text)
    return [i.findtext("title") for i in root.findall("channel/item")]


def test_list_filters_totals_and_paging(c):
    page = c.get("/episodes").text
    assert "Episode 19" in page and "20 episodes" not in page
    assert re.search(r"ABGT</a></th><td>20</td><td>0</td><td>0</td><td>1000\.0 B</td>", page) or "<td>20</td>" in page
    assert c.get("/episodes?state=hidden").text.count('class="card item"') == 0
    assert "No episodes match the filter" in c.get("/episodes?state=hidden").text
    assert c.get("/episodes?podcast=nope&state=junk&page=-3").status_code == 200       # junk is ignored
    # 20 rows fit one page; newest first
    page = c.get("/episodes?podcast=abgt").text
    assert page.index("Episode 19") < page.index("Episode 0<")


def test_detail_shows_files_with_sizes_and_missing_ones(c):
    (c.config.media_dir / "abgt" / "Episode 3.webp").unlink()
    page = c.get("/episodes/abgt/vid003").text
    assert "Episode 3.m4a" in page and "10 B" in page and "missing on disk" in page
    assert c.get("/episodes/abgt/unknown").status_code == 404
    assert c.get("/episodes/Bad Slug/x").status_code == 422


def test_hide_and_unhide_change_the_feed_at_once(c):
    assert len(feed_ids(c)) == 20 and "Episode 5" in feed_ids(c)
    assert post(c, "/episodes/abgt/vid005/hide").status_code == 303
    assert len(feed_ids(c)) == 19 and "Episode 5" not in feed_ids(c)
    assert (c.config.media_dir / "abgt" / "Episode 5.m4a").exists()                    # files kept
    assert c.get("/files/abgt/Episode%205.m4a").status_code == 200
    assert "Show in feed" in c.get("/episodes/abgt/vid005").text
    assert post(c, "/episodes/abgt/vid005/hide").status_code == 409                    # not twice
    assert post(c, "/episodes/abgt/vid005/unhide").status_code == 303
    assert "Episode 5" in feed_ids(c) and len(feed_ids(c)) == 20
    assert post(c, "/episodes/abgt/nope/hide").status_code == 404


def test_delete_needs_the_confirmation_page_and_keeps_the_row(c):
    page = c.get("/episodes/abgt/vid002/confirm?action=delete").text
    assert "never downloaded again" in page and "Episode 2.m4a" in page
    assert (c.config.media_dir / "abgt" / "Episode 2.m4a").exists()                    # viewing deletes nothing
    r = post(c, "/episodes/abgt/vid002/delete")
    assert r.status_code == 303 and r.headers["location"].endswith("?msg=deleted")
    assert not (c.config.media_dir / "abgt" / "Episode 2.m4a").exists()
    assert q(c, "SELECT state FROM episodes WHERE video_id = 'vid002'")[0][0] == "deleted"
    assert "Episode 2" not in feed_ids(c) and len(feed_ids(c)) == 19
    assert "deleted" in c.get("/episodes/abgt/vid002").text and "deleted by hand" in c.get("/episodes/abgt/vid002").text
    assert post(c, "/episodes/abgt/vid002/delete").status_code == 409
    assert q(c, "SELECT COUNT(*) FROM tasks")[0][0] == 0                                # nothing is queued


def test_redownload_queues_a_task_and_takes_the_episode_out_of_the_feed(c):
    r = post(c, "/episodes/abgt/vid004/redownload")
    assert r.status_code == 303 and re.fullmatch(r"/tasks/\d+", r.headers["location"])
    (t,) = q(c, "SELECT type, state, video_id FROM tasks")
    assert tuple(t) == ("download", "pending", "vid004")
    assert "Episode 4" not in feed_ids(c) and not (c.config.media_dir / "abgt" / "Episode 4.m4a").exists()
    assert post(c, "/episodes/abgt/vid004/redownload").status_code == 409              # already queued
    assert "download of this video is already queued" in post(c, "/episodes/abgt/vid004/redownload").text


def test_retention_page_preview_run_and_log(c):
    conn = connect(c.config.db_path)
    conn.execute("UPDATE podcasts SET keep_last = 10")
    conn.close()
    page = c.get("/retention").text
    assert "would delete the files of <strong>10</strong>" in page and "keep the last 10" in page and "Episode 0<" in page
    assert "Automatic deletion is <strong>off</strong>" in page
    assert "Nothing deleted yet" in page and len(q(c, "SELECT * FROM episodes WHERE state = 'deleted'")) == 0   # preview deletes nothing
    r = post(c, "/retention/run")
    assert r.status_code == 200 and "Deleted the files of 10 episode(s)" in r.text
    assert len(q(c, "SELECT * FROM episodes WHERE state = 'deleted'")) == 10 and len(feed_ids(c)) == 10
    page = c.get("/retention").text
    assert "A run now would delete nothing" in page and page.count("beyond the last N") == 10


def test_retention_run_reports_the_per_run_limit(c):
    conn = connect(c.config.db_path)
    conn.execute("UPDATE podcasts SET keep_last = 10")
    settings.save(conn, {"retention_max_per_run": 3})
    conn.close()
    r = post(c, "/retention/run")
    assert "Deleted the files of 3 episode(s)" in r.text and "7 more match the rules" in r.text
    assert [x[0] for x in q(c, "SELECT video_id FROM episodes WHERE state = 'deleted' ORDER BY video_id")] == ["vid000", "vid001", "vid002"]


def test_podcast_page_shows_the_preview(c):
    assert "No retention rule" in c.get("/podcasts/abgt").text
    conn = connect(c.config.db_path)
    conn.execute("UPDATE podcasts SET keep_last = 12")
    conn.close()
    page = c.get("/podcasts/abgt").text
    assert "would delete <strong>8</strong>" in page and "/retention?podcast=abgt" in page


def test_settings_page_has_the_retention_controls(c):
    page = c.get("/settings").text
    assert 'name="retention_enabled"' in page and 'name="retention_time"' in page and 'name="retention_max_per_run"' in page
    r = post(c, "/settings", **settings_form(retention_time="25:99"))
    assert r.status_code == 422 and "must be a time like 03:45" in r.text
    r = post(c, "/settings", **settings_form(retention_enabled="1", retention_time="04:15", retention_max_per_run="5"))
    assert r.status_code == 303
    conn = connect(c.config.db_path)
    assert (settings.get(conn, "retention_enabled"), settings.get(conn, "retention_time"), settings.get(conn, "retention_max_per_run")) == (1, "04:15", 5)
    conn.close()


def test_pages_need_login_and_csrf(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="https://testserver", follow_redirects=False) as anon:
        for path in ("/episodes", "/retention", "/episodes/abgt/x"):
            assert anon.get(path).status_code == 303
        for path in ("/retention/run", "/episodes/abgt/x/delete", "/episodes/abgt/x/hide", "/episodes/abgt/x/redownload"):
            assert anon.post(path).status_code == 303
        do_login(anon)
        for path in ("/retention/run", "/episodes/abgt/x/delete", "/episodes/abgt/x/hide", "/episodes/abgt/x/redownload"):
            assert anon.post(path).status_code == 403
