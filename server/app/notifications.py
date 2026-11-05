"""Outbox for Telegram messages.

Events (a task was paused, a worker went offline, ...) are written to the ``notifications`` table with a
unique key, in the same transaction as the event itself, so one condition can never produce two messages.
``deliver_pending`` sends them, retrying with backoff while Telegram is unreachable.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import settings, telegram
from .db import connect
from .workers import TIME_FORMAT

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 8
BATCH = 20


def _ts(moment: datetime) -> str:
    return moment.strftime(TIME_FORMAT)


def enqueue(conn: sqlite3.Connection, kind: str, key: str, text: str, now: datetime | None = None) -> bool:
    """Queue a message. Returns False if this key was queued before (nothing is added)."""
    now = now or datetime.now(timezone.utc)
    cur = conn.execute("INSERT OR IGNORE INTO notifications (key, kind, text, created_at) VALUES (?, ?, ?, ?)",
                       (key, kind, text, _ts(now)))
    return cur.rowcount == 1


def credentials(conn: sqlite3.Connection) -> tuple[str, str]:
    return settings.get(conn, "telegram_token") or "", settings.get(conn, "telegram_chat_id") or ""


def deliver_pending(conn: sqlite3.Connection, now: datetime | None = None,
                    transport=None, base: str = telegram.API) -> dict:
    """Send what is due. Not configured: the messages are marked ``skipped`` (they are not kept for later)."""
    now = now or datetime.now(timezone.utc)
    token, chat = credentials(conn)
    rows = conn.execute("SELECT * FROM notifications WHERE status = 'queued' AND (next_try_at IS NULL OR next_try_at <= ?) "
                        "ORDER BY id LIMIT ?", (_ts(now), BATCH)).fetchall()
    result = {"sent": 0, "failed": 0, "skipped": 0, "retry": 0}
    for row in rows:
        if not token or not chat:
            conn.execute("UPDATE notifications SET status = 'skipped', last_error = 'Telegram is not configured' WHERE id = ?", (row["id"],))
            result["skipped"] += 1
            continue
        try:
            telegram.send_message(token, chat, row["text"], transport, base)
        except telegram.TelegramError as exc:
            attempts = row["attempts"] + 1
            give_up = attempts >= MAX_ATTEMPTS
            wait = exc.retry_after or min(1800, 30 * 2 ** attempts)
            conn.execute("UPDATE notifications SET attempts = ?, last_error = ?, status = ?, next_try_at = ? WHERE id = ?",
                         (attempts, str(exc)[:300], "failed" if give_up else "queued", _ts(now + timedelta(seconds=wait)), row["id"]))
            result["failed" if give_up else "retry"] += 1
            log.warning("Telegram message %s not sent (attempt %d): %s", row["kind"], attempts, exc)
        else:
            conn.execute("UPDATE notifications SET status = 'sent', attempts = attempts + 1, sent_at = ?, last_error = '' WHERE id = ?",
                         (_ts(now), row["id"]))
            result["sent"] += 1
    return result


def tick(db_path, now: datetime | None = None, transport=None, base: str = telegram.API) -> dict:
    conn = connect(db_path)
    try:
        return deliver_pending(conn, now, transport, base)
    finally:
        conn.close()


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
