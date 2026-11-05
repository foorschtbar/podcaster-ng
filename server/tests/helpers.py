import re

from fastapi.testclient import TestClient

from app import auth
from app.config import Config
from app.main import create_app

USER, PASSWORD = "admin", "s3cret-pw"


def make_config(tmp_path, user=USER, password=PASSWORD, secret="", base_url="", import_dir=None, **extra):
    kw = {"import_dir": import_dir} if import_dir else {}
    # The scheduler stays off in tests unless a test asks for it (deterministic task tables).
    return Config(data_dir=tmp_path, base_url=base_url, log_level="INFO", admin_user=user, admin_password=password,
                  session_secret=secret, **{"run_scheduler": False, **kw, **extra})


def make_client(tmp_path, **kw):
    limiter = auth.LoginLimiter(delay=0)
    # https base URL, otherwise the Secure session cookie is not sent back
    return TestClient(create_app(make_config(tmp_path, **kw), limiter), base_url="https://testserver",
                      follow_redirects=False)


def login_token(client):
    html = client.get("/login").text
    return re.search(r'name="login_token" value="([^"]+)"', html).group(1)


def do_login(client, password=PASSWORD, user=USER):
    return client.post("/login", data={"username": user, "password": password,
                                       "login_token": login_token(client)})


def csrf_of(client):
    return re.search(r'name="csrf-token" content="([^"]+)"', client.get("/dashboard").text).group(1)


def settings_form(**over):
    """A complete, valid settings form (all defaults), so tests do not break when a setting is added."""
    from app import settings
    # secrets stay out; a checkbox is "off" when it is absent from the form
    form = {f.key: ("" if f.default is None else str(f.default)) for f in settings.FIELDS if f.kind not in ("secret", "bool")}
    form.update(over)
    return form
