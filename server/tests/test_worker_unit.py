import json
import logging
import os
import random
import stat
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

from pn_shared.logs import RedactFilter
from pn_worker import tools
from pn_worker.client import ApiClient, AuthError, ServerError
from pn_worker.config import Config, ConfigError
from pn_worker.main import backoff_delays, hello_request, run

ROOT = Path(__file__).resolve().parents[2]
HELLO_BODY = {"api_version": 1, "server_time": "2026-11-05T12:00:00Z", "worker": {"id": 1, "name": "w", "tier": 1},
              "pacing": {"pause_min_s": 5, "pause_max_s": 20, "downloads_per_hour": 3}, "lease_seconds": 300,
              "ping_interval_seconds": 30}


# --- config -----------------------------------------------------------------------------

def test_config_from_env(monkeypatch):
    monkeypatch.setenv("SERVER_URL", "https://pod.example.org/")
    monkeypatch.setenv("WORKER_TOKEN", " pnw_abc ")
    cfg = Config.from_env()
    assert cfg.server_url == "https://pod.example.org" and cfg.token == "pnw_abc" and cfg.log_level == "INFO"


@pytest.mark.parametrize("url,token", [("", "t"), ("pod.example.org", "t"), ("ftp://x", "t"), ("https://x", ""), ("", "")])
def test_config_errors(monkeypatch, url, token):
    monkeypatch.setenv("SERVER_URL", url)
    monkeypatch.setenv("WORKER_TOKEN", token)
    with pytest.raises(ConfigError):
        Config.from_env()


def test_missing_configuration_exits_with_message(tmp_path):
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": f"{ROOT / 'worker'}{os.pathsep}{ROOT / 'shared'}"}
    r = subprocess.run([sys.executable, "-m", "pn_worker"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "SERVER_URL" in r.stdout


# --- backoff ----------------------------------------------------------------------------

def test_backoff_grows_is_capped_and_jittered():
    delays = backoff_delays(2.0, 60.0, random.Random(1))
    seen = [next(delays) for _ in range(10)]
    bases = [2, 4, 8, 16, 32, 60, 60, 60, 60, 60]
    assert all(b * 0.8 <= d <= b * 1.2 for b, d in zip(bases, seen))
    assert max(seen) <= 72 and seen[0] < seen[4]


# --- client ----------------------------------------------------------------------------

def make_client(handler):
    return ApiClient("https://pod.example.org", "pnw_secret-token", transport=httpx.MockTransport(handler))


def test_client_sends_token_and_parses_hello():
    seen = {}

    def handler(request):
        seen["auth"], seen["url"], seen["ua"] = request.headers["authorization"], str(request.url), request.headers["user-agent"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=HELLO_BODY)

    reply = make_client(handler).hello(hello_request({}))   # explicit: do not depend on the tools installed here
    assert seen["auth"] == "Bearer pnw_secret-token" and seen["url"] == "https://pod.example.org/api/worker/hello"
    assert seen["ua"].startswith("podcaster-ng-worker/") and "worker_version" in seen["body"]
    assert "ytdlp_version" not in seen["body"]  # unset tools are not sent
    assert reply.worker.name == "w" and reply.ping_interval_seconds == 30


def test_client_ping():
    reply = make_client(lambda r: httpx.Response(200, json={"ok": True, "server_time": "2026-11-05T12:00:00Z"})).ping()
    assert reply.ok


@pytest.mark.parametrize("status", [401, 403])
def test_client_auth_errors(status):
    with pytest.raises(AuthError) as exc:
        make_client(lambda r: httpx.Response(status, json={"detail": "nope"})).ping()
    assert exc.value.status == status and "nope" in str(exc.value) and "pnw_secret-token" not in str(exc.value)


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(429), httpx.Response(502, text="bad gateway"),
                                      httpx.Response(200, text="<html>not json</html>"),
                                      httpx.Response(200, json={"unexpected": True})])
def test_client_server_errors(response):
    with pytest.raises(ServerError) as exc:
        make_client(lambda r: response).ping()
    assert "pnw_secret-token" not in str(exc.value)


def test_client_network_errors_become_server_errors():
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    def slow(request):
        raise httpx.ReadTimeout("timed out")

    for handler in (refuse, slow):
        with pytest.raises(ServerError):
            make_client(handler).ping()


def test_client_has_timeouts():
    from pn_worker.client import TIMEOUT
    assert all(v is not None for v in (TIMEOUT.connect, TIMEOUT.read, TIMEOUT.write, TIMEOUT.pool))


# --- main loop ----------------------------------------------------------------------------

class FakeClient:
    def __init__(self, script):
        self.script = list(script)  # items: "ok" or an exception to raise
        self.calls = []
        self.closed = False

    def _next(self, name):
        self.calls.append(name)
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, BaseException):
            raise item
        return item

    def hello(self, request):
        self._next("hello")
        from pn_shared.api import HelloResponse
        return HelloResponse.model_validate(HELLO_BODY)

    def ping(self):
        self._next("ping")

    def close(self):
        self.closed = True


def run_script(script, steps, tmp_path, **cfg):
    """Run the loop with fake time: ``steps`` sleeps are recorded, then the loop is stopped."""
    stop = threading.Event()
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= steps:
            stop.set()

    client = FakeClient(script)
    config = Config(server_url="https://x", token="pnw_t", alive_file=str(tmp_path / "alive"), **cfg)
    run(config, stop, client=client, sleep=fake_sleep, found={})
    return client, sleeps


def test_loop_hello_then_pings(tmp_path):
    client, sleeps = run_script([], 4, tmp_path)
    assert client.calls == ["hello", "ping", "ping", "ping"] and sleeps == [30.0] * 4
    assert client.closed and (tmp_path / "alive").exists()


def test_loop_survives_downtime_with_growing_backoff_and_says_hello_again(tmp_path):
    down = ServerError("connection refused")
    client, sleeps = run_script([down, down, down, "ok", "ok", down, "ok"], 8, tmp_path)
    assert client.calls[:4] == ["hello"] * 4          # keeps trying to say hello while the server is away
    assert sleeps[0] < sleeps[1] < sleeps[2] and sleeps[2] > 6    # backoff grows
    assert client.calls[4] == "ping"                  # connected: only pings now
    assert client.calls[5:7] == ["ping", "hello"]     # after a failed ping the next step is a fresh hello


def test_loop_backoff_resets_after_reconnect(tmp_path):
    down = ServerError("x")
    _, sleeps = run_script([down, down, down, "ok", down, "ok"], 6, tmp_path, backoff_start=1.0)
    first_outage, second_outage = sleeps[2], sleeps[4]
    assert second_outage < first_outage              # a new outage starts from the small delay again


def test_loop_retries_when_token_is_rejected(tmp_path):
    client, sleeps = run_script([AuthError(401, "revoked"), AuthError(403, "off"), "ok"], 3, tmp_path)
    assert client.calls[:3] == ["hello", "hello", "hello"] and sleeps[:2] == [60.0, 60.0]


def test_loop_catches_unexpected_errors(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    client, sleeps = run_script([RuntimeError("bug"), ValueError("bug2"), "ok"], 3, tmp_path)
    assert client.calls[:3] == ["hello"] * 3 and "Unexpected error" in caplog.text


def test_loop_stops_cleanly_when_asked(tmp_path):
    stop = threading.Event()
    stop.set()
    client = FakeClient([])
    run(Config(server_url="https://x", token="t", alive_file=str(tmp_path / "a")), stop, client=client, found={})
    assert client.calls == [] and client.closed


def test_loop_logs_never_contain_the_token(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    run_script([ServerError("boom"), AuthError(401, "revoked"), "ok"], 4, tmp_path)
    assert "pnw_t" not in caplog.text


def test_loop_uses_configured_interval(tmp_path):
    _, sleeps = run_script([], 2, tmp_path, ping_interval=5.0)
    assert sleeps == [5.0, 5.0]


# --- tools / logging ---------------------------------------------------------------------------

def fake_binary(directory: Path, name: str, output: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\necho '{output}'\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_tool_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert tools.detect() == {}
    fake_binary(tmp_path, "yt-dlp", "2026.10.15")
    fake_binary(tmp_path, "ffmpeg", "ffmpeg version 7.0.2-static https://johnvansickle.com")
    fake_binary(tmp_path, "deno", "deno 2.1.4 (stable)")
    assert tools.detect() == {"ytdlp": "2026.10.15", "ffmpeg": "7.0.2-static", "deno": "2.1.4"}
    req = hello_request()
    assert req.capabilities == ["deno", "ffmpeg", "scan", "ytdlp"] and req.ytdlp_version == "2026.10.15"


def test_hello_without_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    req = hello_request()
    assert req.capabilities == [] and req.ytdlp_version is None


def test_redact_filter_scrubs_secrets():
    f = RedactFilter(["pnw_secret"])
    rec = logging.LogRecord("x", logging.INFO, "p", 1, "token is %s here", ("pnw_secret",), None)
    f.filter(rec)
    assert rec.getMessage() == "token is *** here"


def test_loop_reports_dropped_tasks_with_the_next_claim(tmp_path, monkeypatch):
    """A task whose result could not be delivered is announced to the server at the next successful claim, once."""
    from types import SimpleNamespace
    import pn_worker.main as main

    claims, outcomes = [], iter([False, True, True])

    class Client(FakeClient):
        def claim(self, request):
            claims.append(list(request.dropped))
            return SimpleNamespace(task=SimpleNamespace(id=7 + len(claims), type="scan"), retry_after_seconds=1)

    monkeypatch.setattr(main, "run_task", lambda client, task, config: next(outcomes))
    stop, sleeps = threading.Event(), []
    monkeypatch.setattr(main, "hello_request", lambda found=None: None)
    client = Client([])
    config = Config(server_url="https://x", token="pnw_t", alive_file=str(tmp_path / "alive"))

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 4:
            stop.set()

    run(config, stop, client=client, sleep=fake_sleep, found={"ytdlp": "1"})
    assert claims[:3] == [[], [8], []]                      # task 8 was dropped, told once, then forgotten
