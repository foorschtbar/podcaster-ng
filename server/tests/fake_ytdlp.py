"""Stand-ins for yt-dlp and ffprobe, steered by words in the playlist URL (scan) or video id (download)."""

import json
import os
import stat
import sys
from pathlib import Path

YTDLP = r'''#!__PY__
import json, os, sys

args = sys.argv[1:]
url = args[-1]
if "--write-info-json" in args:                       # ---- download mode: files are written into the working directory
    vid = url.split("v=")[-1]
    def fail(text, code=1):
        sys.stderr.write(text + "\n"); sys.exit(code)
    if "hang" in vid: os.execv("/bin/sleep", ["sleep", "120"])
    if "bot" in vid: fail("ERROR: [youtube] %s: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication." % vid)
    if "premiere" in vid: fail("ERROR: [youtube] %s: This live event will begin in 3 hours." % vid)
    if "private" in vid: fail("ERROR: [youtube] %s: Private video. Sign in if you've been granted access to this video" % vid)
    if "removed" in vid: fail("ERROR: [youtube] %s: Video unavailable. This video has been removed by the uploader" % vid)
    if "net" in vid: fail("ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>")
    if "crash" in vid: fail("Traceback (most recent call last): boom", 2)
    duration = 3600
    codec = "mp3" if "mp3" in vid else "aac"
    marker = ("CORRUPT" if "corrupt" in vid else "DUR=%d;CODEC=%s" % (60 if "short" in vid else duration, codec))
    open("audio.m4a", "wb").write(b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42isom" + marker.encode() + b"\x00" * 2000)
    if "noinfo" not in vid:
        info = {"id": vid, "title": "Episode: " + vid, "description": "desc", "channel": "Chan", "duration": 0 if "noduration" in vid else duration,
                "release_date": "20260105", "chapters": [] if "nochap" in vid else [{"start_time": 0, "title": "Intro"}, {"start_time": 30, "title": "x" * 60}]}
        if "wrongid" in vid: info["id"] = "someoneelse"
        open("info.info.json", "w").write(json.dumps(info))
    if "nothumb" not in vid:
        from PIL import Image
        Image.new("RGB", (1280, 720), (200, 30, 30)).save("thumb.webp", "WEBP")
    sys.exit(0)
# ---- scan mode: a flat playlist on stdout
if "hang" in url: os.execv("/bin/sleep", ["sleep", "120"])
for word, text, code in (("bot", "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser", 1),
                         ("private", "ERROR: [youtube:tab] The playlist is private", 1), ("gone", "ERROR: [youtube:tab] The playlist does not exist.", 1),
                         ("net", "ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>", 1),
                         ("crash", "Traceback (most recent call last): boom", 2)):
    if word in url:
        sys.stderr.write(text + "\n"); sys.exit(code)
if "garbage" in url:
    print("this is not json"); sys.exit(0)
open(os.environ.get("FAKE_YTDLP_ARGS", os.devnull), "w").write(" ".join(args))
sys.stdout.write(open(os.environ["FAKE_YTDLP_OUTPUT"]).read())
'''

FFPROBE = r'''#!__PY__
import json, re, sys
path = sys.argv[-1]
data = open(path, "rb").read()
if b"CORRUPT" in data:
    sys.stderr.write("%s: Invalid data found when processing input\n" % path); sys.exit(1)
m = re.search(rb"DUR=(\d+);CODEC=(\w+)", data)
dur, codec = (int(m.group(1)), m.group(2).decode()) if m else (3600, "aac")
print(json.dumps({"streams": [{"codec_type": "audio", "codec_name": codec}], "format": {"duration": str(dur + 0.4)}}))
'''


def _write(path: Path, source: str) -> None:
    path.write_text(source.replace("__PY__", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def install(directory: Path, entries: list | None = None) -> Path:
    """Create ``yt-dlp`` and ``ffprobe`` in ``directory``; ``entries`` are what a scan prints."""
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "yt-dlp", YTDLP)
    _write(directory / "ffprobe", FFPROBE)
    (directory / "output.json").write_text(json.dumps({"_type": "playlist", "entries": entries or []}))
    return directory


def environment(directory: Path) -> dict:
    return {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}", "FAKE_YTDLP_OUTPUT": str(directory / "output.json"),
            "FAKE_YTDLP_ARGS": str(directory / "args.txt")}
