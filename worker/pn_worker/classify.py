"""Maps yt-dlp error output to the error classes the server understands."""

import re

# (error class, lower-case fragments); the first class with a matching fragment wins
RULES = [
    ("not_yet_available", ("premieres in", "live event will begin", "this live event", "will begin in", "is not yet available")),
    ("private", ("private video", "this video is private", "playlist is private", "sign in if you've been granted access")),
    ("bot_check", ("not a bot", "http error 429", "too many requests", "http error 403")),
    ("private", ("members-only", "join this channel", "only available to music premium")),
    ("removed", ("has been removed", "video unavailable", "no longer available", "this video is not available",
                 "account associated with this video has been terminated", "playlist does not exist", "does not exist",
                 "has been deleted", "copyright")),
    ("timeout", ("timed out", "timeout")),
    ("network", ("temporary failure in name resolution", "name or service not known", "connection refused",
                 "connection reset", "network is unreachable", "no route to host", "unable to connect", "ssl",
                 "remote end closed", "http error 5", "bad gateway", "unable to download", "proxy")),
]


_WAIT = re.compile(r"\bin\s+(\d+)\s*(second|minute|hour|day)s?\b", re.I)
_UNIT = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
MARGIN = 300  # start a little after the announced time


def wait_seconds(stderr: str) -> int | None:
    """Seconds until a premiere/live stream starts ("... will begin in 3 hours"), plus a margin; None if unknown."""
    m = _WAIT.search(stderr or "")
    return int(m.group(1)) * _UNIT[m.group(2).lower()] + MARGIN if m else None


def classify(stderr: str) -> str:
    text = (stderr or "").lower()
    for error_class, fragments in RULES:
        if any(fragment in text for fragment in fragments):
            return error_class
    return "internal"
