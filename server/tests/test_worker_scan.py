import json
import threading
import time

import httpx
import pytest

from pn_shared.api import ClaimResponse, TaskInfo
from pn_worker import scan as scan_module
from pn_worker.classify import classify
from pn_worker.client import ApiClient
from pn_worker.config import Config
from pn_worker.main import idle_delay, supported_types
from pn_worker.scan import ScanFailure, ScanLost, build_command, parse_entries, scan_playlist
from pn_worker.tasks import run_task

from fake_ytdlp import environment, install

ENTRIES = [
    {"id": "aaaaaaaaaaa", "title": "Episode 1", "upload_date": "20260101", "live_status": "not_live"},
    {"id": "bbbbbbbbbbb", "title": "Premiere", "live_status": "is_upcoming", "release_timestamp": 1893456000},
    {"id": "ccccccccccc", "title": "[Private video]"},
    {"id": "ddddddddddd", "title": "[Deleted video]"},
    None, {"title": "no id"}, {"id": "bad id!", "title": "invalid"},
    {"id": "eeeeeeeeeee", "title": "Episode 2"},
]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    install(tmp_path / "bin", ENTRIES)
    for k, v in environment(tmp_path / "bin").items():
        monkeypatch.setenv(k, v)
    return tmp_path / "bin"


# --- error classification --------------------------------------------------------------------------------

@pytest.mark.parametrize("stderr,expected", [
    ("ERROR: [youtube] x: Sign in to confirm you’re not a bot", "bot_check"),
    ("HTTP Error 429: Too Many Requests", "bot_check"),
    ("ERROR: This video is private", "private"), ("The playlist is private", "private"),
    ("ERROR: Premieres in 2 hours", "not_yet_available"), ("This live event will begin in 3 hours", "not_yet_available"),
    ("Video unavailable. This video has been removed by the uploader", "removed"), ("The playlist does not exist.", "removed"),
    ("The read operation timed out", "timeout"),
    ("Temporary failure in name resolution", "network"), ("Connection refused", "network"), ("HTTP Error 503", "network"),
    ("Traceback (most recent call last): boom", "internal"), ("", "internal"),
])
def test_error_classification(stderr, expected):
    assert classify(stderr) == expected


# --- command and parsing ---------------------------------------------------------------------------------

def test_command_is_metadata_only_and_passes_options():
    cmd = build_command("/usr/bin/yt-dlp", "https://www.youtube.com/playlist?list=PL1", 20, "http://proxy:3128")
    assert cmd[0] == "/usr/bin/yt-dlp" and "--flat-playlist" in cmd and "--dump-single-json" in cmd
    assert cmd[cmd.index("--playlist-end") + 1] == "20" and cmd[cmd.index("--proxy") + 1] == "http://proxy:3128"
    assert "--socket-timeout" in cmd and "--no-cache-dir" in cmd
    assert "--download" not in cmd and cmd[-2:] == ["--", "https://www.youtube.com/playlist?list=PL1"]
    assert "--proxy" not in build_command("y", "u", 5, None)


def test_url_starting_with_a_dash_cannot_become_an_option():
    assert build_command("y", "--exec evil", 1, None)[-2:] == ["--", "--exec evil"]


def test_parse_entries_skips_unusable_ones():
    out = parse_entries(json.dumps({"entries": ENTRIES}))
    assert [v.id for v in out] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "eeeeeeeeeee"]
    assert out[0].upload_date == "20260101" and out[1].live_status == "is_upcoming" and out[1].release_timestamp == 1893456000


@pytest.mark.parametrize("bad", ["", "not json", "[1, 2]", "null"])
def test_parse_entries_rejects_garbage(bad):
    with pytest.raises(ScanFailure) as exc:
        parse_entries(bad)
    assert exc.value.error_class == "incomplete"


def test_parse_entries_without_entries_is_an_empty_playlist():
    assert parse_entries(json.dumps({"_type": "playlist"})) == []


# --- running the (fake) yt-dlp ---------------------------------------------------------------------------------

def run(url, depth=20, timeout=10, lost=None, proxy=None):
    return scan_playlist(url, depth, timeout=timeout, proxy=proxy, lost=lost or threading.Event())


def test_scan_returns_videos_and_passes_arguments(fake):
    videos = run("https://www.youtube.com/playlist?list=ok", depth=7, proxy="http://p:1")
    assert [v.id for v in videos] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "eeeeeeeeeee"]
    args = (fake / "args.txt").read_text()
    assert "--playlist-end 7" in args and "--proxy http://p:1" in args and "list=ok" in args


@pytest.mark.parametrize("word,expected", [("bot", "bot_check"), ("private", "private"), ("gone", "removed"),
                                           ("net", "network"), ("garbage", "incomplete"), ("crash", "internal")])
def test_scan_failures_are_classified(fake, word, expected):
    with pytest.raises(ScanFailure) as exc:
        run(f"https://www.youtube.com/playlist?list={word}")
    assert exc.value.error_class == expected and exc.value.message


def test_hard_time_limit_kills_the_process(fake):
    started = time.monotonic()
    with pytest.raises(ScanFailure) as exc:
        run("https://www.youtube.com/playlist?list=hang", timeout=1.2)
    assert exc.value.error_class == "timeout" and time.monotonic() - started < 6


def test_lost_lease_aborts_the_scan(fake):
    lost = threading.Event()
    threading.Timer(0.6, lost.set).start()
    started = time.monotonic()
    with pytest.raises(ScanLost):
        run("https://www.youtube.com/playlist?list=hang", timeout=60, lost=lost)
    assert time.monotonic() - started < 6


def test_missing_binary_is_an_internal_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ScanFailure) as exc:
        run("https://x/ok")
    assert exc.value.error_class == "internal" and "not installed" in exc.value.message


def test_supported_types_depend_on_tools():
    assert supported_types({}) == [] and supported_types({"ytdlp": "1"}) == ["scan"]
    assert supported_types({"ffmpeg": "7"}) == []          # downloads need a later step anyway


def test_idle_delay_grows_and_is_capped():
    assert [idle_delay(15, n) for n in range(6)] == [15, 22.5, 33.75, 50.625, 50.625, 50.625]
    assert idle_delay(100, 3) == 60


# --- run_task: reporting -------------------------------------------------------------------------------------------

def task_info(url="https://www.youtube.com/playlist?list=ok", lease=300):
    return TaskInfo.model_validate({
        "id": 7, "type": "scan", "podcast": {"id": "abgt", "name": "ABGT", "playlist_url": url, "scan_depth": 20},
        "attempt": 1, "lease_seconds": lease, "lease_until": "2026-11-05T12:05:00Z",
        "pacing": {"pause_min_s": 5, "pause_max_s": 20, "downloads_per_hour": 3}})


class Recorder:
    def __init__(self, heartbeat_status=200):
        self.calls, self.heartbeat_status = [], heartbeat_status

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("/heartbeat"):
            if self.heartbeat_status != 200:
                return httpx.Response(self.heartbeat_status, json={"detail": "not yours"})
            return httpx.Response(200, json={"lease_until": "2026-11-05T12:10:00Z"})
        if request.url.path.endswith("/scan-result"):
            return httpx.Response(200, json={"created": 2, "known": 1, "skipped": 0})
        if request.url.path.endswith("/fail"):
            return httpx.Response(200, json={"state": "pending", "not_before": "2026-11-05T12:01:00Z"})
        return httpx.Response(404)

    def paths(self):
        return [(m, p.rsplit("/", 1)[-1]) for m, p, _ in self.calls if not p.endswith("/heartbeat")]


def make(recorder):
    return ApiClient("https://pod.example.org", "pnw_t", transport=httpx.MockTransport(recorder))


def config(tmp_path, **kw):
    return Config(server_url="https://x", token="pnw_t", alive_file=str(tmp_path / "a"), **kw)


def test_successful_scan_is_reported(fake, tmp_path):
    rec = Recorder()
    run_task(make(rec), task_info(), config(tmp_path))
    assert rec.paths() == [("POST", "scan-result")]
    body = [b for m, p, b in rec.calls if p.endswith("/scan-result")][0]
    assert [v["id"] for v in body["videos"]] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "eeeeeeeeeee"]


@pytest.mark.parametrize("word,expected", [("bot", "bot_check"), ("private", "private"), ("net", "network"), ("crash", "internal")])
def test_failed_scan_is_reported_with_class_and_text(fake, tmp_path, word, expected):
    rec = Recorder()
    run_task(make(rec), task_info(f"https://www.youtube.com/playlist?list={word}"), config(tmp_path))
    assert rec.paths() == [("POST", "fail")]
    body = [b for m, p, b in rec.calls if p.endswith("/fail")][0]
    assert body["error_class"] == expected and body["message"]


def test_unexpected_exception_is_reported_as_internal_failure(fake, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("something broke")

    monkeypatch.setattr("pn_worker.tasks.scan_playlist", boom)
    rec = Recorder()
    run_task(make(rec), task_info(), config(tmp_path))
    body = [b for m, p, b in rec.calls if p.endswith("/fail")][0]
    assert body["error_class"] == "internal" and "something broke" in body["message"]


def test_hung_scan_is_stopped_and_reported_as_timeout(fake, tmp_path):
    rec = Recorder()
    run_task(make(rec), task_info("https://www.youtube.com/playlist?list=hang"), config(tmp_path, scan_timeout=1.0))
    body = [b for m, p, b in rec.calls if p.endswith("/fail")][0]
    assert body["error_class"] == "timeout"


def test_heartbeats_are_sent_while_the_task_runs(fake, tmp_path):
    rec = Recorder()
    run_task(make(rec), task_info("https://www.youtube.com/playlist?list=hang"), config(tmp_path, scan_timeout=2.0, heartbeat_interval=0.3))
    beats = [c for c in rec.calls if c[1].endswith("/7/heartbeat")]
    assert len(beats) >= 3


def test_losing_the_lease_aborts_the_scan_and_reports_nothing(fake, tmp_path):
    rec = Recorder(heartbeat_status=409)
    started = time.monotonic()
    run_task(make(rec), task_info("https://www.youtube.com/playlist?list=hang"), config(tmp_path, scan_timeout=60, heartbeat_interval=0.3))
    assert time.monotonic() - started < 8
    assert rec.paths() == []                       # no result, no fail: the task is not ours any more


def test_task_runner_survives_server_errors_when_reporting(fake, tmp_path):
    def handler(request):
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_until": "x"})
        raise httpx.ConnectError("down")

    run_task(ApiClient("https://x", "t", transport=httpx.MockTransport(handler)), task_info(), config(tmp_path))  # must not raise


def test_download_tasks_are_refused_for_now(fake, tmp_path):
    rec = Recorder()
    info = task_info().model_copy(update={"type": "download", "video_id": "aaaaaaaaaaa"})
    run_task(make(rec), info, config(tmp_path))
    body = [b for m, p, b in rec.calls if p.endswith("/fail")][0]
    assert body["error_class"] == "internal" and "download" in body["message"]
