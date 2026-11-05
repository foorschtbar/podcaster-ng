"""Entry point: ``python -m app``."""

import logging
import sys

import uvicorn

from .config import Config
from .logging_setup import setup_logging
from .main import create_app
from .migrations import MigrationError, migrate


def main() -> int:
    config = Config.from_env()
    setup_logging(config.log_level)
    log = logging.getLogger("app")
    try:
        migrate(config.db_path, config.backup_dir)
    except MigrationError as exc:
        log.error("Startup aborted: %s", exc)
        return 1
    uvicorn.run(create_app(config), host="0.0.0.0", port=8080, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
