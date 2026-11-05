"""A stand-in for the yt-dlp command line, chosen by words in the playlist URL."""

import json
import os
import stat
from pathlib import Path

SCRIPT = r"""#!/bin/sh
for a; do url=$a; done
case "$url" in
  *hang*)  exec sleep 120 ;;
  *bot*)   echo "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser" >&2; exit 1 ;;
  *private*) echo "ERROR: [youtube:tab] The playlist is private" >&2; exit 1 ;;
  *gone*)  echo "ERROR: [youtube:tab] The playlist does not exist." >&2; exit 1 ;;
  *net*)   echo "ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>" >&2; exit 1 ;;
  *garbage*) echo "this is not json"; exit 0 ;;
  *crash*) echo "Traceback (most recent call last): boom" >&2; exit 2 ;;
esac
echo "$@" > "${FAKE_YTDLP_ARGS:-/dev/null}"
cat "$FAKE_YTDLP_OUTPUT"
"""


def install(directory: Path, entries: list | None = None) -> Path:
    """Create an executable ``yt-dlp`` in ``directory`` that prints ``entries`` as a flat playlist."""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "yt-dlp"
    binary.write_text(SCRIPT)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    output = directory / "output.json"
    output.write_text(json.dumps({"_type": "playlist", "entries": entries or []}))
    return directory


def environment(directory: Path) -> dict:
    return {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}", "FAKE_YTDLP_OUTPUT": str(directory / "output.json"),
            "FAKE_YTDLP_ARGS": str(directory / "args.txt")}
