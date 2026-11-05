"""Entry point: ``python -m pn_worker``."""

import logging
import signal
import sys
import threading

from pn_shared.logs import setup_logging

from .config import Config, ConfigError
from .main import run


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        setup_logging()
        logging.getLogger("pn_worker").error("%s", exc)
        return 2
    setup_logging(config.log_level, redact=[config.token])
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per ping is noise
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    run(config, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
