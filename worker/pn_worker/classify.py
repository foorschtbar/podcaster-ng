"""Maps yt-dlp error output to the error classes the server understands."""

# (error class, lower-case fragments); the first class with a matching fragment wins
RULES = [
    ("not_yet_available", ("premieres in", "live event will begin", "this live event", "will begin in", "is not yet available")),
    ("private", ("private video", "this video is private", "playlist is private", "sign in if you've been granted access")),
    ("bot_check", ("sign in to confirm", "not a bot", "http error 429", "too many requests")),
    ("removed", ("has been removed", "video unavailable", "no longer available", "this video is not available",
                 "account associated with this video has been terminated", "playlist does not exist", "does not exist",
                 "has been deleted", "copyright")),
    ("timeout", ("timed out", "timeout")),
    ("network", ("temporary failure in name resolution", "name or service not known", "connection refused",
                 "connection reset", "network is unreachable", "no route to host", "unable to connect", "ssl",
                 "remote end closed", "http error 5", "bad gateway", "unable to download", "proxy")),
]


def classify(stderr: str) -> str:
    text = (stderr or "").lower()
    for error_class, fragments in RULES:
        if any(fragment in text for fragment in fragments):
            return error_class
    return "internal"
