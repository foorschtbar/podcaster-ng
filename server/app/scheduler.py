"""Background scheduler: leases, reopening, escalation, scans, the worker watchdog and the Telegram outbox."""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from . import notifications, queue, settings, watchdog
from .db import connect

log = logging.getLogger(__name__)
FIRST_TICK_DELAY = 5.0
STARTED = datetime.now(timezone.utc)


def tick(db_path: Path, env_defaults: dict | None = None, transport=None, now: datetime | None = None,
         started_at: datetime | None = None) -> dict:
    """One scheduler pass (also callable from tests)."""
    now = now or queue.utcnow()
    conn = connect(db_path)
    try:
        s = settings.get_all(conn, env_defaults)
        policy = queue.Policy.from_settings(s)
        expired = queue.expire_leases(conn, now, policy)
        reopened = queue.reopen_due(conn, now)
        escalated = queue.escalate_overdue(conn, now, policy)
        created = queue.schedule_scans(conn, s["scan_interval_min"], now)
        alerts = watchdog.check(conn, now, s["worker_offline_alert_min"], s["worker_online_seconds"], started_at or STARTED)
        sent = notifications.deliver_pending(conn, now, transport)
    finally:
        conn.close()
    if expired:
        log.warning("%d task lease(s) expired; the tasks are pending again", expired)
    if created:
        log.info("Created %d scan task(s)", len(created))
    if reopened:
        log.info("Reopened %d paused task(s)", reopened)
    return {"expired": expired, "scans_created": len(created), "reopened": reopened, "escalated": escalated,
            "alerts": alerts, "messages": sent}


async def scheduler_loop(db_path: Path, interval: float, env_defaults: dict | None = None, transport=None) -> None:
    await asyncio.sleep(min(FIRST_TICK_DELAY, interval))
    while True:
        try:
            await run_in_threadpool(tick, db_path, env_defaults, transport)
        except Exception:  # noqa: BLE001 - the scheduler must never die
            log.exception("Scheduler tick failed")
        await asyncio.sleep(interval)
