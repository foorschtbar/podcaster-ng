import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from pn_shared.api import TaskInfo
from pn_shared.chapters import build_chapters_json
from pn_worker import media
from pn_worker.classify import classify, wait_seconds
from pn_worker.client import ApiClient
from pn_worker.config import Config
from pn_worker.download import Progress, build_command, run_download
from pn_worker.main import supported_types
from pn_worker.process import Failure, Lost
from pn_worker.tasks import run_task

from fake_ytdlp import environment, install

BOT = ("ERROR: [youtube] zPMoD167_14: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the "
       "authentication. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass "
       "cookies. Also see  https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies  for tips on effectively "
       "exporting YouTube cookies")

# Recorded yt-dlp error strings (see the error-class table in classify.py)
RECORDED = [
    (BOT, "bot_check"),
    ("ERROR: [youtube] Wi5UmtcTyBQ: This live event will begin in 3 hours.", "not_yet_available"),
    ("ERROR: [youtube] abc: Premieres in 2 hours", "not_yet_available"),
    ("ERROR: [youtube] abc: Premieres in 45 minutes", "not_yet_available"),
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video", "private"),
    ("ERROR: [youtube] abc: This video is private", "private"),
    ("ERROR: [youtube] abc: Join this channel to get access to members-only content like this video", "private"),
    ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", "removed"),
    ("ERROR: [youtube] abc: Video unavailable", "removed"),
    ("ERROR: [youtube] abc: Video unavailable. The uploader has not made this video available in your country", "removed"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "bot_check"),
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "bot_check"),
    ("ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution> (caused by URLError(...))", "network"),
    ("ERROR: Unable to download webpage: <urlopen error [Errno 111] Connection refused>", "network"),
    ("ERROR: Unable to download webpage: HTTPSConnectionPool(host='www.youtube.com', port=443): Read timed out. (read timeout=30.0)", "timeout"),
    ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.", "internal"),
    ("ERROR: Postprocessing: ffmpeg not found. Please install or provide the path using --ffmpeg-location", "internal"),
    ("ERROR: [youtube] abc: Requested format is not available. Use --list-formats for a list of available formats", "internal"),
    ("Traceback (most recent call last):\n  File \"yt_dlp/YoutubeDL.py\", line 1\nKeyError: 'x'", "internal"),
    ("", "internal"),
]


@pytest.mark.parametrize("text,expected", RECORDED)
def test_recorded_yt_dlp_errors(text, expected):
    assert classify(text) == expected


@pytest.mark.parametrize("text,seconds", [
    ("This live event will begin in 3 hours.", 3 * 3600 + 300), ("Premieres in 45 minutes", 45 * 60 + 300),
    ("Premieres in 2 days", 2 * 86400 + 300), ("will begin in 1 hour", 3600 + 300), ("starts in 30 seconds", 330),
    ("Sign in to confirm you’re not a bot", None), ("", None),
])
def test_wait_time_from_premiere_message(text, seconds):
    assert wait_seconds(text) == seconds


# --- supported types / command -----------------------------------------------------------------------------------------

def test_download_needs_ytdlp_ffmpeg_and_ffprobe():
    assert supported_types({"ytdlp": "1"}) == ["scan"]
    assert supported_types({"ytdlp": "1", "ffmpeg": "7"}) == ["scan"]
    assert supported_types({"ytdlp": "1", "ffmpeg": "7", "ffprobe": "7"}) == ["scan", "download"]
    assert supported_types({"ffmpeg": "7", "ffprobe": "7"}) == []


def cfg(tmp_path, **kw):
    return Config(server_url="https://x", token="pnw_t", alive_file=str(tmp_path / "alive"), work_dir=str(tmp_path / "work"), **kw)


def test_command_follows_the_old_recipe(tmp_path):
    cmd = build_command("yt-dlp", "aaaaaaaaaaa", cfg(tmp_path))
    joined = " ".join(cmd)
    for part in ("-f m4a/bestaudio/best", "-x", "--audio-format m4a", "--write-info-json", "--write-thumbnail", "--no-playlist",
                 "--socket-timeout 30", "-o audio.%(ext)s", "-o infojson:info", "-o thumbnail:thumb", "--ignore-config"):
        assert part in joined, part
    assert cmd[-2:] == ["--", "https://www.youtube.com/watch?v=aaaaaaaaaaa"]
    assert "--proxy" not in cmd and "--cookies" not in cmd


def test_proxy_and_cookies_are_passed_when_configured(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("x")
    cmd = build_command("yt-dlp", "aaaaaaaaaaa", cfg(tmp_path, ytdlp_proxy="http://vpn:3128", cookies_file=str(cookies)))
    assert cmd[cmd.index("--proxy") + 1] == "http://vpn:3128" and cmd[cmd.index("--cookies") + 1] == str(cookies)


# --- media helpers ---------------------------------------------------------------------------------------------------------

def test_square_thumbnail_like_the_old_project(tmp_path):
    src = tmp_path / "thumb.png"
    image = Image.new("RGB", (1280, 720), (10, 10, 10))
    for x in range(1280):
        for y in range(0, 720, 4):
            image.putpixel((x, y), (250, 40, 40))
    image.save(src)
    out = tmp_path / "square.jpg"
    assert media.square_thumbnail(src, out)
    with Image.open(out) as sq:
        assert sq.size == (1400, 1400) and sq.format == "JPEG"
        top_band = sq.getpixel((700, 10))                      # blurred background above/below the centred original
        assert top_band != (250, 40, 40)
    with Image.open(out) as sq:
        # the original keeps its aspect ratio: 1400 x 787 in the middle
        assert sq.getpixel((700, 700)) != sq.getpixel((700, 100))
    assert not list(tmp_path.glob("tmp*"))


def test_square_thumbnail_of_webp_and_of_garbage(tmp_path):
    src = tmp_path / "thumb.webp"
    Image.new("RGB", (640, 360), (0, 128, 255)).save(src, "WEBP")
    assert media.square_thumbnail(src, tmp_path / "a.jpg") and (tmp_path / "a.jpg").stat().st_size > 0
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert media.square_thumbnail(bad, tmp_path / "b.jpg") is False and not (tmp_path / "b.jpg").exists()


def test_chapters_file():
    data = json.loads(build_chapters_json([{"start_time": 0, "title": "Intro"}, {"start_time": 61.5, "title": "x" * 60},
                                           {"start_time": 9, "title": ""}, {"start_time": 10}, "junk"]))
    assert data["version"] == "1.2.0" and len(data["chapters"]) == 2
    assert data["chapters"][1] == {"startTime": 61.5, "title": "x" * 44 + "…"}


def probe(duration, codec="aac", audio=True):
    streams = [{"codec_type": "audio", "codec_name": codec}] if audio else [{"codec_type": "video", "codec_name": "h264"}]
    return {"streams": streams, "format": {"duration": str(duration)}}


def test_verify_audio_rules():
    assert media.verify_audio(Path("x"), 3600, probe(3600.4)) == 3600.4
    assert media.verify_audio(Path("x"), 3600, probe(3590.0))                     # within 10 s
    assert media.verify_audio(Path("x"), 36000, probe(35000))                     # within 3 %
    assert media.verify_audio(Path("x"), None, probe(12))                         # no metadata duration: only readability
    for bad, text in ((probe(3000), "metadata says"), (probe(3600, "opus"), "not AAC"), (probe(3600, audio=False), "no audio"),
                      ({"streams": [{"codec_type": "audio", "codec_name": "aac"}], "format": {}}, "duration")):
        with pytest.raises(Failure) as exc:
            media.verify_audio(Path("x"), 3600, bad)
        assert exc.value.error_class == "incomplete" and text in exc.value.message


# --- the executor with fake tools ---------------------------------------------------------------------------------------------

@pytest.fixture
def tools(tmp_path, monkeypatch):
    install(tmp_path / "bin")
    for k, v in environment(tmp_path / "bin").items():
        monkeypatch.setenv(k, v)


class Server:
    """Records the calls of a download and answers like the real server."""

    def __init__(self, upload_status=200, complete_status=200, upload_failures=0):
        self.calls, self.uploads, self.upload_status = [], {}, upload_status
        self.complete_status, self.fail_first = complete_status, upload_failures

    def __call__(self, request: httpx.Request):
        path = request.url.path
        self.calls.append((request.method, path.rsplit("/", 1)[-1], dict(request.url.params)))
        if path.endswith("/upload"):
            if self.fail_first > 0:
                self.fail_first -= 1
                return httpx.Response(503)
            if self.upload_status != 200:
                return httpx.Response(self.upload_status, json={"detail": "nope"})
            p = request.url.params
            self.uploads[p["role"]] = (len(request.content), media_sha(request.content), p.get("ext"))
            return httpx.Response(200, json={"role": p["role"], "size": int(p["size"]), "sha256": p["sha256"]})
        if path.endswith("/complete"):
            if self.complete_status != 200:
                return httpx.Response(self.complete_status, json={"detail": "Package rejected: x"})
            return httpx.Response(200, json={"video_id": "v", "audio_file": "Episode： v.m4a"})
        return httpx.Response(404)


def media_sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def task(video_id, task_id=7):
    return TaskInfo.model_validate({
        "id": task_id, "type": "download", "podcast": {"id": "abgt", "name": "ABGT", "playlist_url": "https://x/p", "scan_depth": 20},
        "video_id": video_id, "title": "t", "attempt": 1, "lease_seconds": 300, "lease_until": "2026-11-05T12:05:00Z",
        "pacing": {"pause_min_s": 0, "pause_max_s": 0, "downloads_per_hour": 3}})


def execute(tmp_path, video_id, server=None, **cfg_kw):
    server = server or Server()
    client = ApiClient("https://x", "pnw_t", transport=httpx.MockTransport(server))
    progress = Progress()
    run_download(client, task(video_id), cfg(tmp_path, **cfg_kw), threading.Event(), progress, pause=lambda s: None)
    return server, progress


def test_successful_download_uploads_the_whole_package_and_completes(tools, tmp_path):
    server, progress = execute(tmp_path, "aaaaaaaaaaa")
    roles = [p["role"] for m, name, p in server.calls if name == "upload"]
    assert roles == ["audio", "info", "thumb", "square", "chapters"] and server.calls[-1][:2] == ("POST", "complete")
    assert server.uploads["thumb"][2] == "webp" and server.uploads["audio"][0] > 2000
    assert progress.text == "publishing"
    assert not (tmp_path / "work" / "task-7").exists()                                  # the work directory is cleaned up
    # each upload carried its real SHA-256 and size
    for _, name, p in server.calls:
        if name == "upload":
            size, digest, _ = server.uploads[p["role"]]
            assert (int(p["size"]), p["sha256"]) == (size, digest)


def test_chapters_file_has_the_old_format(tools, tmp_path, monkeypatch):
    captured = {}

    class Capturing(Server):
        def __call__(self, request):
            if request.url.path.endswith("/upload") and request.url.params["role"] == "chapters":
                captured["chapters"] = json.loads(request.content)
            if request.url.path.endswith("/upload") and request.url.params["role"] == "square":
                captured["square"] = request.content
            return super().__call__(request)

    execute(tmp_path, "aaaaaaaaaaa", Capturing())
    assert captured["chapters"]["chapters"][1]["title"] == "x" * 44 + "…" and captured["chapters"]["version"] == "1.2.0"
    assert captured["square"][:3] == b"\xff\xd8\xff"


def test_optional_files_may_be_missing(tools, tmp_path):
    server, _ = execute(tmp_path, "nothumbnochap")
    roles = [p["role"] for m, name, p in server.calls if name == "upload"]
    assert roles == ["audio", "info"]


@pytest.mark.parametrize("video_id,error_class", [
    ("bot00000001", "bot_check"), ("private0001", "private"), ("removed0001", "removed"), ("net00000001", "network"),
    ("crash000001", "internal"), ("premiere001", "not_yet_available"),
])
def test_download_errors_are_classified(tools, tmp_path, video_id, error_class):
    with pytest.raises(Failure) as exc:
        execute(tmp_path, video_id)
    assert exc.value.error_class == error_class and exc.value.message
    if error_class == "not_yet_available":
        assert exc.value.retry_after == 3 * 3600 + 300                                  # "will begin in 3 hours" plus a margin
    assert not (tmp_path / "work" / "task-7").exists()


@pytest.mark.parametrize("video_id,message", [
    ("short000001", "metadata says"), ("mp3000000001", "not AAC"), ("corrupt0001", "ffprobe cannot read"),
    ("noinfo00001", "no info.json"), ("wrongid0001", "different video"), ("noduration01", "no duration"),
])
def test_unverifiable_downloads_are_incomplete_and_never_uploaded(tools, tmp_path, video_id, message):
    server = Server()
    with pytest.raises(Failure) as exc:
        execute(tmp_path, video_id, server)
    assert exc.value.error_class == "incomplete" and message in exc.value.message
    assert server.calls == []                                                              # nothing reached the server


def test_hung_download_is_killed_at_the_hard_limit(tools, tmp_path):
    started = time.monotonic()
    with pytest.raises(Failure) as exc:
        execute(tmp_path, "hang0000001", download_timeout=1.0)
    assert exc.value.error_class == "timeout" and time.monotonic() - started < 40


def test_lost_lease_stops_the_download(tools, tmp_path):
    lost = threading.Event()
    threading.Timer(0.8, lost.set).start()
    client = ApiClient("https://x", "pnw_t", transport=httpx.MockTransport(Server()))
    started = time.monotonic()
    with pytest.raises(Lost):
        run_download(client, task("hang0000001"), cfg(tmp_path), lost, Progress())
    assert time.monotonic() - started < 15


def test_upload_is_retried_on_server_errors(tools, tmp_path):
    server, _ = execute(tmp_path, "aaaaaaaaaaa", Server(upload_failures=2))
    assert server.calls[-1][1] == "complete" and len([c for c in server.calls if c[1] == "upload"]) == 7   # 5 files + 2 retries


def test_upload_gives_up_after_the_configured_retries(tools, tmp_path):
    with pytest.raises(Failure) as exc:
        execute(tmp_path, "aaaaaaaaaaa", Server(upload_failures=99), upload_retries=3)
    assert exc.value.error_class == "network" and "3 times" in exc.value.message


@pytest.mark.parametrize("status,error_class", [(422, "incomplete"), (413, "incomplete")])
def test_rejected_uploads_are_incomplete(tools, tmp_path, status, error_class):
    server = Server(upload_status=status)
    with pytest.raises(Failure) as exc:
        execute(tmp_path, "aaaaaaaaaaa", server, upload_retries=2)
    assert exc.value.error_class == error_class
    assert len([c for c in server.calls if c[1] == "upload"]) == (1 if status == 413 else 2)   # 413 is final, 422 may be a glitch
    assert all(c[1] != "complete" for c in server.calls)


def test_a_rejected_package_is_not_reported_twice(tools, tmp_path):
    """The server records a rejected package as an incomplete attempt itself; the worker must not also call fail."""
    server, _ = execute(tmp_path, "aaaaaaaaaaa", Server(complete_status=422))
    assert server.calls[-1][1] == "complete"


def test_run_task_reports_download_failures_to_the_server(tools, tmp_path):
    calls = []

    def handler(request):
        calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content) if request.content else None))
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_until": "x"})
        return httpx.Response(200, json={"state": "pending", "not_before": "2026-11-05T12:01:00Z"})

    client = ApiClient("https://x", "pnw_t", transport=httpx.MockTransport(handler))
    run_task(client, task("premiere001"), cfg(tmp_path))
    (name, body), = [c for c in calls if c[0] == "fail"]
    assert body["error_class"] == "not_yet_available" and body["retry_after_seconds"] == 3 * 3600 + 300


def test_run_task_sends_progress_with_the_heartbeat(tools, tmp_path):
    beats = []

    def handler(request):
        if request.url.path.endswith("/heartbeat"):
            beats.append(json.loads(request.content))
            return httpx.Response(200, json={"lease_until": "x"})
        return httpx.Response(404)

    client = ApiClient("https://x", "pnw_t", transport=httpx.MockTransport(handler))
    run_task(client, task("hang0000001"), cfg(tmp_path, download_timeout=2.5, heartbeat_interval=0.3))
    assert any(b.get("progress") == "downloading" for b in beats)


def test_worker_logs_never_contain_cookies_path_content(tools, tmp_path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("SECRET-COOKIE-VALUE")
    execute(tmp_path, "aaaaaaaaaaa", cookies_file=str(cookies))
    assert "SECRET-COOKIE-VALUE" not in caplog.text
