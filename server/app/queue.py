"""The work queue: every state transition of tasks and attempts lives here.

The API, the scheduler and the UI only call these functions. Each function that
changes state runs in one ``BEGIN IMMEDIATE`` transaction, so two workers can
never hold the same task. Times are UTC; ``now`` is a parameter so tests can move time.

Task states: pending -> claimed -> done | pending (retry) | closed. ``paused`` is set
by the alerting step later; ``closed`` also covers private/removed videos and manual closing.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .scrub import clean
from .workers import TIME_FORMAT

OPEN_STATES = ("pending", "claimed", "paused")
IDLE_HINT = 15            # seconds a worker waits after an empty claim
MIN_HINT, MAX_HINT = 5, 300
RETRY_BACKOFF_START = 60  # seconds; doubles per failed attempt
RETRY_BACKOFF_MAX = 1800
DEFAULT_NOT_YET_AVAILABLE = 1800
SCAN_RETENTION_DAYS = 14
RETRYABLE = {"bot_check", "network", "timeout", "internal", "incomplete"}
PREMIERE_STATES = {"is_upcoming", "is_live", "post_live"}


class QueueError(Exception):
    pass


class TaskNotFound(QueueError):
    pass


class NotOwner(QueueError):
    """The task is not (or no longer) claimed by this worker."""


class BadState(QueueError):
    pass


def ts(moment: datetime) -> str:
    return moment.strftime(TIME_FORMAT)


def parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, TIME_FORMAT).replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# --- creating tasks -------------------------------------------------------------------------------

def _insert_scan(conn: sqlite3.Connection, podcast_id: str, now: datetime) -> int | None:
    try:
        cur = conn.execute("INSERT INTO tasks (type, podcast_id, created_at, updated_at) VALUES ('scan', ?, ?, ?)",
                           (podcast_id, ts(now), ts(now)))
    except sqlite3.IntegrityError:
        return None  # an open scan task exists (unique index), or the podcast is gone
    return cur.lastrowid


def create_scan(conn: sqlite3.Connection, podcast_id: str, now: datetime | None = None) -> int | None:
    """Queue a scan now ("scan now"). Returns the task id, or None if one is already open."""
    now = now or utcnow()
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM podcasts WHERE id = ?", (podcast_id,)).fetchone():
            raise TaskNotFound(podcast_id)
        return _insert_scan(conn, podcast_id, now)


def schedule_scans(conn: sqlite3.Connection, default_interval_min: int, now: datetime | None = None) -> list[int]:
    """Create a scan task for every active podcast whose interval has elapsed and that has none open."""
    now = now or utcnow()
    created = []
    with transaction(conn):
        rows = conn.execute(
            "SELECT p.id, p.scan_interval_min, "
            "(SELECT MAX(COALESCE(t.finished_at, t.created_at)) FROM tasks t WHERE t.podcast_id = p.id AND t.type = 'scan') AS last "
            "FROM podcasts p WHERE p.active = 1 AND NOT EXISTS "
            "(SELECT 1 FROM tasks t WHERE t.podcast_id = p.id AND t.type = 'scan' AND t.state IN ('pending', 'claimed', 'paused'))"
        ).fetchall()
        for r in rows:
            interval = timedelta(minutes=r["scan_interval_min"] or default_interval_min)
            if r["last"] is None or parse(r["last"]) + interval <= now:
                task_id = _insert_scan(conn, r["id"], now)
                if task_id:
                    created.append(task_id)
    return created


# --- lease expiry ----------------------------------------------------------------------------------

def _expire(conn: sqlite3.Connection, now: datetime) -> int:
    stamp = ts(now)
    rows = conn.execute("SELECT id FROM tasks WHERE state = 'claimed' AND lease_until < ?", (stamp,)).fetchall()
    for r in rows:
        conn.execute("UPDATE tasks SET state = 'pending', claimed_by = NULL, lease_until = NULL, "
                     "failures = failures + 1, updated_at = ? WHERE id = ?", (stamp, r["id"]))
        conn.execute("UPDATE attempts SET ended_at = ?, result = 'lease_expired', error_class = 'timeout', "
                     "error_text = 'The lease expired: no heartbeat from the worker' "
                     "WHERE task_id = ? AND result IS NULL", (stamp, r["id"]))
    return len(rows)


def expire_leases(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Return tasks with an expired lease to pending and record a ``lease_expired`` attempt."""
    with transaction(conn):
        return _expire(conn, now or utcnow())


# --- claiming ----------------------------------------------------------------------------------------

@dataclass
class Claim:
    task: dict | None
    retry_after: int
    attempt: int = 0
    lease_until: str = ""


def _download_usage(conn: sqlite3.Connection, worker_id: int, now: datetime) -> tuple[list[tuple[datetime, str]], datetime | None]:
    """Download attempts of this worker in the last hour as (started_at, podcast_id), and its last download activity."""
    counted = "a.worker_id = ? AND t.type = 'download' AND (a.result IS NULL OR a.result NOT IN ('deferred', 'released'))"
    recent = [(parse(r[0]), r[1]) for r in conn.execute(
        f"SELECT a.started_at, t.podcast_id FROM attempts a JOIN tasks t ON t.id = a.task_id WHERE {counted} "
        "AND a.started_at > ? ORDER BY a.started_at", (worker_id, ts(now - timedelta(hours=1))))]
    row = conn.execute(f"SELECT MAX(COALESCE(a.ended_at, a.started_at)) FROM attempts a JOIN tasks t ON t.id = a.task_id "
                       f"WHERE {counted}", (worker_id,)).fetchone()
    return recent, parse(row[0]) if row and row[0] else None


def _pacing(worker: dict, podcast_limits: dict, defaults: dict) -> dict:
    """Effective limits for a worker on a podcast.

    ``downloads_per_hour`` is the worker's total across all podcasts; ``podcast_downloads_per_hour`` is
    what it may take of this one podcast (the podcast's value, capped by the worker's). The pause between
    downloads is the larger of the two.
    """
    def pick(key: str):
        w = worker.get(key) if worker.get(key) is not None else defaults[key]
        p = podcast_limits.get(key) if podcast_limits.get(key) is not None else defaults[key]
        return w, p

    wmin, pmin = pick("pause_min_s")
    wmax, pmax = pick("pause_max_s")
    wdph, pdph = pick("downloads_per_hour")
    return {"pause_min_s": max(wmin, pmin), "pause_max_s": max(wmax, pmax), "downloads_per_hour": wdph,
            "podcast_downloads_per_hour": min(wdph, pdph)}


def _hour_delay(started: list[datetime], limit: int, now: datetime) -> float:
    """Seconds until fewer than ``limit`` of the attempts are younger than one hour."""
    if len(started) < limit:
        return 0.0
    return (started[len(started) - limit] + timedelta(hours=1) - now).total_seconds()


def _download_delay(limits: dict, usage, now: datetime, podcast_id: str) -> int:
    """Seconds until this worker may take another download of this podcast (0 = now)."""
    recent, last_activity = usage
    delay = max(
        _hour_delay([t for t, _ in recent], limits["downloads_per_hour"], now),
        _hour_delay([t for t, p in recent if p == podcast_id], limits["podcast_downloads_per_hour"], now),
        (last_activity + timedelta(seconds=limits["pause_min_s"]) - now).total_seconds() if last_activity else 0.0,
    )
    return int(delay) + 1 if delay > 0 else 0


def claim(conn: sqlite3.Connection, worker: dict, types: list[str], defaults: dict, lease_seconds: int,
          now: datetime | None = None) -> Claim:
    """Atomically hand the oldest eligible task to the worker, or say when to ask again.

    Rules: one task at a time per worker; only pending tasks that are due (not_before) of active
    podcasts and of the requested types; scans before downloads, oldest first; for downloads the
    pacing limits (downloads per hour, pause between downloads) apply, the worker's value capping
    the podcast's. Escalation across tiers comes later: all tiers see all tasks for now.
    """
    now = now or utcnow()
    with transaction(conn):
        _expire(conn, now)
        if conn.execute("SELECT 1 FROM tasks WHERE claimed_by = ? AND state = 'claimed'", (worker["id"],)).fetchone():
            return Claim(None, IDLE_HINT)
        marks = ",".join("?" * len(types))
        candidates = conn.execute(
            "SELECT t.*, p.pause_min_s AS p_pause_min, p.pause_max_s AS p_pause_max, p.downloads_per_hour AS p_dph "
            f"FROM tasks t JOIN podcasts p ON p.id = t.podcast_id WHERE t.state = 'pending' AND t.type IN ({marks}) "
            "AND (t.not_before IS NULL OR t.not_before <= ?) AND p.active = 1 "
            "ORDER BY (t.type != 'scan'), t.id LIMIT 100", [*types, ts(now)]).fetchall()
        wait: int | None = None
        usage = None
        for c in candidates:
            if c["type"] == "download":
                usage = usage or _download_usage(conn, worker["id"], now)
                limits = _pacing(worker, {"pause_min_s": c["p_pause_min"], "pause_max_s": c["p_pause_max"],
                                          "downloads_per_hour": c["p_dph"]}, defaults)
                delay = _download_delay(limits, usage, now, c["podcast_id"])
                if delay:
                    wait = delay if wait is None else min(wait, delay)
                    continue
            lease_until = ts(now + timedelta(seconds=lease_seconds))
            cur = conn.execute("UPDATE tasks SET state = 'claimed', claimed_by = ?, lease_until = ?, updated_at = ? "
                               "WHERE id = ? AND state = 'pending'", (worker["id"], lease_until, ts(now), c["id"]))
            if cur.rowcount != 1:
                continue
            conn.execute("INSERT INTO attempts (task_id, worker_id, worker_name, started_at) VALUES (?, ?, ?, ?)",
                         (c["id"], worker["id"], worker["name"], ts(now)))
            attempt = conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id = ?", (c["id"],)).fetchone()[0]
            task = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (c["id"],)).fetchone())
            return Claim(task, 0, attempt, lease_until)
        return Claim(None, max(MIN_HINT, min(MAX_HINT, wait)) if wait else IDLE_HINT)


def _owned(conn: sqlite3.Connection, task_id: int, worker_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskNotFound(task_id)
    if row["state"] != "claimed" or row["claimed_by"] != worker_id:
        raise NotOwner(task_id)
    return row


def heartbeat(conn: sqlite3.Connection, task_id: int, worker_id: int, lease_seconds: int,
              now: datetime | None = None) -> str:
    """Extend the lease. Raises NotOwner if the task was taken away (lease expired and handed on)."""
    now = now or utcnow()
    with transaction(conn):
        _owned(conn, task_id, worker_id)
        lease_until = ts(now + timedelta(seconds=lease_seconds))
        conn.execute("UPDATE tasks SET lease_until = ?, updated_at = ? WHERE id = ?", (lease_until, ts(now), task_id))
        return lease_until


# --- results ------------------------------------------------------------------------------------------

def complete_scan(conn: sqlite3.Connection, task_id: int, worker_id: int, videos: list[dict],
                  now: datetime | None = None) -> dict:
    """Store a scan result: create download tasks for videos that are new, mark the scan done.

    Only the first ``scan_depth`` videos count. A video is known if it has an episode row (any
    state) or a download task (any state). Premieres and live streams get ``not_before``.
    """
    now = now or utcnow()
    with transaction(conn):
        task = _owned(conn, task_id, worker_id)
        if task["type"] != "scan":
            raise BadState("not a scan task")
        depth = conn.execute("SELECT scan_depth FROM podcasts WHERE id = ?", (task["podcast_id"],)).fetchone()
        depth = depth["scan_depth"] if depth else 0
        created = known = 0
        seen: set[str] = set()
        for video in videos[:depth]:
            vid = video["id"]
            if vid in seen:
                continue
            seen.add(vid)
            if (conn.execute("SELECT 1 FROM episodes WHERE podcast_id = ? AND video_id = ?", (task["podcast_id"], vid)).fetchone()
                    or conn.execute("SELECT 1 FROM tasks WHERE podcast_id = ? AND video_id = ? AND type = 'download'",
                                    (task["podcast_id"], vid)).fetchone()):
                known += 1
                continue
            not_before = None
            if video.get("live_status") in PREMIERE_STATES:
                release = video.get("release_timestamp")
                start = datetime.fromtimestamp(release, timezone.utc) if release else now
                not_before = ts(max(start, now) + timedelta(minutes=30))  # a margin after the announced start
            conn.execute("INSERT INTO tasks (type, podcast_id, video_id, title, not_before, created_at, updated_at) "
                         "VALUES ('download', ?, ?, ?, ?, ?, ?)",
                         (task["podcast_id"], vid, clean(video.get("title", ""), 500), not_before, ts(now), ts(now)))
            created += 1
        skipped = max(0, len(videos) - depth)
        note = f"{len(videos)} videos reported, {created} new, {known} known, {skipped} beyond the scan depth"
        conn.execute("UPDATE tasks SET state = 'done', claimed_by = NULL, lease_until = NULL, finished_at = ?, "
                     "updated_at = ? WHERE id = ?", (ts(now), ts(now), task_id))
        conn.execute("UPDATE attempts SET ended_at = ?, result = 'ok', note = ? WHERE task_id = ? AND result IS NULL",
                     (ts(now), note, task_id))
        return {"created": created, "known": known, "skipped": skipped}


def backoff_seconds(failures: int) -> int:
    return min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_START * 2 ** max(0, failures - 1))


def fail(conn: sqlite3.Connection, task_id: int, worker_id: int, error_class: str, message: str,
         retry_after: int | None = None, now: datetime | None = None,
         extra_secrets: tuple[str, ...] = ()) -> tuple[str, str | None]:
    """Record a failed attempt. Returns ``(new state, not_before)``.

    - ``not_yet_available`` (premiere/live, downloads only): not an error, the task waits until
      ``not_before`` and the failure counter does not move;
    - ``private`` / ``removed``: the task is closed, no retry;
    - everything else is retried after a growing backoff (the same worker is not offered the
      task before it is due again).
    """
    now = now or utcnow()
    text = clean(message, 500, extra_secrets)
    with transaction(conn):
        task = _owned(conn, task_id, worker_id)
        if error_class == "not_yet_available" and task["type"] != "download":
            error_class = "internal"  # a scan cannot be "not yet available"
        if error_class == "not_yet_available":
            wait = min(max(retry_after or DEFAULT_NOT_YET_AVAILABLE, 60), 7 * 86400)
            not_before = ts(now + timedelta(seconds=wait))
            state, result, failures = "pending", "deferred", task["failures"]
        elif error_class in ("private", "removed"):
            state, result, not_before, failures = "closed", "failed", None, task["failures"] + 1
        else:
            failures = task["failures"] + 1
            wait = max(backoff_seconds(failures), min(retry_after or 0, 3600))
            not_before = ts(now + timedelta(seconds=wait))
            state, result = "pending", "failed"
        finished = ts(now) if state == "closed" else None
        conn.execute("UPDATE tasks SET state = ?, claimed_by = NULL, lease_until = NULL, not_before = ?, failures = ?, "
                     "updated_at = ?, finished_at = ? WHERE id = ?", (state, not_before, failures, ts(now), finished, task_id))
        conn.execute("UPDATE attempts SET ended_at = ?, result = ?, error_class = ?, error_text = ? "
                     "WHERE task_id = ? AND result IS NULL", (ts(now), result, error_class, text, task_id))
        return state, not_before


# --- manual actions (UI) --------------------------------------------------------------------------------

def _close_open_attempt(conn: sqlite3.Connection, task_id: int, now: datetime, note: str) -> None:
    conn.execute("UPDATE attempts SET ended_at = ?, result = 'released', note = ? WHERE task_id = ? AND result IS NULL",
                 (ts(now), note, task_id))


def _get(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskNotFound(task_id)
    return row


def release(conn: sqlite3.Connection, task_id: int, now: datetime | None = None) -> None:
    """Take a claimed task away from its worker and make it pending again."""
    now = now or utcnow()
    with transaction(conn):
        if _get(conn, task_id)["state"] != "claimed":
            raise BadState("only a claimed task can be released")
        conn.execute("UPDATE tasks SET state = 'pending', claimed_by = NULL, lease_until = NULL, updated_at = ? WHERE id = ?",
                     (ts(now), task_id))
        _close_open_attempt(conn, task_id, now, "released manually")


def retry(conn: sqlite3.Connection, task_id: int, now: datetime | None = None) -> None:
    """Make a pending (waiting), paused or closed task pending and due again."""
    now = now or utcnow()
    with transaction(conn):
        task = _get(conn, task_id)
        if task["state"] not in ("pending", "paused", "closed"):
            raise BadState("only a waiting, paused or closed task can be retried")
        try:
            conn.execute("UPDATE tasks SET state = 'pending', not_before = NULL, pause_reason = NULL, finished_at = NULL, "
                         "updated_at = ? WHERE id = ?", (ts(now), task_id))
        except sqlite3.IntegrityError:
            raise BadState("another scan of this podcast is already open") from None


def close(conn: sqlite3.Connection, task_id: int, now: datetime | None = None) -> None:
    """Close an open task by hand."""
    now = now or utcnow()
    with transaction(conn):
        if _get(conn, task_id)["state"] not in OPEN_STATES:
            raise BadState("only an open task can be closed")
        conn.execute("UPDATE tasks SET state = 'closed', claimed_by = NULL, lease_until = NULL, finished_at = ?, "
                     "updated_at = ? WHERE id = ?", (ts(now), ts(now), task_id))
        _close_open_attempt(conn, task_id, now, "closed manually")


# --- reading ----------------------------------------------------------------------------------------------

_LIST_SQL = ("FROM tasks t JOIN podcasts p ON p.id = t.podcast_id LEFT JOIN workers w ON w.id = t.claimed_by "
             "WHERE (? IS NULL OR t.state = ?) AND (? IS NULL OR t.podcast_id = ?) AND (? IS NULL OR t.type = ?)")


def list_tasks(conn: sqlite3.Connection, state: str | None = None, podcast_id: str | None = None,
               type_: str | None = None, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.*, p.name AS podcast_name, w.name AS worker_name, "
        "(SELECT COUNT(*) FROM attempts a WHERE a.task_id = t.id) AS attempt_count " + _LIST_SQL +
        " ORDER BY t.id DESC LIMIT ? OFFSET ?", (state, state, podcast_id, podcast_id, type_, type_, limit, offset)).fetchall()


def count_tasks(conn: sqlite3.Connection, state: str | None = None, podcast_id: str | None = None,
                type_: str | None = None) -> int:
    return conn.execute("SELECT COUNT(*) " + _LIST_SQL, (state, state, podcast_id, podcast_id, type_, type_)).fetchone()[0]


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT t.*, p.name AS podcast_name, w.name AS worker_name FROM tasks t JOIN podcasts p ON p.id = t.podcast_id "
        "LEFT JOIN workers w ON w.id = t.claimed_by WHERE t.id = ?", (task_id,)).fetchone()


def attempts_of(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM attempts WHERE task_id = ? ORDER BY id DESC", (task_id,)).fetchall()


def counts(conn: sqlite3.Connection) -> dict:
    """Task counts for the dashboard: per state, and the open downloads."""
    by_state = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")}
    open_downloads = conn.execute("SELECT COUNT(*) FROM tasks WHERE type = 'download' AND state IN ('pending', 'claimed', 'paused')").fetchone()[0]
    return {"pending": by_state.get("pending", 0), "claimed": by_state.get("claimed", 0),
            "paused": by_state.get("paused", 0), "done": by_state.get("done", 0), "closed": by_state.get("closed", 0),
            "open_downloads": open_downloads}


def recent_errors(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.*, t.type, t.podcast_id, t.title FROM attempts a JOIN tasks t ON t.id = a.task_id "
        "WHERE a.result IN ('failed', 'lease_expired') ORDER BY a.id DESC LIMIT ?", (limit,)).fetchall()


def prune(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Delete finished scan tasks (and their attempts) older than two weeks. Download tasks are kept as history."""
    cutoff = ts((now or utcnow()) - timedelta(days=SCAN_RETENTION_DAYS))
    return conn.execute("DELETE FROM tasks WHERE type = 'scan' AND state IN ('done', 'closed') AND finished_at < ?",
                        (cutoff,)).rowcount
