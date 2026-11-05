"""Public routes (no session, no CSRF): feeds and media files. URLs match the old Podcaster."""

import os
from email.utils import parsedate_to_datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from . import episodes, feeds, podcasts, settings
from .db import connect

router = APIRouter()

# Only these file types are served; the content type is derived from the name.
MEDIA_TYPES = {".m4a": "audio/x-m4a", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp"}


def _not_found() -> Response:
    return PlainTextResponse("Not Found", status_code=404)


def media_type_of(name: str) -> str | None:
    lower = name.lower()
    if lower.endswith(".chapters.json"):
        return "application/json+chapters"
    return MEDIA_TYPES.get(os.path.splitext(lower)[1])


def safe_media_path(media_dir: Path, podcast_id: str, name: str) -> Path | None:
    """Resolve a requested file or return None. Rejects traversal, hidden names and symlinks."""
    if not podcasts.SLUG_RE.match(podcast_id) or not name or name in (".", "..") or name.startswith("."):
        return None
    if "/" in name or "\\" in name or "\x00" in name:
        return None
    root = media_dir / podcast_id
    path = root / name
    try:
        # Neither the podcast directory nor the file may be a symlink, and the
        # resolved location must be exactly where we expect it.
        if root.is_symlink() or path.is_symlink() or not path.is_file():
            return None
        if root.resolve().parent != media_dir.resolve() or path.resolve().parent != root.resolve():
            return None
    except OSError:
        return None
    return path


@router.api_route("/{podcast_id}.xml", methods=["GET", "HEAD"], include_in_schema=False)
def feed(request: Request, podcast_id: str):
    if not podcasts.SLUG_RE.match(podcast_id):
        return _not_found()
    config = request.app.state.config
    conn = connect(config.db_path)
    try:
        podcast = podcasts.get(conn, podcast_id)
        if not podcast:
            return _not_found()
        items = episodes.list_available(conn, podcast_id)
        base = settings.get(conn, "base_url", {"base_url": config.base_url}) or str(request.base_url).rstrip("/")
    finally:
        conn.close()
    return Response(feeds.build_feed(podcast, items, base), media_type="application/xml; charset=utf-8")


@router.api_route("/files/{podcast_id}/{name}", methods=["GET", "HEAD"], include_in_schema=False)
def media_file(request: Request, podcast_id: str, name: str):
    media_type = media_type_of(name)
    path = safe_media_path(request.app.state.config.media_dir, podcast_id, name) if media_type else None
    if path is None:
        return _not_found()
    stat = path.stat()
    response = FileResponse(path, media_type=media_type, stat_result=stat)  # Range, ETag, Last-Modified, HEAD
    if _not_modified(request, response, stat.st_mtime):
        return Response(status_code=304, headers={k: response.headers[k] for k in ("etag", "last-modified")})
    return response


def _not_modified(request: Request, response: FileResponse, mtime: float) -> bool:
    """Answer conditional requests (apps re-check artwork and audio with If-None-Match / If-Modified-Since)."""
    etag = response.headers.get("etag", "")
    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None:
        tags = [t.strip().removeprefix("W/") for t in if_none_match.split(",")]
        return "*" in tags or etag in tags
    since = request.headers.get("if-modified-since")
    if since:
        try:
            return int(mtime) <= int(parsedate_to_datetime(since).timestamp())
        except (TypeError, ValueError):
            return False
    return False
