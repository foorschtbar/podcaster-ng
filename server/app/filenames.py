"""File names for episode packages, compatible with the old Podcaster.

The old project let yt-dlp name the files (``%(title)s.%(ext)s``). The server has no yt-dlp, so the
title sanitising is ported here; a test compares it with yt-dlp itself and with the real file names.
"""

import re

MAX_BASE_BYTES = 200  # leaves room for ".square.jpg" etc. within the usual 255-byte limit
_TIMESTAMP = re.compile(r"[0-9]+(?::[0-9]+)+")
_FULLWIDTH = '"*:<>?|/\\'
_STRIP = r"(?:\0.|[ _-])*"


def _replace_insane(char: str) -> str:
    if char == "\n":
        return "\0 "
    if char in _FULLWIDTH:  # look-alike characters from the full-width block, as yt-dlp does
        return {"/": "\u29f8", "\\": "\u29f9"}.get(char, chr(ord(char) + 0xFEE0))
    if ord(char) < 32 or ord(char) == 127:
        return ""
    return char


def sanitize_title(title: str) -> str:
    """The file name part yt-dlp derives from a title (the filename-sanitising of ``%(title)s``)."""
    s = _TIMESTAMP.sub(lambda m: m.group(0).replace(":", "_"), title or "")
    result = "".join(map(_replace_insane, s))
    result = re.sub(r"(\0.)(?:(?=\1)..)+", r"\1", result)  # repeated substitute characters
    result = re.sub(f"^\0.{_STRIP}|{_STRIP}\0.$", "", result)  # substitute characters at the ends
    return result.replace("\0", "") or "_"


def limit_bytes(name: str, limit: int = MAX_BASE_BYTES) -> str:
    """Shorten to ``limit`` UTF-8 bytes without cutting a character in half."""
    raw = name.encode("utf-8")
    if len(raw) <= limit:
        return name
    return raw[:limit].decode("utf-8", errors="ignore").rstrip(" .") or "_"


def base_name(title: str, video_id: str, taken: set[str]) -> str:
    """The base name for a new package: the sanitised title, or ``Title [video id]`` if that name is taken.

    ``taken`` holds base names that already exist in the podcast's directory (compared case-insensitively).
    """
    name = sanitize_title(title if (title or "").strip() else video_id)
    # yt-dlp keeps a leading dot, but such a file would be a hidden file that the file server refuses to serve
    name = limit_bytes(name.lstrip(".").lstrip() or video_id)
    if name.lower() not in {t.lower() for t in taken}:
        return name
    return limit_bytes(name, MAX_BASE_BYTES - len(video_id) - 3) + f" [{video_id}]"


def base_of(file_name: str | None) -> str | None:
    """Base name of an episode's audio file (without ``.m4a``)."""
    if file_name and file_name.lower().endswith(".m4a"):
        return file_name[: -len(".m4a")]
    return None
