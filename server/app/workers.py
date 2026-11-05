"""Worker storage, tokens and online status (no web code)."""

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import date, datetime, timezone

from .scrub import clean
from .validate import parse_int

TOKEN_PREFIX = "pnw_"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}$")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class NameExists(Exception):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or now()).strftime(TIME_FORMAT)


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)  # 32 random bytes


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validate(form, defaults: dict) -> tuple[dict, dict]:
    """Return ``(data, errors)``; ``defaults`` are the global settings (for the pause comparison)."""
    def text(key: str) -> str:
        return str(form.get(key) or "").strip()

    data: dict = {}
    errors: dict[str, str] = {}
    data["name"] = text("name")
    if not NAME_RE.match(data["name"]):
        errors["name"] = "must be 1–60 characters: letters, digits, spaces, dots, dashes, underscores"
    data["tier"], err = parse_int(text("tier") or "1", minimum=1, maximum=99)
    if err:
        errors["tier"] = err
    for key, lo, hi in (("pause_min_s", 0, 3600), ("pause_max_s", 0, 3600), ("downloads_per_hour", 1, 1000)):
        data[key], err = parse_int(text(key), minimum=lo, maximum=hi, optional=True)
        if err:
            errors[key] = err
    eff_min = data["pause_min_s"] if data.get("pause_min_s") is not None else defaults["pause_min_s"]
    eff_max = data["pause_max_s"] if data.get("pause_max_s") is not None else defaults["pause_max_s"]
    if "pause_min_s" not in errors and "pause_max_s" not in errors and eff_max < eff_min:
        errors["pause_max_s"] = f"must not be smaller than the minimum pause ({eff_min} s)"
    data["active"] = 1 if form.get("active") else 0
    return data, errors


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM workers ORDER BY tier, lower(name)").fetchall()


def get(conn: sqlite3.Connection, worker_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()


def create(conn: sqlite3.Connection, data: dict) -> tuple[int, str]:
    """Insert a worker and return ``(id, token)``. The token exists only in the return value."""
    token = new_token()
    stamp = _stamp()
    cols = [*data, "token_hash", "created_at", "updated_at"]
    try:
        cur = conn.execute(f"INSERT INTO workers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           [*data.values(), hash_token(token), stamp, stamp])
    except sqlite3.IntegrityError as exc:
        if conn.execute("SELECT 1 FROM workers WHERE name = ?", (data["name"],)).fetchone():
            raise NameExists from exc
        raise
    return cur.lastrowid, token


def update(conn: sqlite3.Connection, worker_id: int, data: dict) -> bool:
    sets = ", ".join(f"{k} = ?" for k in data)
    try:
        cur = conn.execute(f"UPDATE workers SET {sets}, updated_at = ? WHERE id = ?",
                           [*data.values(), _stamp(), worker_id])
    except sqlite3.IntegrityError as exc:
        raise NameExists from exc
    return cur.rowcount == 1


def set_active(conn: sqlite3.Connection, worker_id: int, active: bool) -> bool:
    cur = conn.execute("UPDATE workers SET active = ?, updated_at = ? WHERE id = ?",
                       (int(active), _stamp(), worker_id))
    return cur.rowcount == 1


def regenerate_token(conn: sqlite3.Connection, worker_id: int) -> str | None:
    """Replace the token; the old one stops working immediately."""
    token = new_token()
    cur = conn.execute("UPDATE workers SET token_hash = ?, updated_at = ? WHERE id = ?",
                       (hash_token(token), _stamp(), worker_id))
    return token if cur.rowcount == 1 else None


def delete(conn: sqlite3.Connection, worker_id: int) -> bool:
    return conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,)).rowcount == 1


def find_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM workers WHERE token_hash = ?", (hash_token(token),)).fetchone()


def touch(conn: sqlite3.Connection, worker_id: int, moment: datetime | None = None, ip: str | None = None) -> None:
    if ip is None:
        conn.execute("UPDATE workers SET last_seen = ? WHERE id = ?", (_stamp(moment), worker_id))
    else:
        conn.execute("UPDATE workers SET last_seen = ?, last_ip = ? WHERE id = ?", (_stamp(moment), ip[:45], worker_id))


def record_device(conn: sqlite3.Connection, worker_id: int, device: dict, moment: datetime | None = None) -> None:
    """Store what the worker reported about its machine (only the fields it sent; text is cleaned)."""
    clean_device = {k: (clean(v, 100) if isinstance(v, str) else v) for k, v in device.items() if v is not None}
    if clean_device:
        conn.execute("UPDATE workers SET device = ?, device_at = ? WHERE id = ?",
                     (json.dumps(clean_device), _stamp(moment), worker_id))


def device_of(row: sqlite3.Row) -> dict:
    try:
        data = json.loads(row["device"])
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def record_hello(conn: sqlite3.Connection, worker_id: int, version: str, capabilities: list[str],
                 tools: dict[str, str]) -> None:
    conn.execute("UPDATE workers SET version = ?, capabilities = ?, tools = ? WHERE id = ?",
                 (version, json.dumps(capabilities), json.dumps(tools), worker_id))


def seconds_since_seen(row: sqlite3.Row, moment: datetime | None = None) -> float | None:
    if not row["last_seen"]:
        return None
    seen = datetime.strptime(row["last_seen"], TIME_FORMAT).replace(tzinfo=timezone.utc)
    return ((moment or now()) - seen).total_seconds()


def status(row: sqlite3.Row, online_seconds: int, moment: datetime | None = None) -> str:
    """'inactive', 'never' (never connected), 'online' or 'offline'."""
    if not row["active"]:
        return "inactive"
    age = seconds_since_seen(row, moment)
    if age is None:
        return "never"
    return "online" if age <= online_seconds else "offline"


def capabilities_of(row: sqlite3.Row) -> list[str]:
    try:
        return list(json.loads(row["capabilities"]))
    except (ValueError, TypeError):
        return []


def tools_of(row: sqlite3.Row) -> dict[str, str]:
    try:
        return dict(json.loads(row["tools"]))
    except (ValueError, TypeError):
        return {}


YTDLP_OLD_DAYS = 60


def version_differs(worker_version: str, server_version: str) -> bool:
    """True if the worker runs another build than the server. Unstamped builds ("dev") cannot be compared."""
    return bool(worker_version) and "dev" not in (worker_version, server_version) and worker_version != server_version


def ytdlp_age_days(tools: dict[str, str], today: date | None = None) -> int | None:
    """Age of the worker's yt-dlp in days (its version is a date like 2026.10.26), or None if unknown."""
    match = re.match(r"^(\d{4})\.(\d{2})\.(\d{2})", tools.get("yt-dlp", ""))
    if not match:
        return None
    try:
        released = date(*(int(g) for g in match.groups()))
    except ValueError:
        return None
    return ((today or datetime.now(timezone.utc).date()) - released).days
