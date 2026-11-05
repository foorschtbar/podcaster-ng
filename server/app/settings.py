"""Global settings: typed getters with defaults, validation and storage.

Values live in the ``settings`` table as text. A missing row means "default";
the default of ``base_url`` may come from the environment (``env_defaults``).
Secrets are stored like any other value but never rendered back by the UI.
"""

import re
import sqlite3
from dataclasses import dataclass

from .validate import parse_int, parse_url


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # "int" | "bool" | "text" | "url" | "secret" | "time"
    group: str
    default: object = None
    minimum: int = 0
    maximum: int = 0
    optional: bool = False
    unit: str = ""
    hint: str = ""


FIELDS = (
    Field("base_url", "Base URL", "url", "General", "", optional=True,
          hint="Public URL used in feeds, e.g. https://podcaster.example.org. Empty: use BASE_URL from the environment."),
    Field("scan_interval_min", "Scan interval", "int", "Podcast defaults", 60, 5, 10080, unit="minutes"),
    Field("pause_min_s", "Pause between downloads, min", "int", "Podcast defaults", 5, 0, 3600, unit="seconds"),
    Field("pause_max_s", "Pause between downloads, max", "int", "Podcast defaults", 20, 0, 3600, unit="seconds"),
    Field("downloads_per_hour", "Downloads per worker and hour", "int", "Podcast defaults", 3, 1, 1000),
    Field("keep_last", "Keep last episodes", "int", "Retention defaults", None, 1, 100000, optional=True,
          hint="Empty: keep all."),
    Field("max_age_days", "Delete episodes older than", "int", "Retention defaults", None, 1, 36500,
          optional=True, unit="days", hint="Empty: no age limit."),
    Field("retention_enabled", "Delete old episodes automatically every night", "bool", "Retention defaults", 0,
          hint="Off by default. Check the preview on the Retention page first; rules are set per podcast or above."),
    Field("retention_time", "Nightly run at (UTC)", "time", "Retention defaults", "03:45"),
    Field("retention_max_per_run", "Delete at most this many episodes per run", "int", "Retention defaults", 25, 1, 100000,
          hint="A safety net: a wrong rule cannot wipe everything in one night. The newest episode of a podcast is never deleted."),
    Field("upload_limit_mb", "Upload size limit per file", "int", "Uploads", 1024, 1, 102400, unit="MB",
          hint="Must match the limit configured in the reverse proxy."),
    Field("lease_seconds", "Task lease duration", "int", "Queue", 300, 30, 3600, unit="seconds"),
    Field("escalation_deadline_min", "Escalation deadline", "int", "Queue", 10, 1, 1440, unit="minutes",
          hint="An unclaimed task becomes visible to the next tier after this time."),
    Field("failure_threshold", "Failed attempts before pausing a task (N)", "int", "Queue", 4, 1, 100,
          hint="Counted per task: bot_check, internal and incomplete fully, network and timeout a quarter. "
               "Also needs failures from two different workers (one if only one worker is active)."),
    Field("task_reopen_hours", "A paused task reopens by itself after", "int", "Queue", 6, 1, 168, unit="hours"),
    Field("worker_online_seconds", "Worker counts as online if seen within", "int", "Workers", 120, 30, 3600,
          unit="seconds"),
    Field("stats_retention_days", "Keep daily hit counters for", "int", "Statistics", 730, 30, 3650, unit="days",
          hint="Older counters are deleted every night."),
    Field("telegram_token", "Telegram bot token", "secret", "Telegram", ""),
    Field("worker_offline_alert_min", "Message when a worker has been offline for", "int", "Telegram", 15, 1, 1440, unit="minutes"),
    Field("telegram_new_episode", "Message for every new episode", "bool", "Telegram", 0),
    Field("telegram_chat_id", "Telegram chat id", "text", "Telegram", "", optional=True,
          hint="Filled in when you link a chat with the button below. Enter one by hand for a group or channel."),
)
BY_KEY = {f.key: f for f in FIELDS}

_TOKEN = re.compile(r"^\d+:[A-Za-z0-9_-]{8,}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CHAT = re.compile(r"^(-?\d+|@[A-Za-z0-9_]{5,})$")


def groups() -> list[tuple[str, list[Field]]]:
    out: dict[str, list[Field]] = {}
    for f in FIELDS:
        out.setdefault(f.group, []).append(f)
    return list(out.items())


def _convert(field: Field, raw: str):
    if field.kind in ("int", "bool"):
        try:
            return int(raw)
        except ValueError:
            return field.default
    return raw


def default_of(field: Field, env_defaults: dict | None) -> object:
    return (env_defaults or {}).get(field.key, field.default)


def get_all(conn: sqlite3.Connection, env_defaults: dict | None = None) -> dict:
    stored = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    return {f.key: _convert(f, stored[f.key]) if f.key in stored else default_of(f, env_defaults)
            for f in FIELDS}


def get(conn: sqlite3.Connection, key: str, env_defaults: dict | None = None):
    """Typed value of one setting (int, str or None)."""
    field = BY_KEY[key]
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return _convert(field, row[0]) if row else default_of(field, env_defaults)


def validate(form, env_defaults: dict | None = None) -> tuple[dict, dict]:
    """Return ``(changes, errors)``.

    ``changes`` maps key to a new value, or to None to reset to the default.
    Secrets that were left empty are absent from ``changes`` (unchanged).
    """
    changes: dict = {}
    errors: dict[str, str] = {}
    for f in FIELDS:
        raw = str(form.get(f.key) or "").strip()
        if f.kind == "secret":
            if form.get(f"clear_{f.key}"):
                changes[f.key] = None
            elif raw:
                if _TOKEN.match(raw):
                    changes[f.key] = raw
                else:
                    errors[f.key] = "does not look like a Telegram bot token (123456:ABC…)"
        elif f.kind == "bool":
            changes[f.key] = 1 if form.get(f.key) else 0
        elif f.kind == "int":
            value, err = parse_int(raw, minimum=f.minimum, maximum=f.maximum, optional=f.optional)
            if err:
                errors[f.key] = err
            else:
                changes[f.key] = value
        elif f.kind == "url":
            value, err = parse_url(raw, optional=f.optional)
            if err:
                errors[f.key] = err
            else:
                changes[f.key] = value.rstrip("/") if value else None
        elif f.kind == "time":
            if not _TIME.match(raw):
                errors[f.key] = "must be a time like 03:45"
            else:
                changes[f.key] = raw
        else:  # text
            if raw and not _CHAT.match(raw):
                errors[f.key] = "must be a numeric id or @channelname"
            else:
                changes[f.key] = raw or None
    lo, hi = changes.get("pause_min_s"), changes.get("pause_max_s")
    if lo is not None and hi is not None and hi < lo and "pause_max_s" not in errors:
        errors["pause_max_s"] = "must not be smaller than the minimum"
    return changes, errors


def save(conn: sqlite3.Connection, changes: dict) -> None:
    """Store changes; joins a transaction the caller already opened, otherwise uses its own."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        for key, value in changes.items():
            if value is None:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
        if own:
            conn.execute("COMMIT")
    except Exception:
        if own:
            conn.execute("ROLLBACK")
        raise
