"""Worker watchdog: one message when an active worker has been silent too long, one when it is back."""

import sqlite3
from datetime import datetime, timedelta

from . import notifications
from .queue import ts, transaction
from .workers import TIME_FORMAT

GRACE = timedelta(minutes=3)  # after a server start the workers need a moment to reconnect


def _parse(stamp: str) -> datetime:
    from datetime import timezone
    return datetime.strptime(stamp, TIME_FORMAT).replace(tzinfo=timezone.utc)


def check(conn: sqlite3.Connection, now: datetime, offline_minutes: int, online_seconds: int,
          started_at: datetime) -> dict:
    """Compare ``last_seen`` of the active workers with the thresholds.

    ``offline_alerted_at`` is the memory that makes the messages happen once: it is set together with the
    "offline" message and cleared together with the "back online" message, in one transaction.
    Workers that never connected, and deactivated ones, are not reported.
    """
    result = {"offline": 0, "online": 0}
    if now - started_at < GRACE:
        return result
    with transaction(conn):
        for w in conn.execute("SELECT * FROM workers WHERE active = 1 AND last_seen IS NOT NULL").fetchall():
            age = (now - _parse(w["last_seen"])).total_seconds()
            if w["offline_alerted_at"] is None:
                if age > offline_minutes * 60:
                    conn.execute("UPDATE workers SET offline_alerted_at = ? WHERE id = ?", (ts(now), w["id"]))
                    notifications.enqueue(
                        conn, "worker_offline", f"worker_offline:{w['id']}:{ts(now)}",
                        f"⚠ Worker '{w['name']}' (tier {w['tier']}) has been offline for {int(age // 60)} min "
                        f"(last seen {w['last_seen']} UTC).", now)
                    result["offline"] += 1
            elif age <= online_seconds:
                conn.execute("UPDATE workers SET offline_alerted_at = NULL WHERE id = ?", (w["id"],))
                notifications.enqueue(
                    conn, "worker_online", f"worker_online:{w['id']}:{w['offline_alerted_at']}",
                    f"✅ Worker '{w['name']}' (tier {w['tier']}) is back online "
                    f"(it was reported offline at {w['offline_alerted_at']} UTC).", now)
                result["online"] += 1
    return result
