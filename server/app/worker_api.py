"""Worker API v1 under /api/worker (bearer token). Separate from the public and admin routes."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from pn_shared import API_VERSION
from pn_shared.api import (ClaimRequest, ClaimResponse, FailRequest, FailResponse, HeartbeatRequest, HeartbeatResponse,
                           HelloRequest, HelloResponse, Pacing, PingResponse, ScanResult, ScanResultResponse, TaskInfo,
                           TaskPodcast, WorkerInfo)

from . import queue, settings, workers
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


# --- queue -----------------------------------------------------------------------------------------------

def _queue_error(exc: queue.QueueError) -> HTTPException:
    if isinstance(exc, queue.TaskNotFound):
        return HTTPException(404, "Unknown task")
    if isinstance(exc, queue.NotOwner):
        return HTTPException(409, "This task is not claimed by you (any more)")
    return HTTPException(409, str(exc) or "Not possible in the current state")


@router.post("/claim", response_model=ClaimResponse)
def claim(body: ClaimRequest, request: Request, worker: dict = Depends(authenticate)) -> ClaimResponse:
    config = request.app.state.config
    conn = connect(config.db_path)
    try:
        defaults = settings.get_all(conn, {"base_url": config.base_url})
        result = queue.claim(conn, worker, sorted(set(body.types)), defaults, defaults["lease_seconds"])
        if result.task is None:
            return ClaimResponse(retry_after_seconds=result.retry_after)
        task = result.task
        podcast = conn.execute("SELECT * FROM podcasts WHERE id = ?", (task["podcast_id"],)).fetchone()
        limits = queue._pacing(worker, {"pause_min_s": podcast["pause_min_s"], "pause_max_s": podcast["pause_max_s"],
                                        "downloads_per_hour": podcast["downloads_per_hour"]}, defaults)
    finally:
        conn.close()
    log.info("Task %d (%s) claimed by worker %r", task["id"], task["type"], worker["name"])
    return ClaimResponse(task=TaskInfo(
        id=task["id"], type=task["type"],
        podcast=TaskPodcast(id=podcast["id"], name=podcast["name"], playlist_url=podcast["playlist_url"],
                            scan_depth=podcast["scan_depth"]),
        video_id=task["video_id"], title=task["title"], attempt=result.attempt,
        lease_seconds=defaults["lease_seconds"], lease_until=result.lease_until, pacing=Pacing(pause_min_s=limits["pause_min_s"], pause_max_s=limits["pause_max_s"],
                                                              downloads_per_hour=limits["podcast_downloads_per_hour"])))


def _with_conn(request: Request, call):
    conn = connect(request.app.state.config.db_path)
    try:
        return call(conn)
    except queue.QueueError as exc:
        raise _queue_error(exc) from None
    finally:
        conn.close()


@router.post("/tasks/{task_id}/heartbeat", response_model=HeartbeatResponse)
def heartbeat(task_id: int, body: HeartbeatRequest, request: Request, worker: dict = Depends(authenticate)):
    def run(conn):
        lease = settings.get(conn, "lease_seconds")
        return HeartbeatResponse(lease_until=queue.heartbeat(conn, task_id, worker["id"], lease))
    return _with_conn(request, run)


@router.post("/tasks/{task_id}/scan-result", response_model=ScanResultResponse)
def scan_result(task_id: int, body: ScanResult, request: Request, worker: dict = Depends(authenticate)):
    videos = [v.model_dump() for v in body.videos]
    return _with_conn(request, lambda conn: ScanResultResponse(**queue.complete_scan(conn, task_id, worker["id"], videos)))


@router.post("/tasks/{task_id}/fail", response_model=FailResponse)
def fail(task_id: int, body: FailRequest, request: Request, worker: dict = Depends(authenticate)):
    def run(conn):
        extra = tuple(s for s in (settings.get(conn, "telegram_token"),) if s)
        state, not_before = queue.fail(conn, task_id, worker["id"], body.error_class, body.message,
                                       body.retry_after_seconds, extra_secrets=extra)
        return FailResponse(state=state, not_before=not_before)
    log.info("Worker %r reports %s for task %d", worker["name"], body.error_class, task_id)
    return _with_conn(request, run)
