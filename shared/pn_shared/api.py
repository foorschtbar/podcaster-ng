"""Worker API v1 schemas (used by the server for validation and by the worker as client models)."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class HelloRequest(BaseModel):
    worker_version: Short
    ytdlp_version: Short | None = None
    ffmpeg_version: Short | None = None
    deno_version: Short | None = None
    capabilities: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=20)


class WorkerInfo(BaseModel):
    id: int
    name: str
    tier: int


class Pacing(BaseModel):
    """Effective limits for this worker (its own value, or the server default)."""
    pause_min_s: int
    pause_max_s: int
    downloads_per_hour: int


class HelloResponse(BaseModel):
    api_version: int
    server_time: str  # ISO 8601, UTC
    worker: WorkerInfo
    pacing: Pacing
    lease_seconds: int
    ping_interval_seconds: int


class PingResponse(BaseModel):
    ok: bool = True
    server_time: str


# --- queue: claim, heartbeat, scan result, fail -----------------------------------------------------

TaskType = Literal["scan", "download"]
ErrorClass = Literal["bot_check", "not_yet_available", "private", "removed", "network", "timeout", "internal",
                     "incomplete"]
VideoId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class ClaimRequest(BaseModel):
    types: list[TaskType] = Field(default_factory=lambda: ["scan"], min_length=1, max_length=2)
    # Tasks this worker gave up on without being able to tell the server (for instance because it was restarting).
    # The server takes them back at once instead of waiting for their lease to run out.
    dropped: list[int] = Field(default_factory=list, max_length=20)


class TaskPodcast(BaseModel):
    id: str
    name: str
    playlist_url: str
    scan_depth: int


class TaskInfo(BaseModel):
    id: int
    type: TaskType
    podcast: TaskPodcast
    video_id: str | None = None
    title: str = ""
    attempt: int
    lease_seconds: int
    lease_until: str
    pacing: Pacing


class ClaimResponse(BaseModel):
    task: TaskInfo | None = None
    retry_after_seconds: int = 15  # when to ask again if there was no task


class HeartbeatRequest(BaseModel):
    progress: Annotated[str, StringConstraints(max_length=200)] | None = None


class HeartbeatResponse(BaseModel):
    lease_until: str


class VideoInfo(BaseModel):
    id: VideoId
    title: Annotated[str, StringConstraints(max_length=500)] = ""
    upload_date: Annotated[str, StringConstraints(max_length=20)] | None = None
    live_status: Annotated[str, StringConstraints(max_length=30)] | None = None  # e.g. is_upcoming, is_live
    release_timestamp: int | None = None  # epoch seconds, for premieres


class ScanResult(BaseModel):
    videos: list[VideoInfo] = Field(default_factory=list, max_length=1000)


class ScanResultResponse(BaseModel):
    created: int  # download tasks created
    known: int    # already known (episode or task)
    skipped: int  # beyond the scan depth


class FailRequest(BaseModel):
    error_class: ErrorClass
    message: Annotated[str, StringConstraints(max_length=4000)] = ""
    retry_after_seconds: int | None = Field(default=None, ge=0, le=7 * 86400)


class FailResponse(BaseModel):
    state: str
    not_before: str | None = None


# --- packages: upload and complete ---------------------------------------------------------------------------

PackageRole = Literal["audio", "info", "thumb", "square", "chapters"]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class UploadResponse(BaseModel):
    role: PackageRole
    size: int
    sha256: Sha256


class FileEntry(BaseModel):
    role: PackageRole
    size: int = Field(ge=1)
    sha256: Sha256


class CompleteRequest(BaseModel):
    """What the worker believes it uploaded; the server checks it against what is in staging."""
    files: list[FileEntry] = Field(min_length=1, max_length=5)


class CompleteResponse(BaseModel):
    video_id: str
    audio_file: str
