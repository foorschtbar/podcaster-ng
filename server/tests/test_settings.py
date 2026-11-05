import pytest

from app import settings
from app.db import connect
from app.migrations import migrate

from helpers import csrf_of, do_login, make_client


@pytest.fixture
def conn(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    c = connect(tmp_path / "p.db")
    yield c
    c.close()


def valid_form(**over):
    form = {f.key: "" for f in settings.FIELDS if f.kind != "secret"}
    form.update({"scan_interval_min": "30", "pause_min_s": "2", "pause_max_s": "9", "downloads_per_hour": "5",
                 "upload_limit_mb": "512", "lease_seconds": "120", "escalation_deadline_min": "15",
                 "failure_threshold": "3"})
    form.update(over)
    return form


# --- module ----------------------------------------------------------------

def test_defaults_and_types(conn):
    s = settings.get_all(conn)
    assert s["scan_interval_min"] == 60 and isinstance(s["scan_interval_min"], int)
    assert (s["pause_min_s"], s["pause_max_s"], s["downloads_per_hour"]) == (5, 20, 3)
    assert (s["lease_seconds"], s["escalation_deadline_min"], s["failure_threshold"]) == (300, 10, 4)
    assert s["upload_limit_mb"] == 1024
    assert s["keep_last"] is None and s["max_age_days"] is None
    assert s["telegram_token"] == "" and s["base_url"] == ""
    assert settings.get(conn, "failure_threshold") == 4


def test_env_default_for_base_url(conn):
    env = {"base_url": "https://env.example"}
    assert settings.get(conn, "base_url", env) == "https://env.example"
    settings.save(conn, {"base_url": "https://db.example"})
    assert settings.get(conn, "base_url", env) == "https://db.example"
    settings.save(conn, {"base_url": None})  # reset to default
    assert settings.get(conn, "base_url", env) == "https://env.example"


def test_save_and_typed_read(conn):
    changes, errors = settings.validate(valid_form(base_url="https://x.example/", keep_last="10"))
    assert errors == {}
    settings.save(conn, changes)
    s = settings.get_all(conn)
    assert s["base_url"] == "https://x.example"  # trailing slash removed
    assert s["scan_interval_min"] == 30 and s["keep_last"] == 10 and s["max_age_days"] is None


def test_corrupt_stored_value_falls_back_to_default(conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('lease_seconds', 'abc')")
    assert settings.get(conn, "lease_seconds") == 300


@pytest.mark.parametrize("field,value", [
    ("scan_interval_min", "-5"), ("scan_interval_min", "0"), ("scan_interval_min", "abc"),
    ("scan_interval_min", ""), ("downloads_per_hour", "0"), ("pause_min_s", "-1"),
    ("upload_limit_mb", "1.5"), ("lease_seconds", "5"), ("failure_threshold", "-1"),
    ("keep_last", "0"), ("max_age_days", "-3"), ("base_url", "ftp://x"), ("base_url", "not a url"),
    ("telegram_chat_id", "hello world"),
])
def test_invalid_values_rejected(field, value):
    _, errors = settings.validate(valid_form(**{field: value}))
    assert field in errors


def test_pause_max_below_min_rejected():
    _, errors = settings.validate(valid_form(pause_min_s="30", pause_max_s="10"))
    assert "pause_max_s" in errors


def test_secret_handling(conn):
    token = "123456:ABCdefGhIJKlmNoPQRsTUVwxyz"
    changes, errors = settings.validate({**valid_form(), "telegram_token": token})
    assert errors == {} and changes["telegram_token"] == token
    settings.save(conn, changes)
    # empty field keeps the stored secret
    changes, _ = settings.validate(valid_form())
    assert "telegram_token" not in changes
    settings.save(conn, changes)
    assert settings.get(conn, "telegram_token") == token
    # explicit removal
    changes, _ = settings.validate({**valid_form(), "clear_telegram_token": "1"})
    settings.save(conn, changes)
    assert settings.get(conn, "telegram_token") == ""
    _, errors = settings.validate({**valid_form(), "telegram_token": "garbage"})
    assert "telegram_token" in errors


class _Boom:
    def __str__(self):
        raise RuntimeError("boom")


def test_failed_save_rolls_back(conn):
    with pytest.raises(RuntimeError):
        settings.save(conn, {"lease_seconds": 100, "failure_threshold": _Boom()})
    assert settings.get(conn, "lease_seconds") == 300


# --- UI --------------------------------------------------------------------

TOKEN = "123456:ABCdefGhIJKlmNoPQRsTUVwxyz"


def post_settings(c, **over):
    return c.post("/settings", data={**valid_form(), "csrf_token": csrf_of(c), **over})


def test_settings_page_shows_defaults(tmp_path):
    with make_client(tmp_path) as c:
        do_login(c)
        r = c.get("/settings")
        assert r.status_code == 200
        assert 'name="scan_interval_min" type="text" value="60"' in r.text
        assert "not set" in r.text


def test_settings_persist_across_restart_and_apply_without_restart(tmp_path):
    with make_client(tmp_path) as c:
        do_login(c)
        r = post_settings(c, scan_interval_min="45", keep_last="7")
        assert (r.status_code, r.headers["location"]) == (303, "/settings?saved=1")
        # takes effect immediately, in the same process
        page = c.get("/settings?saved=1").text
        assert "Settings saved." in page and 'name="scan_interval_min" type="text" value="45"' in page
        cookie = c.cookies["pn_session"]
    with make_client(tmp_path) as c2:  # new app instance on the same data directory
        c2.cookies.set("pn_session", cookie)
        page = c2.get("/settings").text
        assert 'value="45"' in page and 'name="keep_last" type="text" value="7"' in page


def test_invalid_settings_show_errors_and_save_nothing(tmp_path):
    with make_client(tmp_path) as c:
        do_login(c)
        r = post_settings(c, scan_interval_min="-5", lease_seconds="abc", keep_last="0")
        assert r.status_code == 422
        assert "Nothing was saved" in r.text
        assert "Scan interval (minutes) must be between 5 and 10080" in r.text
        assert "must be a whole number" in r.text
        assert 'value="-5"' in r.text  # submitted input is kept for correction
        again = c.get("/settings").text
        assert 'name="scan_interval_min" type="text" value="60"' in again


def test_secret_never_rendered(tmp_path):
    with make_client(tmp_path) as c:
        do_login(c)
        r = post_settings(c, telegram_token=TOKEN)
        assert r.status_code == 303
        page = c.get("/settings").text
        assert TOKEN not in page and "123456" not in page
        assert "set – leave empty to keep" in page
        # a failed save re-renders without leaking it either
        bad = post_settings(c, scan_interval_min="x")
        assert bad.status_code == 422 and TOKEN not in bad.text
        # leaving the field empty keeps it
        post_settings(c)
        assert "set – leave empty to keep" in c.get("/settings").text
        post_settings(c, clear_telegram_token="1")
        assert "not set" in c.get("/settings").text


def test_settings_require_login_and_csrf(tmp_path):
    with make_client(tmp_path) as c:
        assert c.get("/settings").status_code == 303
        assert c.post("/settings", data=valid_form()).status_code == 303
        do_login(c)
        assert c.post("/settings", data=valid_form()).status_code == 403
