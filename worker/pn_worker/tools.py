"""Detects the external tools a worker may have (yt-dlp, ffmpeg, deno) and their versions."""

import re
import shutil
import subprocess

COMMANDS = {"ytdlp": ("yt-dlp", ["--version"]), "ffmpeg": ("ffmpeg", ["-version"]), "deno": ("deno", ["--version"])}
_VERSION = re.compile(r"\d[\w.+-]*")


def _version(binary: str, args: list[str]) -> str | None:
    path = shutil.which(binary)
    if not path:
        return None
    try:
        out = subprocess.run([path, *args], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    first = out.strip().splitlines()[0] if out.strip() else ""
    match = _VERSION.search(first)
    return match.group(0)[:50] if match else None


def detect() -> dict[str, str]:
    """Versions of the tools that are installed, keyed ytdlp / ffmpeg / deno."""
    found = {}
    for key, (binary, args) in COMMANDS.items():
        version = _version(binary, args)
        if version:
            found[key] = version
    return found
