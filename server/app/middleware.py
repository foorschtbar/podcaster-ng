"""Pure ASGI middleware (no response buffering, so large downloads stream straight through)."""

import logging
import re

from starlette.concurrency import run_in_threadpool

from . import app_groups, hits, stats
from .db import connect

log = logging.getLogger(__name__)

UPLOAD_PATH = re.compile(r"^/api/worker/tasks/\d+/upload$")
FORM_WITH_FILE = re.compile(r"^/podcasts(/[a-z0-9-]+)?$")   # the podcast form can carry a cover image
FILE_FORM_LIMIT = 6 << 20
ADMIN_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; form-action 'self'",
}


class SecurityHeaders:
    """Basic hardening headers everywhere; stricter ones on admin responses (flagged by the admin gate)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message["headers"]}
                extra = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"}
                if scope["path"].startswith("/api/"):
                    extra["Cache-Control"] = "no-store"
                if scope.get("state", {}).get("admin"):
                    extra.update(ADMIN_HEADERS)
                message["headers"] = [*message["headers"], *(
                    (k.lower().encode(), v.encode()) for k, v in extra.items() if k.lower().encode() not in present)]
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodyTooLarge(Exception):
    pass


class BodyLimit:
    """Rejects oversized request bodies (413) everywhere except the worker upload, which has its own per-file limits.

    Forms, JSON and logins are tiny; this stops anyone from making the server read gigabytes into memory.
    """

    def __init__(self, app, limit: int = 1 << 20):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or UPLOAD_PATH.match(scope["path"]):
            return await self.app(scope, receive, send)
        limit = FILE_FORM_LIMIT if scope["method"] == "POST" and FORM_WITH_FILE.match(scope["path"]) else self.limit
        declared = dict(scope["headers"]).get(b"content-length", b"0")
        if declared.isdigit() and int(declared) > limit:
            return await self._reject(send)
        seen, started = 0, False

        async def counting_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge:
            if started:
                raise
            await self._reject(send)

    @staticmethod
    async def _reject(send):
        body = b"Request body too large\n"
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode()),
            (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})


class HitCounter:
    """Counts feed requests and audio downloads (public routes only). See ``hits`` for the rules."""

    def __init__(self, app, db_path):
        self.app = app
        self.db_path = db_path

    async def __call__(self, scope, receive, send):
        hit = hits.classify(scope["path"]) if scope["type"] == "http" and scope["method"] == "GET" else None
        if hit is None:
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        agent = headers.get(b"user-agent", b"").decode("latin-1")  # only used for the group, never stored
        first = hits.starts_download(headers.get(b"range", b"").decode("latin-1") or None)

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and message["status"] in (200, 206):
                length = dict(message["headers"]).get(b"content-length", b"0")
                try:
                    await run_in_threadpool(self._record, hit, agent, first, int(length))
                except Exception:  # noqa: BLE001 - statistics must never break serving
                    log.warning("Could not record hit", exc_info=True)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _record(self, hit, agent: str, first: bool, nbytes: int) -> None:
        kind, podcast_id, file_name = hit
        group = app_groups.classify(agent)
        conn = connect(self.db_path)
        try:
            episode = stats.episode_for_file(conn, podcast_id, file_name) if file_name else None
            counted = 1 if (kind == "feed" or first) else 0
            stats.record_hit(conn, day=stats.today(), podcast_id=podcast_id, episode_id=episode, kind=kind,
                             app_group=group, count=counted, nbytes=nbytes)
        finally:
            conn.close()
