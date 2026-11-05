"""Configuration from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    base_url: str
    log_level: str
    admin_user: str = ""
    admin_password: str = ""
    session_secret: str = ""
    import_dir: Path = Path("/import")
    run_scheduler: bool = True
    scheduler_tick_seconds: float = 15.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "podcaster.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def admin_configured(self) -> bool:
        return bool(self.admin_user and self.admin_password)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR") or "/data"),
            base_url=(os.environ.get("BASE_URL") or "").rstrip("/"),
            log_level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
            admin_user=os.environ.get("ADMIN_USER") or "",
            admin_password=os.environ.get("ADMIN_PASSWORD") or "",
            session_secret=os.environ.get("SESSION_SECRET") or "",
            import_dir=Path(os.environ.get("IMPORT_DIR") or "/import"),
            run_scheduler=(os.environ.get("SCHEDULER") or "1") != "0",
        )
