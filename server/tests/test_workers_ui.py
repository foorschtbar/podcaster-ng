import re

import pytest

from app import workers
from app.db import connect

from helpers import csrf_of, do_login, make_client


@pytest.fixture
def c(tmp_path):
    with make_client(tmp_path) as client:
        do_login(client)
        client.db = client.app.state.config.db_path
        yield client


def post(c, path, **fields):
    return c.post(path, data={"csrf_token": csrf_of(c), **fields})


def create(c, name="home", **over):
    return post(c, "/workers", **{"name": name, "tier": "2", "active": "1", **over})


def token_of(response):
    return re.search(r'class="token" value="([^"]+)"', response.text).group(1)


def rows(c):
    conn = connect(c.db)
    out = workers.list_all(conn)
    conn.close()
    return out


def test_empty_list_and_new_form(c):
    r = c.get("/workers")
    assert r.status_code == 200 and "No workers yet." in r.text
    form = c.get("/workers/new").text
    assert 'name="name"' in form and "Default: 5" in form and 'name="tier"' in form


def test_create_shows_token_once(c):
    r = create(c, pause_min_s="3")
    assert r.status_code == 200 and "shown only once" in r.text
    token = token_of(r)
    assert token.startswith("pnw_") and len(token) >= 47
    assert f"WORKER_TOKEN={token}" in r.text and "SERVER_URL=" in r.text
    conn = connect(c.db)
    assert workers.find_by_token(conn, token)["name"] == "home"
    conn.close()
    # never again: not in the list, not in the edit form, not in any later page
    for path in ("/workers", f"/workers/{rows(c)[0]['id']}", "/dashboard"):
        assert token not in c.get(path).text, path
    assert "Cache-Control" and c.get("/workers").headers["cache-control"] == "no-store"
    assert rows(c)[0]["token_hash"] not in c.get("/workers").text


def test_token_response_is_not_a_redirect(c):
    r = create(c)
    assert r.status_code == 200 and "location" not in r.headers


def test_created_worker_listed_as_never_connected(c):
    create(c)
    page = c.get("/workers").text
    assert "home" in page and "never connected" in page and "Tier 2" in page and "last seen never" in page


def test_status_shown_online_and_offline(c):
    create(c)
    worker_id = rows(c)[0]["id"]
    conn = connect(c.db)
    workers.touch(conn, worker_id)
    workers.record_hello(conn, worker_id, "1.2.3", ["ytdlp"], {"yt-dlp": "2026.1"})
    conn.close()
    page = c.get("/workers").text
    assert "● online" in page and "version 1.2.3" in page and "Capabilities: ytdlp" in page and "yt-dlp 2026.1" in page
    conn = connect(c.db)
    conn.execute("UPDATE workers SET last_seen = '2000-01-01T00:00:00Z'")
    conn.close()
    page = c.get("/workers").text
    assert "● online" not in page and ">offline<" in page
    post(c, f"/workers/{worker_id}/deactivate")
    assert ">inactive<" in c.get("/workers").text and "Activate" in c.get("/workers").text


def test_dashboard_shows_workers(c):
    assert "No workers yet." in c.get("/dashboard").text
    create(c)
    conn = connect(c.db)
    workers.touch(conn, rows(c)[0]["id"])
    conn.close()
    page = c.get("/dashboard").text
    assert "<strong>1</strong> of 1 online" in page and "home" in page


def test_invalid_input_saves_nothing(c):
    r = create(c, name="", pause_min_s="-1", tier="500")
    assert r.status_code == 422 and "Nothing was saved" in r.text and 'class="token"' not in r.text and "pnw_" not in r.text
    assert "Name must be" in r.text and "Tier must be between 1 and 99" in r.text and "Pause between videos, min (seconds) must be" in r.text
    assert rows(c) == []


def test_duplicate_name_rejected_together_with_other_errors(c):
    create(c)
    r = create(c, name="HOME")
    assert r.status_code == 422 and "already used" in r.text and len(rows(c)) == 1
    r = create(c, name="Home", tier="0")
    assert "already used" in r.text and "Tier must be" in r.text


def test_edit_keeps_token_and_updates_fields(c):
    token = token_of(create(c))
    worker_id = rows(c)[0]["id"]
    assert 'value="home"' in c.get(f"/workers/{worker_id}").text
    r = post(c, f"/workers/{worker_id}", name="renamed", tier="1", active="1", downloads_per_hour="2")
    assert r.headers["location"] == "/workers?msg=updated"
    row = rows(c)[0]
    assert (row["name"], row["tier"], row["downloads_per_hour"]) == ("renamed", 1, 2)
    conn = connect(c.db)
    assert workers.find_by_token(conn, token)["id"] == worker_id
    conn.close()
    r = post(c, f"/workers/{worker_id}", name="", tier="1")
    assert r.status_code == 422 and rows(c)[0]["name"] == "renamed"


def test_edit_cannot_take_another_workers_name(c):
    create(c, name="a")
    create(c, name="b")
    b = [r for r in rows(c) if r["name"] == "b"][0]
    r = post(c, f"/workers/{b['id']}", name="A", tier="1")
    assert r.status_code == 422 and "already used" in r.text
    assert post(c, f"/workers/{b['id']}", name="b", tier="3").headers["location"] == "/workers?msg=updated"  # own name is fine


def test_deactivate_and_activate(c):
    create(c)
    wid = rows(c)[0]["id"]
    assert post(c, f"/workers/{wid}/deactivate").headers["location"] == "/workers?msg=deactivated"
    assert not rows(c)[0]["active"]
    assert post(c, f"/workers/{wid}/activate").headers["location"] == "/workers?msg=activated"
    assert rows(c)[0]["active"]


def test_regenerate_needs_confirmation_and_shows_new_token_once(c):
    old = token_of(create(c))
    wid = rows(c)[0]["id"]
    confirm = c.get(f"/workers/{wid}/regenerate")
    assert "Regenerate token?" in confirm.text and old not in confirm.text
    conn = connect(c.db)
    assert workers.find_by_token(conn, old)  # a GET changes nothing
    conn.close()
    r = post(c, f"/workers/{wid}/regenerate")
    new = token_of(r)
    assert new != old and "The previous token no longer works" in r.text
    conn = connect(c.db)
    assert workers.find_by_token(conn, old) is None and workers.find_by_token(conn, new)
    conn.close()
    assert new not in c.get(f"/workers/{wid}").text


def test_delete_needs_confirmation(c):
    token = token_of(create(c))
    wid = rows(c)[0]["id"]
    assert "Delete worker?" in c.get(f"/workers/{wid}/delete").text and len(rows(c)) == 1
    assert post(c, f"/workers/{wid}/delete").headers["location"] == "/workers?msg=deleted"
    assert rows(c) == []
    conn = connect(c.db)
    assert workers.find_by_token(conn, token) is None
    conn.close()


def test_unknown_worker_is_404(c):
    for path in ("/workers/999", "/workers/999/delete", "/workers/999/regenerate"):
        assert c.get(path).status_code == 404, path
    for path in ("/workers/999", "/workers/999/deactivate", "/workers/999/activate", "/workers/999/regenerate",
                 "/workers/999/delete"):
        assert post(c, path, name="x", tier="1").status_code == 404, path
    assert c.get("/workers/0").status_code == 422 or c.get("/workers/0").status_code == 404


def test_requires_login_and_csrf(tmp_path):
    with make_client(tmp_path) as anon:
        for path in ("/workers", "/workers/new", "/workers/1", "/workers/1/delete", "/workers/1/regenerate"):
            assert anon.get(path).status_code == 303, path
        for path in ("/workers", "/workers/1", "/workers/1/deactivate", "/workers/1/regenerate", "/workers/1/delete"):
            assert anon.post(path, data={"name": "x"}).status_code == 303, path
        do_login(anon)
        for path in ("/workers", "/workers/1", "/workers/1/deactivate", "/workers/1/regenerate", "/workers/1/delete"):
            assert anon.post(path, data={"name": "x"}).status_code == 403, path
        conn = connect(anon.app.state.config.db_path)
        assert workers.list_all(conn) == []
        conn.close()


def test_token_page_has_no_inline_script(c):
    html = create(c).text
    assert "onfocus" not in html and not re.search(r"<script(?![^>]*\ssrc=)", html)


def test_token_page_offers_copy_buttons_and_selectable_fields(c):
    html = create(c).text
    token = token_of_html(html)
    assert 'data-copy="worker-token"' in html and 'data-copy="run-command"' in html
    assert 'id="worker-token"' in html and 'data-select-all' in html and 'id="run-command"' in html
    assert f"WORKER_TOKEN={token}" in html                               # the command to copy contains the real token
    assert '<script src="/static/copy.js"' in html
    js = c.get("/static/copy.js").text
    assert "navigator.clipboard" in js and "execCommand" in js and "innerHTML" not in js    # clipboard API with a legacy fallback
    css = c.get("/static/style.css").text
    assert "-webkit-user-select: all" in css                             # Safari needs the prefixed property


def token_of_html(html):
    return re.search(r'class="token" value="([^"]+)"', html).group(1)
