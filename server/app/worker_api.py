"""Worker API v1 under /api/worker (bearer token). Separate from the public and admin routes."""

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from pn_shared import API_VERSION
from pn_shared.api import (ClaimRequest, ClaimResponse, Device, FailRequest, FailResponse, HeartbeatRequest, HeartbeatResponse,
                           HelloRequest, HelloResponse, Pacing, PingResponse, ScanResult, ScanResultResponse, TaskInfo,
                           TaskPodcast, WorkerInfo, CompleteRequest, CompleteResponse, PackageRole, Sha256, UploadResponse)

from . import packages, queue, settings, workers
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
        workers.touch(conn, row["id"], ip=client)
        return dict(row)
    finally:
        conn.close()


@router.get("/ping", response_model=PingResponse)
def ping(worker: dict = Depends(authenticate)) -> PingResponse:
    return PingResponse(server_time=_server_time())


@router.post("/status", response_model=PingResponse)
def status(body: Device, request: Request, worker: dict = Depends(authenticate)) -> PingResponse:
    """The worker's machine state, sent regularly. Counts as "seen" like a ping."""
    def run(conn):
        workers.record_device(conn, worker["id"], body.model_dump(exclude_none=True))
    _with_conn(request, run)
    return PingResponse(server_time=_server_time())


@router.post("/hello", response_model=HelloResponse)
def hello(body: HelloRequest, request: Request, worker: dict = Depends(authenticate)) -> HelloResponse:
    config = request.app.state.config
    tools = {name: version for name, version in (("yt-dlp", body.ytdlp_version), ("ffmpeg", body.ffmpeg_version),
                                                 ("deno", body.deno_version)) if version}
    conn = connect(config.db_path)
    try:
        workers.record_hello(conn, worker["id"], body.worker_version, body.capabilities, tools)
        if body.device:
            workers.record_device(conn, worker["id"], body.device.model_dump(exclude_none=True))
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
        result = queue.claim(conn, worker, sorted(set(body.types)), defaults, defaults["lease_seconds"], dropped=body.dropped)
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
        if body.device:
            workers.record_device(conn, worker["id"], body.device.model_dump(exclude_none=True))
        return HeartbeatResponse(lease_until=queue.heartbeat(conn, task_id, worker["id"], lease, progress=body.progress))
    return _with_conn(request, run)


@router.post("/tasks/{task_id}/scan-result", response_model=ScanResultResponse)
def scan_result(task_id: int, body: ScanResult, request: Request, worker: dict = Depends(authenticate)):
    videos = [v.model_dump() for v in body.videos]
    return _with_conn(request, lambda conn: ScanResultResponse(**queue.complete_scan(conn, task_id, worker["id"], videos)))


@router.post("/tasks/{task_id}/fail", response_model=FailResponse)
def fail(task_id: int, body: FailRequest, request: Request, worker: dict = Depends(authenticate)):
    def run(conn):
        all_settings = settings.get_all(conn, {"base_url": request.app.state.config.base_url})
        extra = tuple(s for s in (all_settings["telegram_token"],) if s)
        state, not_before = queue.fail(conn, task_id, worker["id"], body.error_class, body.message,
                                       body.retry_after_seconds, extra_secrets=extra,
                                       policy=queue.Policy.from_settings(all_settings))
        return FailResponse(state=state, not_before=not_before)
    log.info("Worker %r reports %s for task %d", worker["name"], body.error_class, task_id)
    return _with_conn(request, run)


# --- package upload and complete ------------------------------------------------------------------------------

def _prepare_upload(request: Request, task_id: int, worker_id: int, role: str) -> tuple[Path, int]:
    """Ownership check plus staging directory and size limit for the role."""
    config = request.app.state.config
    conn = connect(config.db_path)
    try:
        try:
            task, attempt_id = queue.owned_attempt(conn, task_id, worker_id)
        except queue.QueueError as exc:
            raise _queue_error(exc) from None
        if task["type"] != "download":
            raise HTTPException(409, "Only download tasks take uploads")
        limit = packages.size_limit(role, settings.get(conn, "upload_limit_mb"))
    finally:
        conn.close()
    directory = packages.staging_dir_for(config.staging_dir, task_id, attempt_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory, limit


@router.post("/tasks/{task_id}/upload", response_model=UploadResponse)
async def upload(task_id: int, request: Request, role: PackageRole, size: int = Query(ge=1), sha256: Sha256 = Query(),
                 ext: str = Query(default="", max_length=5, pattern=r"^[A-Za-z0-9]*$"), worker: dict = Depends(authenticate)):
    """Receive one file of a package as the raw request body (streamed to disk, hashed on the fly)."""
    directory, limit = await run_in_threadpool(_prepare_upload, request, task_id, worker["id"], role)
    if size > limit:
        raise HTTPException(413, f"The {role} file is larger than the allowed {limit >> 20} MB")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) != size:
        raise HTTPException(400, "Content-Length does not match the declared size")
    if role == "thumb" and ext.lower() not in packages.THUMB_EXTS:
        raise HTTPException(422, "The thumbnail needs an extension: webp, jpg, jpeg or png")
    part = directory / f"{role}.part"
    digest, total = hashlib.sha256(), 0
    try:
        with part.open("wb") as out:
            async for chunk in request.stream():
                total += len(chunk)
                if total > size:
                    raise HTTPException(400, "More data than declared")
                digest.update(chunk)
                await run_in_threadpool(out.write, chunk)
        if total != size:
            raise HTTPException(422, f"Received {total} bytes, declared {size}")
        if digest.hexdigest() != sha256:
            raise HTTPException(422, "The SHA-256 of the received file does not match")
        try:
            await run_in_threadpool(packages.sniff, role, part, ext)
        except packages.PackageError as exc:
            raise HTTPException(422, str(exc)) from None
        staged = packages.Staged(role, directory / f"{role}.bin", total, sha256, ext.lower())
        await run_in_threadpool(lambda: (part.replace(staged.path), packages.store_meta(directory, staged)))
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return UploadResponse(role=role, size=total, sha256=sha256)


def _complete(request: Request, task_id: int, worker: dict, body: CompleteRequest) -> CompleteResponse:
    config = request.app.state.config
    conn = connect(config.db_path)
    try:
        try:
            task, attempt_id = queue.owned_attempt(conn, task_id, worker["id"])
        except queue.QueueError as exc:
            raise _queue_error(exc) from None
        if task["type"] != "download":
            raise HTTPException(409, "Only download tasks can be completed")
        directory = packages.staging_dir_for(config.staging_dir, task_id, attempt_id)
        manifest = {f.role: (f.size, f.sha256) for f in body.files}
        try:
            files, info = packages.verify_package(directory, manifest, task["video_id"])
        except packages.PackageError as exc:
            # A broken package never becomes an episode: the attempt is recorded as "incomplete" and retried.
            queue.fail(conn, task_id, worker["id"], "incomplete", f"Package rejected: {exc}")
            packages.remove(directory)
            log.warning("Package of task %d rejected: %s", task_id, exc)
            raise HTTPException(422, f"Package rejected: {exc}") from None
        target = config.media_dir / task["podcast_id"]
        names = packages.plan_names(info["title"], task["video_id"], files, packages.taken_bases(conn, task["podcast_id"], target))
        moved = packages.move_into_media(files, names, target)
        try:
            policy = queue.Policy.from_settings(settings.get_all(conn, {"base_url": config.base_url}))
            queue.complete_download(conn, task_id, worker["id"], packages.episode_fields(info, names, files), policy=policy)
        except BaseException as exc:
            packages.rollback_move(moved)                      # back to staging: the worker can call complete again
            if isinstance(exc, queue.QueueError):
                raise _queue_error(exc) from None
            raise
        packages.remove(directory)
    finally:
        conn.close()
    log.info("Task %d: episode %r published by worker %r", task_id, names["audio"], worker["name"])
    return CompleteResponse(video_id=task["video_id"], audio_file=names["audio"])


@router.post("/tasks/{task_id}/complete", response_model=CompleteResponse)
def complete(task_id: int, body: CompleteRequest, request: Request, worker: dict = Depends(authenticate)):
    """Verify the staged package and publish it: files into the media directory, then the episode row."""
    return _complete(request, task_id, worker, body)
