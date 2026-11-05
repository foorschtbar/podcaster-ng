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
    poll_interval: float | None = None  # idle wait between claims; None: the server's hint
    scan_timeout: float = 300.0         # hard limit for one scan (the yt-dlp process is killed)
    ytdlp_proxy: str | None = None
    heartbeat_interval: float | None = None  # None: a third of the lease, at most 60 s
    download_timeout: float = 3600.0    # hard limit for one download task (yt-dlp, ffmpeg, upload)
    cookies_file: str | None = None     # optional yt-dlp cookies; never logged
    work_dir: str = "/tmp/pn-work"
    upload_retries: int = 3             # how often the server may reject a file before it counts as incomplete
    delivery_timeout: float = 900.0     # how long uploads, the final call and failure reports wait for an unreachable server

    @classmethod
    def from_env(cls) -> "Config":
        server_url = (os.environ.get("SERVER_URL") or "").strip().rstrip("/")
        token = (os.environ.get("WORKER_TOKEN") or "").strip()
        if not server_url.startswith(("http://", "https://")):
            raise ConfigError("SERVER_URL must be set to the server's http(s) address")
        if not token:
            raise ConfigError("WORKER_TOKEN must be set (create the worker in the admin UI to get one)")
        try:
            scan_timeout = float(os.environ.get("SCAN_TIMEOUT") or 300)
        except ValueError:
            raise ConfigError("SCAN_TIMEOUT must be a number of seconds") from None
        try:
            poll = float(os.environ["POLL_INTERVAL"]) if os.environ.get("POLL_INTERVAL") else None
        except ValueError:
            raise ConfigError("POLL_INTERVAL must be a number of seconds") from None
        try:
            download_timeout = float(os.environ.get("DOWNLOAD_TIMEOUT") or 3600)
        except ValueError:
            raise ConfigError("DOWNLOAD_TIMEOUT must be a number of seconds") from None
        try:
            delivery_timeout = float(os.environ.get("DELIVERY_TIMEOUT") or 900)
        except ValueError:
            raise ConfigError("DELIVERY_TIMEOUT must be a number of seconds") from None
        cookies = (os.environ.get("YTDLP_COOKIES") or "").strip() or None
        if cookies and not os.path.isfile(cookies):
            raise ConfigError("YTDLP_COOKIES points to a file that does not exist")
        return cls(server_url=server_url, token=token, poll_interval=poll, download_timeout=download_timeout,
                   cookies_file=cookies, work_dir=os.environ.get("WORK_DIR") or "/tmp/pn-work", log_level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
                   scan_timeout=scan_timeout, delivery_timeout=delivery_timeout, ytdlp_proxy=(os.environ.get("YTDLP_PROXY") or "").strip() or None)
