import re

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import create_app

from helpers import PASSWORD, USER, csrf_of, do_login, login_token, make_client, make_config

PAGES = ["/dashboard", "/podcasts", "/episodes", "/workers", "/tasks", "/events", "/settings"]


@pytest.fixture
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


# --- locked mode -----------------------------------------------------------

@pytest.mark.parametrize("user,password", [("", ""), (USER, ""), ("", PASSWORD)])
def test_locked_when_credentials_missing(tmp_path, user, password):
    with make_client(tmp_path, user=user, password=password) as c:
        for path in ["/", "/login", "/dashboard", "/settings"]:
            r = c.get(path)
            assert r.status_code == 503, path
            assert "Access not configured" in r.text
        assert c.post("/login", data={"username": USER, "password": PASSWORD}).status_code == 503
        assert c.post("/logout").status_code == 503
        assert c.get("/healthz").status_code == 200
        assert c.get("/static/style.css").status_code == 200


# --- login / logout --------------------------------------------------------

def test_root_redirects_to_login_without_session(client):
    r = client.get("/")
    assert (r.status_code, r.headers["location"]) == (303, "/login")


@pytest.mark.parametrize("path", PAGES)
def test_admin_routes_redirect_without_session(client, path):
    r = client.get(path)
    assert (r.status_code, r.headers["location"]) == (303, "/login")


def test_htmx_request_without_session_gets_hx_redirect(client):
    r = client.get("/dashboard", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["hx-redirect"] == "/login"


def test_wrong_password_shows_error_and_counts(tmp_path):
    limiter = auth.LoginLimiter(delay=0, max_failures=3)
    app = create_app(make_config(tmp_path), limiter)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        r = do_login(c, password="nope")
        assert r.status_code == 401
        assert "Invalid username or password" in r.text
        assert "pn_session" not in r.headers.get("set-cookie", "")
        assert len(limiter._failures["testclient"]) == 1


def test_unknown_user_same_error(client):
    r = do_login(client, user="other")
    assert r.status_code == 401
    assert "Invalid username or password" in r.text


def test_login_success_cookie_and_dashboard(client):
    r = do_login(client)
    assert (r.status_code, r.headers["location"]) == (303, "/dashboard")
    cookie = r.headers["set-cookie"].lower()
    assert "pn_session=" in cookie
    for flag in ("httponly", "secure", "samesite=lax", "max-age="):
        assert flag in cookie
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Dashboard" in page.text
    assert client.get("/").headers["location"] == "/dashboard"
    assert client.get("/login").headers["location"] == "/dashboard"
    for path in PAGES:
        assert client.get(path).status_code == 200, path


def test_admin_pages_have_security_headers(client):
    do_login(client)
    h = client.get("/dashboard").headers
    assert h["cache-control"] == "no-store"
    assert h["x-frame-options"] == "DENY"
    assert "default-src 'self'" in h["content-security-policy"]
    assert "content-security-policy" not in client.get("/healthz").headers


def test_logout_ends_session(client):
    do_login(client)
    token = csrf_of(client)
    r = client.post("/logout", data={"csrf_token": token})
    assert (r.status_code, r.headers["location"]) == (303, "/login")
    assert client.get("/dashboard").status_code == 303


def test_tampered_or_foreign_cookie_rejected(client, tmp_path):
    do_login(client)
    value = client.cookies["pn_session"]
    client.cookies.clear()
    client.cookies.set("pn_session", value[:-2] + "xx")
    assert client.get("/dashboard").status_code == 303
    forged = auth.sign(b"another-secret", "session", {"u": USER, "csrf": "x"}, 3600)
    client.cookies.set("pn_session", forged)
    assert client.get("/dashboard").status_code == 303


def test_expired_session_rejected(client):
    forged = auth.sign(client.app.state.secret, "session", {"u": USER, "csrf": "x"}, -10)
    client.cookies.set("pn_session", forged)
    assert client.get("/dashboard").status_code == 303


def test_login_token_cannot_be_used_as_session(client):
    token = login_token(client)
    client.cookies.set("pn_session", token)
    assert client.get("/dashboard").status_code == 303


# --- lockout ---------------------------------------------------------------

def test_lockout_after_repeated_failures(tmp_path):
    now = [1000.0]
    limiter = auth.LoginLimiter(max_failures=3, lockout=600, delay=0, clock=lambda: now[0])
    app = create_app(make_config(tmp_path), limiter)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        for _ in range(3):
            assert do_login(c, password="bad").status_code == 401
        # now locked: even the correct password is refused
        r = do_login(c)
        assert r.status_code == 429
        assert "Too many failed attempts" in r.text
        assert c.get("/dashboard").status_code == 303
        now[0] += 601
        assert do_login(c).status_code == 303


def test_limiter_window_and_success_reset():
    now = [0.0]
    lim = auth.LoginLimiter(max_failures=3, window=100, lockout=50, delay=0, clock=lambda: now[0])
    lim.record_failure("a"); lim.record_failure("a")
    now[0] = 200  # first two fall out of the window
    lim.record_failure("a")
    assert lim.locked_for("a") == 0
    lim.record_failure("a"); lim.record_success("a"); lim.record_failure("a"); lim.record_failure("a")
    assert lim.locked_for("a") == 0
    lim.record_failure("a")
    assert lim.locked_for("a") > 0
    assert lim.locked_for("b") == 0


def test_login_delay_applied(tmp_path, monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    import types
    monkeypatch.setattr("app.admin.asyncio", types.SimpleNamespace(sleep=fake_sleep))  # only the login delay
    limiter = auth.LoginLimiter(delay=1.5)
    with TestClient(create_app(make_config(tmp_path), limiter), base_url="https://testserver",
                    follow_redirects=False) as c:
        do_login(c, password="bad")
    assert slept == [1.5]


# --- CSRF ------------------------------------------------------------------

def test_login_post_requires_valid_token(client):
    for data in ({"username": USER, "password": PASSWORD},
                 {"username": USER, "password": PASSWORD, "login_token": "garbage"},
                 {"username": USER, "password": PASSWORD,
                  "login_token": auth.sign(client.app.state.secret, "session", {}, 60)}):
        assert client.post("/login", data=data).status_code == 403
    assert client.get("/dashboard").status_code == 303


def test_logout_without_csrf_rejected(client):
    do_login(client)
    assert client.post("/logout").status_code == 403
    assert client.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
    assert client.get("/dashboard").status_code == 200  # still logged in


def test_csrf_accepted_via_header_for_htmx(client):
    do_login(client)
    token = csrf_of(client)
    r = client.post("/logout", headers={"X-CSRF-Token": token})
    assert r.status_code == 303


def test_state_changing_request_without_session_is_not_csrf_bypass(client):
    assert client.post("/logout", data={"csrf_token": "x"}).status_code == 303


def test_csrf_token_of_other_session_rejected(tmp_path):
    with make_client(tmp_path) as a, make_client(tmp_path) as b:
        do_login(a); do_login(b)
        assert a.post("/logout", data={"csrf_token": csrf_of(b)}).status_code == 403


# --- session secret --------------------------------------------------------

def test_session_secret_generated_once_and_persisted(tmp_path):
    with make_client(tmp_path) as c:
        first = c.app.state.secret
        do_login(c)
        cookie = c.cookies["pn_session"]
    with make_client(tmp_path) as c2:
        assert c2.app.state.secret == first
        c2.cookies.set("pn_session", cookie)
        assert c2.get("/dashboard").status_code == 200  # session survives restart


def test_session_secret_from_env(tmp_path):
    with make_client(tmp_path, secret="fixed") as c:
        assert c.app.state.secret == b"fixed"


# --- layout / templates ----------------------------------------------------

def test_pages_are_mobile_ready_and_escaped(client):
    r = client.get("/login")
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in r.text
    assert "/static/style.css" in r.text
    do_login(client)
    html = client.get("/tasks").text
    assert html.count('aria-current="page"') == 1
    assert "htmx.min.js" in html
    css = client.get("/static/style.css").text
    assert "prefers-color-scheme: dark" in css


def test_secrets_not_logged(tmp_path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    with make_client(tmp_path) as c:
        do_login(c, password="wrong-password-xyz")
        do_login(c)
        cookie = c.cookies["pn_session"]
    assert "wrong-password-xyz" not in caplog.text
    assert PASSWORD not in caplog.text
    assert cookie not in caplog.text
