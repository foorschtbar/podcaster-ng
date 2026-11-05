"""Background scheduler: expires leases and creates due scan tasks."""

import asyncio
import logging
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from . import queue, settings
from .db import connect

log = logging.getLogger(__name__)
FIRST_TICK_DELAY = 5.0


def tick(db_path: Path) -> dict:
    """One scheduler pass (also callable from tests)."""
    conn = connect(db_path)
    try:
        expired = queue.expire_leases(conn)
        created = queue.schedule_scans(conn, settings.get(conn, "scan_interval_min"))
    finally:
        conn.close()
    if expired:
        log.warning("%d task lease(s) expired; the tasks are pending again", expired)
    if created:
        log.info("Created %d scan task(s)", len(created))
    return {"expired": expired, "scans_created": len(created)}


async def scheduler_loop(db_path: Path, interval: float) -> None:
    await asyncio.sleep(min(FIRST_TICK_DELAY, interval))
    while True:
        try:
            await run_in_threadpool(tick, db_path)
        except Exception:  # noqa: BLE001 - the scheduler must never die
            log.exception("Scheduler tick failed")
        await asyncio.sleep(interval)
