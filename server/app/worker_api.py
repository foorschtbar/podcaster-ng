"""Worker API v1 under /api/worker (bearer token). Separate from the public and admin routes."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from pn_shared import API_VERSION
from pn_shared.api import HelloRequest, HelloResponse, Pacing, PingResponse, WorkerInfo

from . import settings, workers
from .db import connect
from .web import client_of

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/worker", tags=["worker"])

PING_INTERVAL_SECONDS = 30


def _server_time() -> str:
    return datetime.now(timezone.utc).strftime(workers.TIME_FORMAT)


def _bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def authenticate(request: Request) -> dict:
    """Authenticate the calling worker and update ``last_seen``.

    401 for missing/unknown/revoked tokens, 403 for deactivated workers, 429 when too many
    invalid tokens came from one address or a worker calls too often. Rate limiting never
    affects a valid token (an invalid one on the same address cannot lock a good worker out).
    """
    app = request.app
    client = client_of(request)
    token = _bearer(request)
    conn = connect(app.state.config.db_path)
    try:
        row = workers.find_by_token(conn, token) if token else None
        if row is None:
            limiter = app.state.worker_auth_limiter
            if limiter.locked_for(client):
                raise HTTPException(429, "Too many invalid tokens", headers={"Retry-After": str(limiter.locked_for(client))})
            limiter.record_failure(client)
            log.warning("Worker API: invalid token from %s", client)
            raise HTTPException(401, "Invalid or revoked token", headers={"WWW-Authenticate": "Bearer"})
        if not row["active"]:
            raise HTTPException(403, "Worker is deactivated")
        if not app.state.worker_rate_limiter.allow(row["id"]):
            raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": "60"})
        workers.touch(conn, row["id"])
        return dict(row)
    finally:
        conn.close()


@router.get("/ping", response_model=PingResponse)
def ping(worker: dict = Depends(authenticate)) -> PingResponse:
    return PingResponse(server_time=_server_time())


@router.post("/hello", response_model=HelloResponse)
def hello(body: HelloRequest, request: Request, worker: dict = Depends(authenticate)) -> HelloResponse:
    config = request.app.state.config
    tools = {name: version for name, version in (("yt-dlp", body.ytdlp_version), ("ffmpeg", body.ffmpeg_version),
                                                 ("deno", body.deno_version)) if version}
    conn = connect(config.db_path)
    try:
        workers.record_hello(conn, worker["id"], body.worker_version, body.capabilities, tools)
        defaults = settings.get_all(conn, {"base_url": config.base_url})
    finally:
        conn.close()
    log.info("Worker %r connected (version %s)", worker["name"], body.worker_version)

    def effective(key: str) -> int:
        return worker[key] if worker[key] is not None else defaults[key]

    return HelloResponse(
        api_version=API_VERSION, server_time=_server_time(),
        worker=WorkerInfo(id=worker["id"], name=worker["name"], tier=worker["tier"]),
        pacing=Pacing(pause_min_s=effective("pause_min_s"), pause_max_s=effective("pause_max_s"),
                      downloads_per_hour=effective("downloads_per_hour")),
        lease_seconds=defaults["lease_seconds"], ping_interval_seconds=PING_INTERVAL_SECONDS)
