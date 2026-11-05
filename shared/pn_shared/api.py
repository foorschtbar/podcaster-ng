"""Worker API v1 schemas (used by the server for validation and by the worker as client models)."""

from typing import Annotated

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
