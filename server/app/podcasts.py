"""Podcast storage and validation (no web code)."""

import re
import sqlite3
from datetime import datetime, timezone

from .validate import parse_int, parse_url

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED_SLUGS = {"new"}  # collide with UI routes
DEFAULT_SCAN_DEPTH = 20

# column -> (label, minimum, maximum, optional)
INT_FIELDS = {
    "scan_interval_min": (5, 10080, True),
    "scan_depth": (1, 200, False),
    "pause_min_s": (0, 3600, True),
    "pause_max_s": (0, 3600, True),
    "downloads_per_hour": (1, 1000, True),
    "keep_last": (1, 100000, True),
    "max_age_days": (1, 36500, True),
}


class SlugExists(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(form, defaults: dict, *, creating: bool) -> tuple[dict, dict]:
    """Return ``(data, errors)``. ``defaults`` are the global settings."""
    def text(key: str) -> str:
        return str(form.get(key) or "").strip()

    data: dict = {}
    errors: dict[str, str] = {}

    if creating:
        slug = text("id")
        if not slug:
            errors["id"] = "is required"
        elif len(slug) > 64 or not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
            errors["id"] = "must consist of lowercase letters, digits and single dashes (max. 64)"
        data["id"] = slug

    data["name"] = text("name")
    if not data["name"]:
        errors["name"] = "is required"
    elif len(data["name"]) > 200:
        errors["name"] = "must be at most 200 characters"
    data["author"] = text("author")
    if len(data["author"]) > 200:
        errors["author"] = "must be at most 200 characters"
    data["description"] = text("description")
    if len(data["description"]) > 4000:
        errors["description"] = "must be at most 4000 characters"

    data["playlist_url"], err = parse_url(text("playlist_url"))
    if err:
        errors["playlist_url"] = err
    data["image_url"], err = parse_url(text("image_url"), optional=True)
    data["image_url"] = data["image_url"] or ""
    if err:
        errors["image_url"] = err

    for key, (lo, hi, optional) in INT_FIELDS.items():
        raw = text(key)
        if key == "scan_depth" and not raw:
            raw = str(DEFAULT_SCAN_DEPTH)
        data[key], err = parse_int(raw, minimum=lo, maximum=hi, optional=optional)
        if err:
            errors[key] = err

    # Compare against the effective values (an empty field inherits the default).
    eff_min = data.get("pause_min_s") if data.get("pause_min_s") is not None else defaults["pause_min_s"]
    eff_max = data.get("pause_max_s") if data.get("pause_max_s") is not None else defaults["pause_max_s"]
    if "pause_min_s" not in errors and "pause_max_s" not in errors and eff_max < eff_min:
        errors["pause_max_s"] = f"must not be smaller than the minimum pause ({eff_min} s)"

    data["active"] = 1 if form.get("active") else 0
    return data, errors


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM podcasts ORDER BY lower(name), id").fetchall()


def get(conn: sqlite3.Connection, podcast_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()


def create(conn: sqlite3.Connection, data: dict) -> None:
    now = _now()
    cols = list(data) + ["created_at", "updated_at"]
    try:
        conn.execute(f"INSERT INTO podcasts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [*data.values(), now, now])
    except sqlite3.IntegrityError as exc:
        if get(conn, data["id"]):
            raise SlugExists from exc
        raise


def update(conn: sqlite3.Connection, podcast_id: str, data: dict) -> bool:
    data = {k: v for k, v in data.items() if k != "id"}  # the slug is immutable
    sets = ", ".join(f"{k} = ?" for k in data)
    cur = conn.execute(f"UPDATE podcasts SET {sets}, updated_at = ? WHERE id = ?",
                       [*data.values(), _now(), podcast_id])
    return cur.rowcount == 1


def set_active(conn: sqlite3.Connection, podcast_id: str, active: bool) -> bool:
    cur = conn.execute("UPDATE podcasts SET active = ?, updated_at = ? WHERE id = ?",
                       (int(active), _now(), podcast_id))
    return cur.rowcount == 1


def delete(conn: sqlite3.Connection, podcast_id: str) -> bool:
    return conn.execute("DELETE FROM podcasts WHERE id = ?", (podcast_id,)).rowcount == 1
