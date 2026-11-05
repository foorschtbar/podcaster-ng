"""Small input parsers shared by the podcast and settings forms.

Each returns ``(value, error)``; ``error`` is a message without the field name.
"""

import re
from urllib.parse import urlparse

_INT = re.compile(r"^-?\d+$")


def parse_int(raw: str, *, minimum: int, maximum: int, optional: bool = False) -> tuple[int | None, str | None]:
    raw = raw.strip()
    if not raw:
        return (None, None) if optional else (None, "is required")
    if not _INT.match(raw) or len(raw) > 12:
        return None, "must be a whole number"
    value = int(raw)
    if not minimum <= value <= maximum:
        return None, f"must be between {minimum} and {maximum}"
    return value, None


def parse_url(raw: str, *, optional: bool = False) -> tuple[str | None, str | None]:
    raw = raw.strip()
    if not raw:
        return (None, None) if optional else (None, "is required")
    try:
        parts = urlparse(raw)
        ok = parts.scheme in ("http", "https") and bool(parts.hostname) and not re.search(r"\s", raw)
    except ValueError:
        ok = False
    if not ok or len(raw) > 2000:
        return None, "must be a valid http(s) URL"
    return raw, None
