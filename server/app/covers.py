"""Podcast cover images: uploaded, fetched from a remote URL, or imported from a file. All end up the same way:
a file ``cover-<hash>.<ext>`` in ``media/<podcast>/`` (the name changes with the content, so podcast apps that cache
by URL see a new image) and ``podcasts.cover_file`` pointing at it. ``image_url`` then only remembers the source URL.
"""

import hashlib
import ipaddress
import logging
import os
import socket
import sqlite3
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import queue

log = logging.getLogger(__name__)
MAX_BYTES = 5 << 20
MAX_REDIRECTS = 3
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
RESOLVE = socket.getaddrinfo          # replaced in tests


class CoverError(Exception):
    """The image cannot be used; the message is meant for the person who gave it."""


def detect(data: bytes) -> str | None:
    """File extension from the first bytes (JPEG, PNG or WebP), None for anything else."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def check_bytes(data: bytes) -> str:
    if not data:
        raise CoverError("the file is empty")
    if len(data) > MAX_BYTES:
        raise CoverError(f"the image is larger than {MAX_BYTES >> 20} MB")
    ext = detect(data)
    if ext is None:
        raise CoverError("only JPEG, PNG and WebP images are supported")
    return ext


def _check_host(host: str) -> None:
    """Refuse addresses on private networks, so the server cannot be used to look at its own surroundings."""
    try:
        infos = RESOLVE(host, None)
    except OSError:
        raise CoverError("the host name cannot be resolved") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise CoverError("the address points to a private or local network")


def fetch(url: str, transport: httpx.BaseTransport | None = None) -> bytes:
    """Download an image (http/https, at most 3 redirects, each target checked, size and type checked)."""
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False, transport=transport,
                          headers={"User-Agent": "PodcasterNG"}) as client:
            for _ in range(MAX_REDIRECTS + 1):
                parts = urlsplit(url)
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    raise CoverError("the address must start with http:// or https://")
                _check_host(parts.hostname)
                with client.stream("GET", url) as response:
                    if response.is_redirect and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        continue
                    if response.status_code != 200:
                        raise CoverError(f"the server answered {response.status_code}")
                    if int(response.headers.get("content-length") or 0) > MAX_BYTES:
                        raise CoverError(f"the image is larger than {MAX_BYTES >> 20} MB")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data += chunk
                        if len(data) > MAX_BYTES:
                            raise CoverError(f"the image is larger than {MAX_BYTES >> 20} MB")
                    check_bytes(bytes(data))
                    return bytes(data)
            raise CoverError("too many redirects")
    except httpx.HTTPError as exc:  # the text may contain the URL; only its type is shown
        raise CoverError(f"the download failed ({type(exc).__name__})") from None


def store(conn: sqlite3.Connection, media_dir: Path, podcast_id: str, data: bytes, source_url: str = "") -> str:
    """Save ``data`` as the podcast's cover and return the file name. The previous cover file is removed."""
    ext = check_bytes(data)
    name = f"cover-{hashlib.sha256(data).hexdigest()[:10]}.{ext}"
    folder = media_dir / podcast_id
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{name}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, folder / name)
    old = conn.execute("SELECT cover_file FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    conn.execute("UPDATE podcasts SET cover_file = ?, image_url = ?, updated_at = ? WHERE id = ?",
                 (name, source_url, queue.ts(queue.utcnow()), podcast_id))
    if old and old["cover_file"] and old["cover_file"] != name:
        _unlink(folder, old["cover_file"])
    return name


def remove(conn: sqlite3.Connection, media_dir: Path, podcast_id: str) -> None:
    """Drop the cover (file, reference and source URL)."""
    old = conn.execute("SELECT cover_file FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    conn.execute("UPDATE podcasts SET cover_file = NULL, image_url = '', updated_at = ? WHERE id = ?",
                 (queue.ts(queue.utcnow()), podcast_id))
    if old and old["cover_file"]:
        _unlink(media_dir / podcast_id, old["cover_file"])


def _unlink(folder: Path, name: str) -> None:
    path = folder / name
    if name.startswith("cover-") and path.is_file() and not path.is_symlink():   # never touch anything else
        try:
            path.unlink()
        except OSError as exc:
            log.warning("Could not remove %s: %s", path, exc)


def import_file(conn: sqlite3.Connection, media_dir: Path, podcast_id: str, path: Path) -> str:
    """Take an image from the file system as if it had been uploaded."""
    return store(conn, media_dir, podcast_id, Path(path).read_bytes())
