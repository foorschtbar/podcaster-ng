"""Machine details and activity of workers: collected by the worker, stored by the server, shown on the Workers page."""

import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app import queue, workers
from app.db import connect
from app.main import create_app
from pn_shared.api import Device
from pn_worker import sysinfo
from pn_worker.client import ApiClient, ServerError

from helpers import csrf_of, do_login, make_config
from qhelpers import NOW, add_download, add_podcast, add_worker as add_queue_worker, claim

HELLO = {"worker_version": "1.0.0", "capabilities": []}
DEVICE = {"hostname": "homesrv01", "os": "Debian GNU/Linux 13 (trixie)", "kernel": "6.8.0", "arch": "x86_64", "cpus": 4.0,
          "mem_total_mb": 16000, "mem_available_mb": 9000, "disk_total_mb": 500_000, "disk_free_mb": 120_000,
          "uptime_s": 3 * 86400, "load1": 0.42, "local_ip": "192.168.1.20"}


# --- collecting (worker side) -----------------------------------------------------------------------------------------

def test_os_name_reads_the_pretty_name(tmp_path):
    f = tmp_path / "os-release"
    f.write_text('NAME="Debian GNU/Linux"\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    assert sysinfo.os_name(str(f)) == "Debian GNU/Linux 13 (trixie)"
    assert sysinfo.os_name(str(tmp_path / "missing"))                       # falls back to the system name


def test_meminfo_and_uptime_parse_proc_files(tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal:       16384000 kB\nMemFree: 1 kB\nMemAvailable:    8192000 kB\n")
    assert sysinfo.meminfo(str(f)) == (16000, 8000)
    assert sysinfo.meminfo(str(tmp_path / "missing")) == (None, None)
    (tmp_path / "up").write_text("12345.67 54321.0\n")
    assert sysinfo.uptime(str(tmp_path / "up")) == 12345
    (tmp_path / "up").write_text("junk")
    assert sysinfo.uptime(str(tmp_path / "up")) is None


def test_a_container_memory_limit_wins(tmp_path):
    (tmp_path / "memory.max").write_text(str(1024 << 20))
    (tmp_path / "memory.current").write_text(str(300 << 20))
    assert sysinfo.cgroup_memory(str(tmp_path)) == (1024, 724)
    (tmp_path / "memory.max").write_text("max\n")                              # no limit
    assert sysinfo.cgroup_memory(str(tmp_path)) is None


def test_cpu_quota_gives_fractions_of_cores(tmp_path):
    (tmp_path / "cpu.max").write_text("200000 100000\n")
    assert sysinfo.cpu_count(str(tmp_path)) == 2.0
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert sysinfo.cpu_count(str(tmp_path)) >= 1.0                              # the usable cores


def test_collect_reports_what_exists_and_never_raises(tmp_path):
    device = sysinfo.collect(str(tmp_path), "http://127.0.0.1:8080", probe=lambda url: "10.0.0.5")
    assert device.local_ip == "10.0.0.5" and device.arch and device.disk_total_mb and device.disk_free_mb is not None
    dumped = device.model_dump(exclude_none=True)
    assert all(v is not None for v in dumped.values())
    # a work directory that does not exist, and a probe that explodes: still fine
    def boom(url):
        raise RuntimeError("no network")
    assert sysinfo.collect(str(tmp_path / "nope"), None, probe=boom).disk_total_mb is not None


def test_local_ip_without_a_server_address():
    assert sysinfo.local_ip(None) is None and sysinfo.local_ip("not a url") is None


# --- the worker API ---------------------------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="https://pod.example.org") as c:
        c.db = c.app.state.config.db_path
        conn = connect(c.db)
        c.worker_id, token = workers.create(conn, {"name": "home", "tier": 2, "active": 1, "pause_min_s": None,
                                                    "pause_max_s": None, "downloads_per_hour": None})
        conn.close()
        c.bearer = {"Authorization": f"Bearer {token}"}
        yield c


def stored(api):
    conn = connect(api.db)
    try:
        return conn.execute("SELECT * FROM workers WHERE id = ?", (api.worker_id,)).fetchone()
    finally:
        conn.close()


def test_hello_stores_the_device_and_the_address(api):
    assert api.post("/api/worker/hello", json={**HELLO, "device": DEVICE}, headers=api.bearer).status_code == 200
    row = stored(api)
    assert workers.device_of(row) == DEVICE and row["device_at"] and row["last_ip"]


def test_hello_without_a_device_still_works(api):
    assert api.post("/api/worker/hello", json=HELLO, headers=api.bearer).status_code == 200
    assert workers.device_of(stored(api)) == {}


def test_status_updates_the_device_and_counts_as_seen(api):
    r = api.post("/api/worker/status", json={**DEVICE, "mem_available_mb": 1234}, headers=api.bearer)
    assert r.status_code == 200 and r.json()["ok"]
    row = stored(api)
    assert workers.device_of(row)["mem_available_mb"] == 1234 and row["last_seen"] and row["device_at"]


@pytest.mark.parametrize("bad", [{"cpus": -1}, {"mem_total_mb": "lots"}, {"hostname": "x" * 101}, {"load1": 10**9},
                                 {"disk_free_mb": -5}])
def test_status_rejects_implausible_values(api, bad):
    assert api.post("/api/worker/status", json=bad, headers=api.bearer).status_code == 422
    assert workers.device_of(stored(api)) == {}


def test_status_needs_a_valid_token(api):
    assert api.post("/api/worker/status", json=DEVICE).status_code == 401
    assert api.post("/api/worker/status", json=DEVICE, headers={"Authorization": "Bearer pnw_nope"}).status_code == 401


def test_device_text_is_cleaned(api):
    api.post("/api/worker/status", json={"hostname": "pi\x00\x07-home", "os": "token=abc123secret"}, headers=api.bearer)
    device = workers.device_of(stored(api))
    assert "\x00" not in device["hostname"] and "abc123secret" not in device["os"]


def test_the_address_is_what_the_server_sees(api):
    api.get("/api/worker/ping", headers={**api.bearer, "X-Forwarded-For": "203.0.113.9"})
    assert stored(api)["last_ip"]                                               # testclient peer or the forwarded address


def test_heartbeat_can_carry_a_snapshot(api):
    conn = connect(api.db)
    add_podcast(conn, "abgt")
    add_download(conn, "abgt", "vid1")
    from app import settings
    got = queue.claim(conn, dict(workers.get(conn, api.worker_id)), ["download"], settings.get_all(conn), 300, queue.utcnow())
    conn.close()
    r = api.post(f"/api/worker/tasks/{got.task['id']}/heartbeat", json={"progress": "10%", "device": DEVICE}, headers=api.bearer)
    assert r.status_code == 200 and workers.device_of(stored(api))["hostname"] == "homesrv01"


# --- the worker client ------------------------------------------------------------------------------------------------

def client_for(handler):
    return ApiClient("https://pod.example.org", "pnw_t", transport=httpx.MockTransport(handler))


def test_client_sends_the_status_and_stops_on_an_old_server():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(404 if len(calls) > 1 else 200, json={"ok": True, "server_time": "2026-11-05T12:00:00Z"})

    c = client_for(handler)
    assert c.status(Device(hostname="h")).ok
    assert c.status(Device(hostname="h")) is None and c.status_supported is False      # 404: an older server
    assert c.status(Device(hostname="h")) is None and len(calls) == 2                   # and it is not asked again


def test_client_status_raises_for_other_errors():
    with pytest.raises(ServerError):
        client_for(lambda r: httpx.Response(500)).status(Device())


def test_heartbeats_carry_a_snapshot_at_most_every_half_minute(monkeypatch):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"lease_until": "2026-11-05T12:05:00Z"})

    c = client_for(handler)
    c.device_source = lambda: Device(hostname="h")
    now = [100.0]
    monkeypatch.setattr("pn_worker.client.time.monotonic", lambda: now[0])
    for moment in (100.0, 110.0, 131.0, 140.0):
        now[0] = moment
        c.heartbeat(1, "x")
    assert ["device" in b for b in bodies] == [True, False, True, False]


# --- the main loop ----------------------------------------------------------------------------------------------------

def test_the_loop_sends_the_device_with_hello_and_a_status_every_interval(tmp_path, monkeypatch):
    import pn_worker.main as main
    from pn_worker.config import Config
    from test_worker_unit import HELLO_BODY

    seen = {"hello": None, "status": 0}

    class Client:
        device_source = None
        status_supported = True

        def hello(self, request):
            seen["hello"] = request
            from pn_shared.api import HelloResponse
            return HelloResponse.model_validate(HELLO_BODY)

        def status(self, device):
            seen["status"] += 1

        def ping(self):
            pass

        def close(self):
            pass

    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(main.sysinfo, "collect", lambda *a, **k: Device(hostname="box"))
    stop = threading.Event()

    def fake_sleep(seconds):
        now[0] += 31
        if now[0] > 1000 + 31 * 4:
            stop.set()

    config = Config(server_url="https://x", token="pnw_t", alive_file=str(tmp_path / "alive"))
    c = Client()
    main.run(config, stop, client=c, sleep=fake_sleep, found={})
    assert seen["hello"].device.hostname == "box" and seen["status"] >= 2 and c.device_source is not None


# --- activity ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    from qhelpers import make_db
    c = connect(make_db(tmp_path))
    add_podcast(c, "abgt")
    yield c
    c.close()


def test_activity_shows_the_current_and_the_last_task(conn):
    w1, w2 = add_queue_worker(conn, "busy"), add_queue_worker(conn, "resting")
    add_download(conn, "abgt", "vid1", title="First")
    add_download(conn, "abgt", "vid2", title="Second")
    first, second = claim(conn, w1), claim(conn, w2)                      # one task each
    queue.fail(conn, second.task["id"], w2["id"], "network", "boom", now=NOW)
    conn.execute("UPDATE tasks SET progress = '42%' WHERE id = ?", (first.task["id"],))
    act = queue.activity(conn)
    assert act[w1["id"]]["current"]["title"] == "First" and act[w1["id"]]["current"]["progress"] == "42%"
    assert act[w1["id"]]["current"]["since"] and act[w1["id"]]["last"] is None
    assert act[w2["id"]]["current"] is None and act[w2["id"]]["last"]["result"] == "failed"
    assert act[w2["id"]]["last"]["error_class"] == "network" and act[w2["id"]]["last"]["title"] == "Second"


# --- the page ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def ui(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="https://testserver", follow_redirects=False) as client:
        do_login(client)
        client.db = client.app.state.config.db_path
        c = connect(client.db)
        add_podcast(c, "abgt")
        client.worker = add_queue_worker(c, "homesrv01", tier=2)
        c.execute("UPDATE workers SET last_seen = ?", (workers._stamp(),))
        c.close()
        yield client


def page(c, path="/workers"):
    return c.get(path).text


def test_the_page_shows_device_details_and_the_addresses(ui):
    conn = connect(ui.db)
    workers.record_device(conn, ui.worker["id"], DEVICE)
    workers.touch(conn, ui.worker["id"], ip="203.0.113.9")
    conn.close()
    html = page(ui)
    for expected in ("Debian GNU/Linux 13 (trixie)", "kernel 6.8.0", "x86_64", "homesrv01", "4 core(s)", "load 0.42",
                     "8.8 GB free of 15.6 GB", "117 GB free of 488 GB", "203.0.113.9", "seen by the server", "192.168.1.20",
                     "(local)", "3 days", "Reported"):
        assert expected in html, expected


def test_the_page_shows_idle_and_the_last_task(ui):
    conn = connect(ui.db)
    add_download(conn, "abgt", "vid1", title="Group Therapy 700")
    got = claim(conn, ui.worker)
    queue.fail(conn, got.task["id"], ui.worker["id"], "bot_check", "blocked")
    conn.close()
    html = page(ui)
    assert "idle" in html and "download failed (bot_check)" in html and "Group Therapy 700" in html


def test_the_page_shows_what_a_worker_is_working_on(ui):
    conn = connect(ui.db)
    add_download(conn, "abgt", "vid1", title="Group Therapy 701")
    got = claim(conn, ui.worker)
    conn.execute("UPDATE tasks SET progress = 'downloading 42%' WHERE id = ?", (got.task["id"],))
    conn.close()
    html = page(ui)
    assert "working" in html and "Downloading" in html and "Group Therapy 701" in html and "downloading 42%" in html
    assert f'/tasks/{got.task["id"]}' in html and "idle" not in html


def test_a_worker_without_details_still_renders(ui):
    html = page(ui)
    assert "homesrv01" in html and "<dl" not in html


def test_the_cards_refresh_by_themselves(ui):
    assert 'hx-get="/workers/cards"' in page(ui) and 'hx-trigger="every 10s"' in page(ui)
    part = page(ui, "/workers/cards")
    assert part.lstrip().startswith('<div id="worker-cards"') and "<html" not in part and "homesrv01" in part


def test_the_cards_need_login(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="https://testserver", follow_redirects=False) as anon:
        assert anon.get("/workers/cards").status_code == 303


def test_device_text_is_escaped_on_the_page(ui):
    conn = connect(ui.db)
    workers.record_device(conn, ui.worker["id"], {"hostname": "<script>alert(1)</script>"})
    conn.close()
    assert "<script>alert(1)</script>" not in page(ui) and "&lt;script&gt;" in page(ui)


def test_size_and_span_filters():
    from app.workers_ui import size_mb, span
    assert [size_mb(n) for n in (None, 0, 812, 1024, 3482, 120_000, 2_000_000)] == ["", "0 MB", "812 MB", "1.0 GB", "3.4 GB", "117 GB", "1.9 TB"]
    assert [span(n) for n in (None, 5, 300, 7200, 3 * 86400)] == ["", "5 s", "5 min", "2 h", "3 days"]
