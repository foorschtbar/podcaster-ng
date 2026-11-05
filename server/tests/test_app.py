import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Config
from app.db import connect
from app.main import create_app
from app.migrations import migrate

SERVER_DIR = Path(__file__).resolve().parent.parent


def make_config(tmp_path):
    return Config(data_dir=tmp_path, base_url="", log_level="INFO", run_scheduler=False)


def test_healthz_ok(tmp_path):
    with TestClient(create_app(make_config(tmp_path))) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_healthz_unavailable_when_database_fails(tmp_path):
    cfg = make_config(tmp_path)
    with TestClient(create_app(cfg)) as client:
        cfg.db_path.unlink()
        # a directory in place of the file makes any query fail
        cfg.db_path.mkdir()
        r = client.get("/healthz")
    assert r.status_code == 503


def test_config_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://example.org/")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    cfg = Config.from_env()
    assert cfg.data_dir == tmp_path
    assert cfg.base_url == "https://example.org"
    assert cfg.log_level == "DEBUG"


def test_server_exits_with_readable_error_on_newer_database(tmp_path):
    migrate(tmp_path / "podcaster.db", tmp_path / "backups")
    conn = connect(tmp_path / "podcaster.db")
    conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (99, 'x')")
    conn.close()
    r = subprocess.run(
        [sys.executable, "-m", "app"], cwd=SERVER_DIR, env={"DATA_DIR": str(tmp_path), "PATH": "", "PYTHONPATH": str(SERVER_DIR.parent / "shared")},
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 1
    assert "newer than this code" in r.stdout
