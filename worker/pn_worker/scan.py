"""Scan executor: reads a playlist's videos (metadata only) with the yt-dlp command line.

yt-dlp runs as a separate process so that the hard time limit really kills it, whatever it is doing.
"""

import json
import logging
import shutil
import threading

from pn_shared.api import VideoInfo

from .classify import classify
from .process import Failure, Lost, run_process

log = logging.getLogger("pn_worker.scan")
SKIPPED_TITLES = {"[private video]", "[deleted video]"}


ScanFailure = Failure   # kept as the scan's name for the same classes
ScanLost = Lost


def build_command(binary: str, url: str, depth: int, proxy: str | None) -> list[str]:
    cmd = [binary, "--flat-playlist", "--dump-single-json", "--no-warnings", "--playlist-end", str(depth),
           "--socket-timeout", "30", "--no-cache-dir"]
    if proxy:
        cmd += ["--proxy", proxy]
    return [*cmd, "--", url]


def parse_entries(output: str) -> list[VideoInfo]:
    try:
        data = json.loads(output)
    except ValueError:
        raise ScanFailure("incomplete", "yt-dlp did not return valid JSON") from None
    if not isinstance(data, dict):
        raise ScanFailure("incomplete", "yt-dlp returned an unexpected structure")
    videos = []
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        title = str(entry.get("title") or "")
        if title.strip().lower() in SKIPPED_TITLES:
            continue
        release = entry.get("release_timestamp")
        try:
            videos.append(VideoInfo(
                id=str(entry["id"]), title=title[:500], upload_date=(str(entry["upload_date"])[:20] if entry.get("upload_date") else None),
                live_status=(str(entry["live_status"])[:30] if entry.get("live_status") else None),
                release_timestamp=int(release) if isinstance(release, (int, float)) else None))
        except ValueError:
            continue  # an id the server would not accept (not a YouTube video id)
    return videos


def scan_playlist(url: str, depth: int, *, timeout: float, proxy: str | None, lost: threading.Event,
                  binary: str = "yt-dlp") -> list[VideoInfo]:
    path = shutil.which(binary)
    if not path:
        raise ScanFailure("internal", f"{binary} is not installed on this worker")
    code, out, err = run_process(build_command(path, url, depth, proxy), timeout, lost, what="The scan")
    if code != 0:
        tail = "\n".join(err.strip().splitlines()[-6:])[-1500:]
        raise ScanFailure(classify(err), tail or f"yt-dlp exited with status {code}")
    videos = parse_entries(out)
    log.info("Scan found %d videos", len(videos))
    return videos
