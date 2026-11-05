"""Cleans worker error texts before they are stored or shown: no tokens, credentials or control characters."""

import re

_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PATTERNS = [
    (re.compile(r"pnw_[A-Za-z0-9_-]{8,}"), "pnw_***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer ***"),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{25,}\b"), "***"),  # Telegram bot token shape
    (re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key|cookie|authorization|sessionid|sapisid|psid)"
                r"(\s*[=:]\s*)[^\s,;&\"']+"), r"\1\2***"),
]


def scrub(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    text = _CONTROL.sub("", text or "")
    for secret in extra_secrets:
        if secret:
            text = text.replace(secret, "***")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def clean(text: str, limit: int = 500, extra_secrets: tuple[str, ...] = ()) -> str:
    """Scrub, collapse whitespace runs and trim to ``limit`` characters."""
    text = re.sub(r"[ \t]+", " ", scrub(text, extra_secrets)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
