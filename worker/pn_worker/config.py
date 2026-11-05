"""Worker configuration from environment variables."""

import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    server_url: str
    token: str
    log_level: str = "INFO"
    ping_interval: float | None = None  # None: the interval the server announces
    backoff_start: float = 2.0
    backoff_max: float = 60.0
    alive_file: str = "/tmp/pn_worker_alive"

    @classmethod
    def from_env(cls) -> "Config":
        server_url = (os.environ.get("SERVER_URL") or "").strip().rstrip("/")
        token = (os.environ.get("WORKER_TOKEN") or "").strip()
        if not server_url.startswith(("http://", "https://")):
            raise ConfigError("SERVER_URL must be set to the server's http(s) address")
        if not token:
            raise ConfigError("WORKER_TOKEN must be set (create the worker in the admin UI to get one)")
        return cls(server_url=server_url, token=token, log_level=(os.environ.get("LOG_LEVEL") or "INFO").upper())
