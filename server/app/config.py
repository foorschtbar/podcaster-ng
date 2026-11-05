"""Configuration from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    base_url: str
    log_level: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "podcaster.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR") or "/data"),
            base_url=(os.environ.get("BASE_URL") or "").rstrip("/"),
            log_level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
        )
