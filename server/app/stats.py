"""Hit statistics: recording, pruning and the queries behind the dashboard (no web code).

Days are UTC dates. Nothing here ever sees an IP address or a raw user agent.
"""

import asyncio
import logging
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from . import queue, settings
from .app_groups import OTHER
from .db import connect

log = logging.getLogger(__name__)

PRUNE_HOUR, PRUNE_MINUTE = 3, 30  # UTC


def today() -> date:
    return datetime.now(timezone.utc).date()


# --- recording ---------------------------------------------------------------

def record_hit(conn: sqlite3.Connection, *, day: date, podcast_id: str, episode_id: str | None, kind: str,
               app_group: str, count: int, nbytes: int) -> None:
    conn.execute(
        "INSERT INTO hits_daily (day, podcast_id, episode_id, kind, app_group, count, bytes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (day, podcast_id, COALESCE(episode_id, ''), kind, app_group) "
        "DO UPDATE SET count = count + excluded.count, bytes = bytes + excluded.bytes",
        (day.isoformat(), podcast_id, episode_id, kind, app_group, count, nbytes))


def episode_for_file(conn: sqlite3.Connection, podcast_id: str, audio_file: str) -> str | None:
    row = conn.execute("SELECT video_id FROM episodes WHERE podcast_id = ? AND audio_file = ?",
                       (podcast_id, audio_file)).fetchone()
    return row[0] if row else None


# --- pruning -----------------------------------------------------------------

def prune(conn: sqlite3.Connection, retention_days: int, today_: date | None = None) -> int:
    """Delete counters older than ``retention_days``. Returns the number of rows removed."""
    cutoff = ((today_ or today()) - timedelta(days=retention_days)).isoformat()
    return conn.execute("DELETE FROM hits_daily WHERE day < ?", (cutoff,)).rowcount


def prune_once(db_path: Path) -> int:
    conn = connect(db_path)
    try:
        removed = prune(conn, settings.get(conn, "stats_retention_days"))
        old_scans = queue.prune(conn)
    finally:
        conn.close()
    if removed or old_scans:
        log.info("Pruned %d old daily counter rows and %d old scan tasks", removed, old_scans)
    return removed


def seconds_until(hour: int, minute: int, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def prune_loop(db_path: Path, first_delay: float = 60.0) -> None:
    """Prune shortly after start and then every night at 03:30 UTC."""
    await asyncio.sleep(first_delay)
    while True:
        try:
            await run_in_threadpool(prune_once, db_path)
        except Exception:  # noqa: BLE001 - must never kill the loop
            log.exception("Pruning the hit counters failed")
        await asyncio.sleep(seconds_until(PRUNE_HOUR, PRUNE_MINUTE))


# --- dashboard queries -------------------------------------------------------

def daily_series(conn: sqlite3.Connection, days: int = 30, today_: date | None = None) -> list[dict]:
    """One entry per day (oldest first, gaps filled with zeros): feed requests, downloads, bytes."""
    end = today_ or today()
    start = end - timedelta(days=days - 1)
    rows = {d: {"day": d, "feed": 0, "file": 0, "bytes": 0}
            for d in (start + timedelta(days=i) for i in range(days))}
    for r in conn.execute("SELECT day, kind, SUM(count) AS n, SUM(bytes) AS b FROM hits_daily "
                          "WHERE day >= ? AND day <= ? GROUP BY day, kind", (start.isoformat(), end.isoformat())):
        entry = rows.get(date.fromisoformat(r["day"]))
        if entry:
            entry[r["kind"]] = r["n"]
            entry["bytes"] += r["b"]
    return list(rows.values())


def top_episodes(conn: sqlite3.Connection, days: int = 30, limit: int = 10, today_: date | None = None) -> list[dict]:
    since = ((today_ or today()) - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT h.podcast_id, h.episode_id, SUM(h.count) AS n, e.title, p.name AS podcast_name "
        "FROM hits_daily h LEFT JOIN episodes e ON e.podcast_id = h.podcast_id AND e.video_id = h.episode_id "
        "LEFT JOIN podcasts p ON p.id = h.podcast_id "
        "WHERE h.kind = 'file' AND h.episode_id IS NOT NULL AND h.day >= ? "
        "GROUP BY h.podcast_id, h.episode_id HAVING SUM(h.count) > 0 "
        "ORDER BY n DESC, e.title LIMIT ?", (since, limit)).fetchall()
    return [{"podcast_id": r["podcast_id"], "podcast_name": r["podcast_name"] or r["podcast_id"],
             "title": r["title"] or r["episode_id"], "count": r["n"]} for r in rows]


def fold_tail(rows: list[tuple[str, int]], keep: int = 7) -> list[tuple[str, int]]:
    """Largest first; everything past ``keep`` rows is merged into one 'Other' row."""
    rows = sorted(rows, key=lambda r: (-r[1], r[0]))
    if len(rows) <= keep + 1:
        return rows
    head = [r for r in rows[:keep] if r[0] != OTHER]
    tail_sum = sum(n for g, n in rows[keep:]) + sum(n for g, n in rows[:keep] if g == OTHER)
    return sorted([*head, (OTHER, tail_sum)], key=lambda r: (r[0] == OTHER, -r[1], r[0]))


def app_shares(conn: sqlite3.Connection, kind: str, days: int = 30, today_: date | None = None) -> list[tuple[str, int]]:
    since = ((today_ or today()) - timedelta(days=days - 1)).isoformat()
    rows = conn.execute("SELECT app_group, SUM(count) AS n FROM hits_daily WHERE kind = ? AND day >= ? "
                        "GROUP BY app_group HAVING SUM(count) > 0", (kind, since)).fetchall()
    return fold_tail([(r["app_group"], r["n"]) for r in rows])


def storage_overview(conn: sqlite3.Connection, data_dir: Path) -> dict:
    """Free disk space of the data volume plus episode count and size per podcast."""
    try:
        usage = shutil.disk_usage(data_dir)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        disk = None
    per = conn.execute(
        "SELECT p.id, p.name, COUNT(e.video_id) AS episodes, COALESCE(SUM(e.size_bytes), 0) AS size "
        "FROM podcasts p LEFT JOIN episodes e ON e.podcast_id = p.id AND e.state = 'available' "
        "GROUP BY p.id ORDER BY size DESC, lower(p.name)").fetchall()
    podcasts = [dict(r) for r in per]
    return {"disk": disk, "podcasts": podcasts, "total_size": sum(p["size"] for p in podcasts),
            "total_episodes": sum(p["episodes"] for p in podcasts)}
