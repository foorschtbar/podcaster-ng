"""Pure ASGI middleware (no response buffering, so large downloads stream straight through)."""

import logging

from starlette.concurrency import run_in_threadpool

from . import app_groups, hits, stats
from .db import connect

log = logging.getLogger(__name__)

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
                if scope.get("state", {}).get("admin"):
                    extra.update(ADMIN_HEADERS)
                message["headers"] = [*message["headers"], *(
                    (k.lower().encode(), v.encode()) for k, v in extra.items() if k.lower().encode() not in present)]
            await send(message)

        await self.app(scope, receive, send_wrapper)


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
