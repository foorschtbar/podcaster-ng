"""Builds a miniature copy of the old Podcaster's data for import tests."""

import json
from pathlib import Path

CONFIG = """\
baseurl: https://old.example.org
downloads: ./data/downloads
interval: 3600
cookiefile: ./cookies.txt
jobs:
  - id: abgt
    name: ABGT
    type: youtube
    url: https://www.youtube.com/playlist?list=PL1
    description: Above & Beyond
  - id: asot
    name: ASOT
    type: youtube
    url: https://www.youtube.com/playlist?list=PL2
    image: https://img.example.org/asot.jpg
  - id: mix
    name: Mixes
    type: mixcloud
    url: https://www.mixcloud.com/x/
  - id: Bad_ID
    name: Broken
    type: youtube
    url: https://www.youtube.com/playlist?list=PL3
"""

ABGT_1 = "ABGT 100 - Live & Direct.info.json"
ASOT_1 = "ASOT 1 #?% test.info.json"


def write_episode(directory: Path, base: str, info: dict, *, audio=b"A" * 1000, thumb=".webp",
                  square=False, chapters_file=None) -> None:
    (directory / f"{base}.info.json").write_text(json.dumps(info))
    if audio is not None:
        (directory / f"{base}.m4a").write_bytes(audio)
    if thumb:
        (directory / f"{base}{thumb}").write_bytes(b"T" * 50)
    if square:
        (directory / f"{base}.square.jpg").write_bytes(b"S" * 60)
    if chapters_file is not None:
        (directory / f"{base}.chapters.json").write_text(json.dumps(chapters_file))


def build_old_project(root: Path) -> tuple[Path, Path]:
    """Returns (config.yml, downloads dir) below ``root``."""
    (root / "data" / "downloads").mkdir(parents=True)
    config = root / "config.yml"
    config.write_text(CONFIG)
    (root / "cookies.txt").write_text("# Netscape cookie file\nSECRET-COOKIE-VALUE\n")
    dl = root / "data" / "downloads"

    abgt = dl / "abgt"
    abgt.mkdir()
    (abgt / "meta.info.json").write_text(json.dumps({
        "uploader": "Anjunabeats", "webpage_url": "https://www.youtube.com/playlist?list=PL1",
        "thumbnails": [{"url": "https://img.example.org/small.jpg", "width": 100},
                       {"url": "https://img.example.org/big.jpg?a=1&b=2", "width": 900}]}))
    write_episode(abgt, "ABGT 100 - Live & Direct", {
        "id": "vid100", "title": "ABGT 100 - Live & Direct", "description": "Desc <b>& more]]> end",
        "channel": "Above & Beyond", "duration": 3600, "release_date": "20240105",
        "chapters": [{"start_time": 0, "title": "Intro"}, {"start_time": 61.5, "title": "x" * 60}]},
        square=True)
    write_episode(abgt, "ABGT 101", {
        "id": "vid101", "title": "ABGT 101", "description": "second", "channel": "Above & Beyond",
        "duration": 1800, "upload_date": "20240112"}, audio=b"B" * 2000, thumb=".jpg",
        chapters_file={"version": "1.2.0", "chapters": []})
    write_episode(abgt, "Orphan", {"id": "orphan1", "title": "Orphan"}, audio=None)  # premiere never finished
    (abgt / "archive.txt").write_text("youtube vid100\nyoutube vid101\nyoutube gone1\nyoutube orphan1\n")

    asot = dl / "asot"
    asot.mkdir()
    write_episode(asot, "ASOT 1 #?% test", {
        "id": "a1", "title": "ASOT 1", "description": "", "channel": "Armin", "duration": 7200,
        "release_date": "20230101"}, audio=b"C" * 3000, thumb=".png", square=True)
    return config, dl
