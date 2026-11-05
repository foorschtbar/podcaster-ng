"""Classifies public requests for the hit counters.

Counting rules (documented in the README):
- only successful GET responses (200/206) count; HEAD, 304 and errors do not;
- ``feed``: GET /<podcast>.xml;
- ``file``: only audio (.m4a). Artwork and chapter files are not downloads;
- a download is counted once: a request without Range, or whose first range
  starts at byte 0. Later ranges of the same download add their bytes but no count;
- bytes are the size of each response (its Content-Length).
"""

import re

FEED_RE = re.compile(r"^/([a-z0-9]+(?:-[a-z0-9]+)*)\.xml$")
FILE_RE = re.compile(r"^/files/([a-z0-9]+(?:-[a-z0-9]+)*)/([^/]+)$")


def classify(path: str) -> tuple[str, str, str | None] | None:
    """Return ``(kind, podcast_id, file_name)`` for public feed/audio paths, else None."""
    m = FEED_RE.match(path)
    if m:
        return "feed", m.group(1), None
    m = FILE_RE.match(path)
    if m and m.group(2).lower().endswith(".m4a"):
        return "file", m.group(1), m.group(2)
    return None


def starts_download(range_header: str | None) -> bool:
    """True for a full request or a range that starts at byte 0."""
    if not range_header or not range_header.strip().lower().startswith("bytes="):
        return True
    first = range_header.split("=", 1)[1].split(",")[0].strip()
    if first.startswith("-"):  # suffix range: the tail of a file, never the start of a download
        return False
    start = first.split("-")[0].strip()
    return start.isdigit() and int(start) == 0
