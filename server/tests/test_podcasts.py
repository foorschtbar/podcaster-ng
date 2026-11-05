import pytest

from app import podcasts, settings
from app.db import connect
from app.migrations import migrate

from helpers import csrf_of, do_login, make_client, settings_form

DEFAULTS = {"pause_min_s": 5, "pause_max_s": 20}


def valid(**over):
    form = {"id": "abgt", "name": "ABGT", "description": "Weekly show", "active": "1",
            "playlist_url": "https://www.youtube.com/playlist?list=PL123", "image_url": "",
            "scan_interval_min": "", "scan_depth": "20", "pause_min_s": "", "pause_max_s": "",
            "downloads_per_hour": "", "keep_last": "", "max_age_days": ""}
    form.update(over)
    return form


# --- validation ------------------------------------------------------------

def test_valid_form_and_defaults():
    data, errors = podcasts.validate(valid(), DEFAULTS, creating=True)
    assert errors == {}
    assert data["id"] == "abgt" and data["scan_depth"] == 20 and data["active"] == 1
    assert data["scan_interval_min"] is None and data["keep_last"] is None  # inherit defaults
    data, _ = podcasts.validate(valid(scan_depth="", active=""), DEFAULTS, creating=True)
    assert data["scan_depth"] == 20 and data["active"] == 0


@pytest.mark.parametrize("slug", ["", "ABGT", "a b", "a_b", "-a", "a-", "a--b", "ä", "../x", "new", "x" * 65])
def test_bad_slugs(slug):
    _, errors = podcasts.validate(valid(id=slug), DEFAULTS, creating=True)
    assert "id" in errors


@pytest.mark.parametrize("slug", ["a", "abgt", "a-b-1", "2024", "x" * 64])
def test_good_slugs(slug):
    _, errors = podcasts.validate(valid(id=slug), DEFAULTS, creating=True)
    assert "id" not in errors


@pytest.mark.parametrize("field,value", [
    ("name", ""), ("name", "x" * 201), ("playlist_url", ""), ("playlist_url", "youtube.com/x"),
    ("playlist_url", "javascript:alert(1)"), ("playlist_url", "https://"), ("image_url", "ftp://x/y.png"),
    ("scan_interval_min", "-1"), ("scan_interval_min", "4"), ("scan_depth", "0"), ("scan_depth", "-3"),
    ("scan_depth", "201"), ("scan_depth", "1e3"), ("pause_min_s", "-1"), ("downloads_per_hour", "0"),
    ("keep_last", "-2"), ("max_age_days", "0"), ("description", "x" * 4001),
])
def test_invalid_fields(field, value):
    _, errors = podcasts.validate(valid(**{field: value}), DEFAULTS, creating=True)
    assert field in errors


def test_pause_range_uses_effective_defaults():
    _, errors = podcasts.validate(valid(pause_min_s="10", pause_max_s="5"), DEFAULTS, creating=True)
    assert "pause_max_s" in errors
    # only min given: compared with the default max (20)
    _, errors = podcasts.validate(valid(pause_min_s="30"), DEFAULTS, creating=True)
    assert "pause_max_s" in errors
    _, errors = podcasts.validate(valid(pause_min_s="10"), DEFAULTS, creating=True)
    assert errors == {}


def test_update_never_changes_slug(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    conn = connect(tmp_path / "p.db")
    data, _ = podcasts.validate(valid(), DEFAULTS, creating=True)
    podcasts.create(conn, data)
    with pytest.raises(podcasts.SlugExists):
        podcasts.create(conn, data)
    data, _ = podcasts.validate(valid(id="other", name="Renamed"), DEFAULTS, creating=False)
    assert "id" not in data
    assert podcasts.update(conn, "abgt", {**data, "id": "other"})
    assert podcasts.get(conn, "other") is None and podcasts.get(conn, "abgt")["name"] == "Renamed"
    conn.close()


def test_database_rejects_bad_slug(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    conn = connect(tmp_path / "p.db")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO podcasts (id, name, playlist_url, created_at, updated_at) "
                     "VALUES ('Bad Slug', 'n', 'u', 'x', 'x')")
    conn.close()


# --- UI --------------------------------------------------------------------

def post(c, path, **fields):
    return c.post(path, data={"csrf_token": csrf_of(c), **fields})


def count(tmp_path):
    conn = connect(tmp_path / "podcaster.db")
    n = conn.execute("SELECT COUNT(*) FROM podcasts").fetchone()[0]
    conn.close()
    return n


@pytest.fixture
def c(tmp_path):
    with make_client(tmp_path) as client:
        do_login(client)
        yield client


def test_empty_list_and_new_form(c):
    r = c.get("/podcasts")
    assert r.status_code == 200 and "No podcasts yet." in r.text
    form = c.get("/podcasts/new").text
    assert 'name="id"' in form and 'name="scan_depth" type="text" value="20"' in form
    assert "Default: 5" in form  # pacing defaults come from settings


def test_full_lifecycle(c, tmp_path):
    r = post(c, "/podcasts", **valid())
    assert (r.status_code, r.headers["location"]) == (303, "/podcasts?msg=created")
    page = c.get("/podcasts?msg=created").text
    assert "Podcast created." in page and "ABGT" in page and "active" in page and "scan every 60 min" in page

    # edit: the slug is shown but cannot be changed, even by a forged field
    edit = c.get("/podcasts/abgt").text
    assert 'name="id"' not in edit and "<code>abgt</code>" in edit
    r = post(c, "/podcasts/abgt", **valid(id="hacked", name="ABGT Renamed", scan_interval_min="30"))
    assert r.headers["location"] == "/podcasts?msg=updated"
    assert c.get("/podcasts/hacked").status_code == 404
    page = c.get("/podcasts").text
    assert "ABGT Renamed" in page and "scan every 30 min" in page

    # pause / resume
    assert post(c, "/podcasts/abgt/pause").headers["location"] == "/podcasts?msg=paused"
    assert "paused" in c.get("/podcasts").text and "Resume" in c.get("/podcasts").text
    assert post(c, "/podcasts/abgt/resume").headers["location"] == "/podcasts?msg=resumed"
    assert "Pause" in c.get("/podcasts").text

    # delete needs the confirmation page; GET does not delete
    assert "Delete podcast?" in c.get("/podcasts/abgt/delete").text
    assert count(tmp_path) == 1
    assert post(c, "/podcasts/abgt/delete").headers["location"] == "/podcasts?msg=deleted"
    assert count(tmp_path) == 0
    assert c.get("/podcasts/abgt").status_code == 404


def test_active_checkbox_in_edit_form(c):
    post(c, "/podcasts", **valid())
    post(c, "/podcasts/abgt", **{k: v for k, v in valid().items() if k != "active"})
    assert "paused" in c.get("/podcasts").text


def test_duplicate_slug_rejected(c, tmp_path):
    post(c, "/podcasts", **valid())
    r = post(c, "/podcasts", **valid(name="Second"))
    assert r.status_code == 422
    assert "already used" in r.text and "Nothing was saved" in r.text
    assert count(tmp_path) == 1


def test_duplicate_slug_reported_together_with_other_errors(c):
    post(c, "/podcasts", **valid())
    r = post(c, "/podcasts", **valid(scan_depth="-1"))
    assert "already used" in r.text and "must be between 1 and 200" in r.text


def test_invalid_input_shows_errors_and_saves_nothing(c, tmp_path):
    r = post(c, "/podcasts", **valid(playlist_url="nope", scan_depth="-1", keep_last="-4", name=""))
    assert r.status_code == 422
    for msg in ("Playlist URL must be a valid http(s) URL", "Scan depth (latest videos) must be between 1 and 200",
                "Keep last episodes must be between 1", "Name is required", "Nothing was saved"):
        assert msg in r.text
    assert 'value="nope"' in r.text  # input is kept
    assert count(tmp_path) == 0


def test_invalid_edit_keeps_old_values(c):
    post(c, "/podcasts", **valid())
    r = post(c, "/podcasts/abgt", **valid(name="Changed", scan_depth="-1"))
    assert r.status_code == 422
    assert "<code>abgt</code>" in r.text
    assert "Changed" not in c.get("/podcasts").text


def test_user_input_is_escaped(c):
    post(c, "/podcasts", **valid(name="<script>alert(1)</script>", description="<b>x</b>"))
    assert "<script>alert(1)</script>" not in c.get("/podcasts").text
    edit = c.get("/podcasts/abgt").text
    assert "<script>alert(1)</script>" not in edit and "&lt;b&gt;x&lt;/b&gt;" in edit


def test_settings_defaults_shown_and_inherited(c):
    post(c, "/settings", **settings_form(scan_interval_min="15"))
    post(c, "/podcasts", **valid())
    assert "scan every 15 min" in c.get("/podcasts").text  # empty interval follows the setting


def test_unknown_podcast_is_404(c):
    assert c.get("/podcasts/nope").status_code == 404
    assert post(c, "/podcasts/nope", **valid()).status_code == 404
    assert post(c, "/podcasts/nope/pause").status_code == 404
    assert post(c, "/podcasts/nope/delete").status_code == 404


def test_new_is_not_a_podcast(c):
    r = post(c, "/podcasts", **valid(id="new"))
    assert r.status_code == 422


def test_requires_login_and_csrf(tmp_path):
    with make_client(tmp_path) as anon:
        for path in ["/podcasts", "/podcasts/new", "/podcasts/abgt", "/podcasts/abgt/delete"]:
            assert anon.get(path).status_code == 303, path
        for path in ["/podcasts", "/podcasts/abgt", "/podcasts/abgt/pause", "/podcasts/abgt/delete"]:
            assert anon.post(path, data=valid()).status_code == 303, path
        do_login(anon)
        for path in ["/podcasts", "/podcasts/abgt", "/podcasts/abgt/pause", "/podcasts/abgt/delete"]:
            assert anon.post(path, data=valid()).status_code == 403, path
        assert count(tmp_path) == 0


def test_podcasts_persist_across_restart(tmp_path):
    with make_client(tmp_path) as a:
        do_login(a)
        post(a, "/podcasts", **valid(keep_last="12"))
        cookie = a.cookies["pn_session"]
    with make_client(tmp_path) as b:
        b.cookies.set("pn_session", cookie)
        assert 'name="keep_last" type="text" value="12"' in b.get("/podcasts/abgt").text


def test_delete_refused_while_episodes_exist(c, tmp_path):
    from app import episodes
    post(c, "/podcasts", **valid())
    conn = connect(tmp_path / "podcaster.db")
    episodes.insert(conn, podcast_id="abgt", video_id="v1", title="T", audio_file="t.m4a")
    episodes.insert(conn, podcast_id="abgt", video_id="v2", state="deleted")  # known-only rows count too
    conn.close()
    page = c.get("/podcasts/abgt/delete").text
    assert "cannot be deleted" in page and "2 episode record(s)" in page and "Delete permanently" not in page
    r = post(c, "/podcasts/abgt/delete")
    assert r.status_code == 409 and "cannot be deleted" in r.text
    assert count(tmp_path) == 1
    assert "1 episodes" in c.get("/podcasts").text and "1 hidden/known" in c.get("/podcasts").text


def test_author_field(c):
    post(c, "/podcasts", **valid(author="Anjunabeats"))
    assert 'name="author" type="text" value="Anjunabeats"' in c.get("/podcasts/abgt").text
    r = post(c, "/podcasts/abgt", **valid(author="x" * 201))
    assert r.status_code == 422 and "Author must be at most 200" in r.text
