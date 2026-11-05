import re
from datetime import timedelta

import pytest

from app import queue
from app.db import connect

from helpers import csrf_of, do_login, make_client
from qhelpers import NOW, add_download, add_podcast, add_worker, attempts, claim, task


@pytest.fixture
def c(tmp_path):
    with make_client(tmp_path) as client:
        do_login(client)
        client.db = client.app.state.config.db_path
        conn = connect(client.db)
        add_podcast(conn, "abgt")
        add_podcast(conn, "asot")
        conn.close()
        yield client


def q(c, fn):
    conn = connect(c.db)
    try:
        return fn(conn)
    finally:
        conn.close()


def post(c, path, **fields):
    return c.post(path, data={"csrf_token": csrf_of(c), **fields})


def seed(c):
    scan = q(c, lambda x: queue.create_scan(x, "abgt"))
    d1 = q(c, lambda x: add_download(x, "abgt", "v1"))
    d2 = q(c, lambda x: add_download(x, "asot", "v2", state="closed"))
    d3 = q(c, lambda x: add_download(x, "asot", "v3", state="paused"))
    return scan, d1, d2, d3


def test_empty_list(c):
    r = c.get("/tasks")
    assert r.status_code == 200 and "No tasks." in r.text and "0 pending" in r.text


def test_list_filters(c):
    scan, d1, d2, d3 = seed(c)
    page = c.get("/tasks").text
    for tid in (scan, d1, d2, d3):
        assert f"#{tid} " in page
    only = c.get("/tasks?state=closed").text
    assert f"#{d2} " in only and f"#{d1} " not in only
    by_podcast = c.get("/tasks?podcast=asot").text
    assert f"#{d3} " in by_podcast and f"#{scan} " not in by_podcast
    assert f"#{scan} " in c.get("/tasks?type=scan").text and f"#{d1} " not in c.get("/tasks?type=scan").text
    both = c.get("/tasks?state=pending&podcast=abgt").text
    assert f"#{d1} " in both and f"#{scan} " in both and f"#{d2} " not in both
    assert "No tasks match the filter" in c.get("/tasks?state=done").text
    # selected values stay selected
    assert '<option value="closed" selected>' in only and '<option value="asot" selected>' in by_podcast


@pytest.mark.parametrize("query", ["state=bogus", "podcast=../etc", "type=<script>", "page=-3", "page=99999", "state[]=x", "podcast=a%00b"])
def test_bad_filter_values_are_ignored_not_errors(c, query):
    seed(c)
    r = c.get(f"/tasks?{query}")
    assert r.status_code == 200 and "<script>" not in r.text


def test_pagination(c):
    q(c, lambda x: [add_download(x, "abgt", f"v{i}") for i in range(120)])
    first = c.get("/tasks").text
    assert "Page 1 of 3" in first and first.count('class="card item"') == 50 and "Older →" in first and "Newer" not in first
    last = c.get("/tasks?page=3").text
    assert last.count('class="card item"') == 20 and "← Newer" in last and "Older →" not in last
    assert "state=pending&amp;page=2" in c.get("/tasks?state=pending").text


def test_detail_shows_attempts_and_errors(c):
    conn = connect(c.db)
    w = add_worker(conn)
    tid = queue.create_scan(conn, "abgt", NOW)
    claim(conn, w)
    queue.fail(conn, tid, w["id"], "bot_check", "Sign in to confirm you're not a bot <b>x</b>", now=NOW + timedelta(minutes=1))
    claim(conn, w, now=NOW + timedelta(minutes=5))
    conn.close()
    page = c.get(f"/tasks/{tid}").text
    assert f"Task #{tid}" in page and "ABGT" in page and "Attempts (2)" in page
    assert "bot_check" in page and "not a bot" in page and "running" in page and "failed" in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page            # error text is escaped
    assert "Release" in page and "Close" in page and "Retry" not in page.replace("Retry now", "")


def test_detail_unknown_task_is_404(c):
    assert c.get("/tasks/9999").status_code == 404
    assert post(c, "/tasks/9999/retry").status_code == 404


def test_release_retry_close_actions(c):
    conn = connect(c.db)
    w = add_worker(conn)
    tid = queue.create_scan(conn, "abgt", NOW)
    claim(conn, w)
    conn.close()
    r = post(c, f"/tasks/{tid}/release")
    assert (r.status_code, r.headers["location"]) == (303, f"/tasks/{tid}?msg=released")
    assert "Task released" in c.get(f"/tasks/{tid}?msg=released").text
    assert q(c, lambda x: task(x, tid))["state"] == "pending"
    assert post(c, f"/tasks/{tid}/close").headers["location"].endswith("msg=closed")
    assert q(c, lambda x: task(x, tid))["state"] == "closed"
    assert post(c, f"/tasks/{tid}/retry").headers["location"].endswith("msg=retried")
    assert q(c, lambda x: task(x, tid))["state"] == "pending"


def test_impossible_actions_show_a_message(c):
    scan, d1, d2, _ = seed(c)
    r = post(c, f"/tasks/{d1}/release")                      # pending, not claimed
    assert r.status_code == 409 and "Not possible: only a claimed or paused task can be released" in r.text
    r = post(c, f"/tasks/{d2}/close")                        # already closed
    assert r.status_code == 409 and "only an open task" in r.text
    q(c, lambda x: x.execute("UPDATE tasks SET state = 'done' WHERE id = ?", (d1,)))
    assert post(c, f"/tasks/{d1}/retry").status_code == 409


def test_retry_conflict_with_another_open_scan(c):
    first = q(c, lambda x: queue.create_scan(x, "abgt"))
    q(c, lambda x: queue.close(x, first))
    q(c, lambda x: queue.create_scan(x, "abgt"))
    r = post(c, f"/tasks/{first}/retry")
    assert r.status_code == 409 and "already open" in r.text


def test_scan_now(c):
    r = post(c, "/podcasts/abgt/scan")
    assert r.status_code == 303 and re.match(r"^/tasks/\d+$", r.headers["location"])
    tid = int(r.headers["location"].rsplit("/", 1)[1])
    assert q(c, lambda x: (task(x, tid)["type"], task(x, tid)["podcast_id"], task(x, tid)["state"])) == ("scan", "abgt", "pending")
    again = post(c, "/podcasts/abgt/scan")
    assert again.headers["location"] == "/podcasts?msg=scan_open"
    assert "already queued or running" in c.get("/podcasts?msg=scan_open").text
    assert q(c, lambda x: queue.count_tasks(x, type_="scan")) == 1


def test_scan_now_for_paused_or_unknown_podcast(c):
    post(c, "/podcasts/asot/pause")
    r = post(c, "/podcasts/asot/scan")
    assert r.headers["location"] == "/podcasts?msg=scan_paused"
    assert "Resume it first" in c.get("/podcasts?msg=scan_paused").text
    assert q(c, lambda x: queue.count_tasks(x)) == 0
    assert post(c, "/podcasts/nope/scan").status_code == 404


def test_podcast_list_has_scan_button(c):
    assert "Scan now" in c.get("/podcasts").text


def test_dashboard_queue_card(c):
    page = c.get("/dashboard").text
    assert "<h2 id=\"q-h\">Queue</h2>" in page and "0 pending" in page and "No errors so far." in page
    conn = connect(c.db)
    w = add_worker(conn)
    tid = queue.create_scan(conn, "abgt", NOW)
    add_download(conn, "abgt", "v1")
    claim(conn, w)
    queue.fail(conn, tid, w["id"], "network", "Temporary failure in name resolution", now=NOW)
    conn.close()
    page = c.get("/dashboard").text
    assert "2 pending" in page and "1 downloads open" in page and "network" in page and "Temporary failure" in page


def test_tasks_require_login_and_csrf(tmp_path):
    with make_client(tmp_path) as anon:
        for path in ("/tasks", "/tasks/1"):
            assert anon.get(path).status_code == 303
        for path in ("/tasks/1/release", "/tasks/1/retry", "/tasks/1/close", "/podcasts/abgt/scan"):
            assert anon.post(path).status_code == 303
        do_login(anon)
        for path in ("/tasks/1/release", "/tasks/1/retry", "/tasks/1/close", "/podcasts/abgt/scan"):
            assert anon.post(path).status_code == 403
