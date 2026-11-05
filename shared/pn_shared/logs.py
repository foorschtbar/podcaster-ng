"""Structured (JSON lines) logging to stdout, with optional secret redaction."""

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class RedactFilter(logging.Filter):
    """Replaces the given secrets in every message (defence in depth; secrets are not logged on purpose)."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            text = record.getMessage()
            for secret in self.secrets:
                text = text.replace(secret, "***")
            record.msg, record.args = text, None
            if record.exc_info:  # keep tracebacks free of secrets too
                record.exc_text = None
        return True


def setup_logging(level: str = "INFO", redact: list[str] | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    if redact:
        handler.addFilter(RedactFilter(redact))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level if level in logging.getLevelNamesMapping() else "INFO")
