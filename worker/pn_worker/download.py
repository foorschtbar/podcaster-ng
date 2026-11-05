"""Download executor: yt-dlp (audio, thumbnail, info.json) -> verify -> square artwork and chapters -> upload -> complete."""

import json
import logging
import shutil
import threading
import time
from pathlib import Path

from pn_shared.api import FileEntry, TaskInfo
from pn_shared.chapters import build_chapters_json

from . import media
from .classify import classify, wait_seconds
from .client import ApiClient, AuthError, ServerError, TaskLost, UploadRejected
from .config import Config
from .process import Failure, Lost, run_process

log = logging.getLogger("pn_worker.download")
THUMB_EXTS = ("webp", "jpg", "jpeg", "png")
VIDEO_URL = "https://www.youtube.com/watch?v={}"


class Progress:
    """What the worker is doing; the heartbeat sends it to the server."""

    def __init__(self) -> None:
        self.text = ""

    def set(self, text: str) -> None:
        self.text = text[:200]
        log.info("%s", text)


def build_command(binary: str, video_id: str, config: Config) -> list[str]:
    """Same recipe as the old Podcaster: best audio as m4a, thumbnail and info.json; fixed file names in the work dir."""
    cmd = [binary, "--ignore-config", "--no-playlist", "--no-warnings", "--no-cache-dir",
           "-f", "m4a/bestaudio/best", "-x", "--audio-format", "m4a",
           "--write-info-json", "--write-thumbnail",
           "-o", "audio.%(ext)s", "-o", "infojson:info", "-o", "thumbnail:thumb",
           "--socket-timeout", "30", "--retries", "5", "--fragment-retries", "5"]
    if config.ytdlp_proxy:
        cmd += ["--proxy", config.ytdlp_proxy]
    if config.cookies_file:
        cmd += ["--cookies", config.cookies_file]
    return [*cmd, "--", VIDEO_URL.format(video_id)]


def _find(work: Path, patterns: tuple[str, ...]) -> Path | None:
    for pattern in patterns:
        hit = sorted(work.glob(pattern))
        if hit:
            return hit[0]
    return None


def _read_info(path: Path | None, video_id: str) -> dict:
    if path is None:
        raise Failure("incomplete", "yt-dlp wrote no info.json")
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise Failure("incomplete", "the info.json is not valid JSON") from None
    if not isinstance(info, dict) or str(info.get("id") or "") != video_id:
        raise Failure("incomplete", "the info.json belongs to a different video")
    if not str(info.get("title") or "").strip():
        raise Failure("incomplete", "the info.json has no title")
    if not info.get("duration"):
        raise Failure("incomplete", "the metadata has no duration (live stream or premiere that has not finished?)")
    return info


def prepare(task: TaskInfo, config: Config, work: Path, lost: threading.Event, progress: Progress, deadline: float) -> dict:
    """Run yt-dlp and build the package files. Returns role -> (path, ext)."""
    import shutil as _shutil
    binary = _shutil.which("yt-dlp")
    if not binary:
        raise Failure("internal", "yt-dlp is not installed on this worker")
    progress.set("downloading")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Failure("timeout", "The time limit for this task is used up")
    code, out, err = run_process(build_command(binary, task.video_id, config), remaining, lost, what="The download", cwd=str(work))
    if code != 0:
        tail = "\n".join(err.strip().splitlines()[-8:])[-1500:]
        error_class = classify(err)
        raise Failure(error_class, tail or f"yt-dlp exited with status {code}",
                      wait_seconds(err) if error_class == "not_yet_available" else None)

    audio = work / "audio.m4a"
    if not audio.is_file():
        raise Failure("incomplete", "yt-dlp produced no m4a file")
    progress.set("verifying")
    info = _read_info(_find(work, ("info.info.json", "*.info.json")), task.video_id)
    media.verify_audio(audio, float(info["duration"]))

    files = {"audio": (audio, ""), "info": (_find(work, ("info.info.json", "*.info.json")), "")}
    thumb = _find(work, tuple(f"thumb.{e}" for e in THUMB_EXTS) + tuple(f"*.{e}" for e in THUMB_EXTS))
    if thumb:
        files["thumb"] = (thumb, thumb.suffix.lstrip(".").lower())
        progress.set("preparing artwork")
        square = work / "square.jpg"
        if media.square_thumbnail(thumb, square):
            files["square"] = (square, "")
    if info.get("chapters"):
        chapters = build_chapters_json(info["chapters"])
        if json.loads(chapters)["chapters"]:
            path = work / "chapters.json"
            path.write_bytes(chapters)
            files["chapters"] = (path, "")
    return files


def deliver(call, lost: threading.Event, config: Config, what: str, pause=time.sleep):
    """Run ``call()`` until the server answers. A server that restarts or is briefly unreachable is waited out
    (2 s, doubling to 60 s) for up to ``config.delivery_timeout``, so a finished download is not thrown away.
    Anything but "unreachable" (a rejection, a lost task) is raised at once."""
    start, waited, delay = time.monotonic(), 0.0, 2.0
    while True:
        if lost.is_set():
            raise Lost()
        try:
            return call()
        except ServerError as exc:
            if max(time.monotonic() - start, waited) + delay > config.delivery_timeout:
                raise Failure("network", f"{what}: the server stayed unreachable for {config.delivery_timeout:.0f} s ({exc})") from None
            log.warning("%s failed (%s); trying again in %.0f s", what, exc, delay)
            pause(delay)
            waited += delay
            delay = min(60.0, delay * 2)


def upload_file(client: ApiClient, task: TaskInfo, role: str, path: Path, ext: str, progress: Progress, config: Config,
                lost: threading.Event, pause=time.sleep) -> FileEntry:
    digest = media.sha256_file(path)
    last_percent = -10

    def report(sent: int, total: int) -> None:
        nonlocal last_percent
        percent = sent * 100 // total
        if percent - last_percent >= 10:
            last_percent = percent
            progress.text = f"uploading {role} {percent}%"

    rejected = 0
    while True:
        progress.set(f"uploading {role}")
        try:
            reply = deliver(lambda: client.upload(task.id, role, path, digest, ext, on_progress=report), lost, config,
                            f"Upload of {role}", pause)
            return FileEntry(role=role, size=reply.size, sha256=reply.sha256)
        except UploadRejected as exc:
            rejected += 1
            if exc.status == 413:
                raise Failure("incomplete", f"the server refuses the {role} file: {exc.detail}") from None
            if rejected >= config.upload_retries:
                raise Failure("incomplete", f"the {role} upload was rejected {rejected} times: {exc}") from None
            log.warning("Upload of %s was rejected (%s); trying again", role, exc)
            pause(2.0 * rejected)


def run_download(client: ApiClient, task: TaskInfo, config: Config, lost: threading.Event, progress: Progress,
                 pause=time.sleep) -> None:
    """Execute one download task completely. Raises Failure / Lost / AuthError / TaskLost for the caller to report."""
    deadline = time.monotonic() + config.download_timeout
    work = Path(config.work_dir) / f"task-{task.id}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        files = prepare(task, config, work, lost, progress, deadline)
        entries = [upload_file(client, task, role, path, ext, progress, config, lost, pause)
                   for role, (path, ext) in files.items()]
        progress.set("publishing")
        try:
            reply = deliver(lambda: client.complete(task.id, entries), lost, config, "Publishing", pause)
        except UploadRejected as exc:
            # The server has already recorded the rejected package as an incomplete attempt.
            log.error("Task %d: package rejected by the server: %s", task.id, exc.detail)
            return
        log.info("Task %d published as %r", task.id, reply.audio_file)
    finally:
        shutil.rmtree(work, ignore_errors=True)
