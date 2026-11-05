import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from helpers import csrf_of, do_login, make_config


@pytest.fixture
def c(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="https://testserver", follow_redirects=False) as client:
        yield client


def test_a_huge_form_is_refused_with_413_and_security_headers(c):
    r = c.post("/login", content=b"a=" + b"x" * (2 << 20), headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413 and r.headers["x-content-type-options"] == "nosniff"


def test_a_streamed_body_without_content_length_is_cut_off(c):
    def chunks():
        for _ in range(40):
            yield b"x" * (64 << 10)
    r = c.post("/login", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_normal_requests_and_small_forms_pass(c):
    assert c.get("/healthz").status_code == 200
    assert do_login(c).status_code == 303
    assert c.post("/settings", data={"csrf_token": csrf_of(c), "base_url": "x" * 5000}).status_code in (303, 422)


def test_the_worker_upload_is_not_subject_to_the_small_limit(c):
    # no valid token: it must fail on authentication (401), not on size (413)
    r = c.post("/api/worker/tasks/1/upload?role=audio&size=3000000&sha256=" + "0" * 64 + "&ext=m4a", content=b"x" * (3 << 20),
               headers={"authorization": "Bearer pnw_" + "a" * 43})
    assert r.status_code == 401
