"""RSS feed rendering.

Ported from the old Podcaster's template: same elements, GUID = file URL,
enclosure type audio/x-m4a, square episode artwork, Podcasting 2.0 chapters.
Text is XML-escaped by Jinja instead of by hand.
"""

import re
import sqlite3
from datetime import date, datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

ENCLOSURE_TYPE = "audio/x-m4a"
CHAPTERS_TYPE = "application/json+chapters"
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _clean(value):
    return _INVALID_XML.sub("", value) if isinstance(value, str) else value


_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent / "templates")),
    autoescape=select_autoescape(["xml"]), finalize=_clean, trim_blocks=True, lstrip_blocks=True,
)


def file_url(base_url: str, podcast_id: str, name: str) -> str:
    return f"{base_url}/files/{podcast_id}/{quote(name)}"


def _rfc2822_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        day = date.fromisoformat(iso[:10])
    except ValueError:
        return ""
    return format_datetime(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))


def _rfc2822_timestamp(stamp: str) -> str:
    return format_datetime(datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))


def build_feed(podcast: sqlite3.Row, episodes: list[sqlite3.Row], base_url: str) -> str:
    base_url = base_url.rstrip("/")
    pid = podcast["id"]
    author = podcast["author"] or podcast["name"]
    # Stable between requests: the newest change, not "now".
    last_change = max([podcast["updated_at"], *(e["updated_at"] for e in episodes)])

    channel = {
        "title": podcast["name"],
        "link": podcast["playlist_url"],
        "image": podcast["image_url"],
        "description": podcast["description"] or podcast["name"],
        "author": author,
        "last_build_date": _rfc2822_timestamp(last_change),
    }
    items = []
    for e in episodes:
        url = file_url(base_url, pid, e["audio_file"])
        if e["square_file"]:
            image = file_url(base_url, pid, e["square_file"])
        elif e["thumb_file"]:
            image = file_url(base_url, pid, e["thumb_file"])
        else:
            image = podcast["image_url"]
        items.append({
            "title": e["title"],
            "description": e["description"],
            "author": e["author"] or author,
            "image": image,
            "chapters_url": file_url(base_url, pid, e["chapters_file"]) if e["chapters_file"] else "",
            "url": url,
            "length": e["size_bytes"],
            "type": ENCLOSURE_TYPE,
            "guid": url,
            "pub_date": _rfc2822_date(e["published_at"]),
            "duration": e["duration_s"],
        })
    return _env.get_template("feed.xml").render(channel=channel, items=items, chapters_type=CHAPTERS_TYPE)
