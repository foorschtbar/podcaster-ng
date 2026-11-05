"""The work queue: every state transition of tasks and attempts lives here.

The API, the scheduler and the UI only call these functions. Each function that
changes state runs in one ``BEGIN IMMEDIATE`` transaction, so two workers can
never hold the same task. Times are UTC; ``now`` is a parameter so tests can move time.

Task states: pending -> claimed -> done | pending (retry) | closed. ``paused`` is set
by the alerting step later; ``closed`` also covers private/removed videos and manual closing.
"""

import sqlite3
from contextlib import contextmanager
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import notifications
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
COOLDOWN_SECONDS = 600
BLOCK_WINDOW_SECONDS = 1800      # bot checks on this many different videos within the window block a worker's downloads
BLOCK_VIDEOS = 2
SUGGEST_HOURS = 24               # a bot check this recent decides where a retry starts   # a worker does not get a task back that it has just failed
# Classes whose failure makes the task visible to the next tier at once. network/timeout are retried with
# backoff on the same tier; not_yet_available, private and removed never escalate.
ESCALATING = {"bot_check", "internal", "incomplete"}
# How much a failed attempt counts towards pausing a task (network problems count weakly).
FAIL_WEIGHTS = {"bot_check": 1.0, "internal": 1.0, "incomplete": 1.0, "network": 0.25, "timeout": 0.25}


@dataclass(frozen=True)
class Policy:
    """Settings the queue rules depend on."""
    failure_threshold: int = 4
    reopen_hours: int = 6
    escalation_deadline_s: int = 600
    notify_new_episode: bool = False
    base_url: str = ""
    block_minutes: int = 60

    @classmethod
    def from_settings(cls, s: dict) -> "Policy":
        return cls(int(s["failure_threshold"]), int(s["task_reopen_hours"]), int(s["escalation_deadline_min"]) * 60,
                   bool(s.get("telegram_new_episode")), s.get("base_url") or "", int(s.get("download_block_minutes", 60)))


DEFAULT_POLICY = Policy()


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


# --- escalation, pausing and reopening ------------------------------------------------------------------

def _event(conn: sqlite3.Connection, task_id: int, now: datetime, kind: str, detail: str = "") -> None:
    conn.execute("INSERT INTO task_events (task_id, at, kind, detail) VALUES (?, ?, ?, ?)", (task_id, ts(now), kind, detail))


def _next_tier(conn: sqlite3.Connection, tier: int) -> int | None:
    """The next worker tier above ``tier`` that has an active worker (tiers without workers are skipped)."""
    return conn.execute("SELECT MIN(tier) FROM workers WHERE active = 1 AND tier > ?", (tier,)).fetchone()[0]


def _escalate(conn: sqlite3.Connection, task_id: int, tier: int, now: datetime, why: str) -> int | None:
    """Make the task visible to the next tier. Returns the new tier, or None if there is no higher tier."""
    new = _next_tier(conn, tier)
    if new is None:
        return None
    conn.execute("UPDATE tasks SET tier = ?, tier_since = ?, updated_at = ? WHERE id = ?", (new, ts(now), ts(now), task_id))
    _event(conn, task_id, now, "escalated", f"now visible to tier {new}: {why}")
    return new


def _escalate_overdue(conn: sqlite3.Connection, now: datetime, policy: Policy) -> int:
    """Pending tasks that no worker of their tier took within the deadline move up one tier."""
    cutoff = ts(now - timedelta(seconds=policy.escalation_deadline_s))
    rows = conn.execute("SELECT id, tier FROM tasks WHERE state = 'pending' AND COALESCE(tier_since, created_at) < ? "
                        "AND (not_before IS NULL OR not_before < ?)", (cutoff, cutoff)).fetchall()
    moved = 0
    for r in rows:
        if _escalate(conn, r["id"], r["tier"], now, f"no tier {r['tier']} worker took it within "
                                                     f"{policy.escalation_deadline_s // 60} min") is not None:
            moved += 1
    return moved


def _escalate_blocked(conn: sqlite3.Connection, now: datetime) -> int:
    """Pending downloads whose whole tier is download-blocked move up at once, if a higher tier has a worker."""
    rows = conn.execute(
        "SELECT t.id, t.tier FROM tasks t WHERE t.state = 'pending' AND t.type = 'download' "
        "AND NOT EXISTS (SELECT 1 FROM workers w WHERE w.active = 1 AND w.tier <= t.tier AND w.tier >= t.min_tier "
        "AND (w.download_blocked_until IS NULL OR w.download_blocked_until <= ?))", (ts(now),)).fetchall()
    moved = 0
    for r in rows:
        new = _escalate(conn, r["id"], r["tier"], now, f"every tier {r['tier']} worker has paused its downloads after bot checks")
        if new is not None:
            conn.execute("UPDATE tasks SET not_before = NULL WHERE id = ?", (r["id"],))
            moved += 1
    return moved


def escalate_overdue(conn: sqlite3.Connection, now: datetime | None = None, policy: Policy = DEFAULT_POLICY) -> int:
    now = now or utcnow()
    with transaction(conn):
        return _escalate_overdue(conn, now, policy) + _escalate_blocked(conn, now)


def _maybe_block(conn: sqlite3.Connection, worker_id: int, now: datetime, policy: Policy) -> None:
    """Bot checks on several different videos in a short time mean the worker's address is blocked, not one video:
    stop its downloads for a while (it keeps scanning) instead of hammering YouTube from a blocked address."""
    if policy.block_minutes <= 0:
        return
    since = ts(now - timedelta(seconds=BLOCK_WINDOW_SECONDS))
    videos = conn.execute("SELECT COUNT(DISTINCT t.video_id) FROM attempts a JOIN tasks t ON t.id = a.task_id "
                          "WHERE a.worker_id = ? AND a.result = 'failed' AND a.error_class = 'bot_check' AND t.type = 'download' "
                          "AND a.ended_at >= ?", (worker_id, since)).fetchone()[0]
    row = conn.execute("SELECT name, download_blocked_until FROM workers WHERE id = ?", (worker_id,)).fetchone()
    if videos < BLOCK_VIDEOS or row is None or (row["download_blocked_until"] or "") > ts(now):
        return
    until = ts(now + timedelta(minutes=policy.block_minutes))
    conn.execute("UPDATE workers SET download_blocked_until = ? WHERE id = ?", (until, worker_id))
    notifications.enqueue(conn, "worker_blocked", f"worker_blocked:{worker_id}:{until}",
                          f"⏸ Worker {row['name']} got bot checks on {videos} different videos and pauses its downloads for "
                          f"{policy.block_minutes} min (scans continue). Its tasks go to the next tier.", now)
    _escalate_blocked(conn, now)


def _short(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _assess(conn: sqlite3.Connection, task_id: int, now: datetime, policy: Policy) -> bool:
    """Pause a pending task that has failed too often. Returns True if it was paused.

    The failures since the last reopening are weighed by class; the task is paused when the weight reaches
    the threshold N and the failures come from at least two workers (from one if only one worker is active:
    two are impossible then and a lone blocked worker must still raise the alarm).
    """
    task = conn.execute("SELECT t.*, p.name AS podcast_name FROM tasks t JOIN podcasts p ON p.id = t.podcast_id "
                        "WHERE t.id = ?", (task_id,)).fetchone()
    if task is None or task["state"] != "pending":
        return False
    rows = conn.execute("SELECT worker_id, worker_name, error_class, error_text FROM attempts WHERE task_id = ? AND id > ? "
                        "AND result IN ('failed', 'lease_expired') ORDER BY id", (task_id, task["reset_attempt_id"])).fetchall()
    counted = [r for r in rows if FAIL_WEIGHTS.get(r["error_class"], 0) > 0]
    score = sum(FAIL_WEIGHTS[r["error_class"]] for r in counted)
    names = {r["worker_id"] if r["worker_id"] is not None else r["worker_name"] for r in counted}
    active = conn.execute("SELECT COUNT(*) FROM workers WHERE active = 1").fetchone()[0]
    if score < policy.failure_threshold or len(names) < min(2, max(1, active)):
        return False
    per_worker = Counter(r["worker_name"] or "?" for r in counted)
    reopen_at = ts(now + timedelta(hours=policy.reopen_hours))
    reason = f"{len(counted)} failed attempts from {len(per_worker)} worker(s), last: {counted[-1]['error_class']}"
    conn.execute("UPDATE tasks SET state = 'paused', pause_reason = ?, pause_count = pause_count + 1, reopen_at = ?, "
                 "not_before = NULL, updated_at = ? WHERE id = ?", (reason, reopen_at, ts(now), task_id))
    _event(conn, task_id, now, "paused", reason)
    title = task["title"] or task["video_id"] or "playlist scan"
    lines = [f"⏸ Task #{task_id} paused: {task['podcast_name']} – {title}"]
    if task["video_id"]:
        lines.append(f"Video: {task['video_id']}")
    lines.append(f"{len(counted)} failed attempts (weight {score:g}) from {len(per_worker)} worker(s): "
                 + ", ".join(f"{w} ×{c}" for w, c in per_worker.most_common()))
    lines.append("Last errors:")
    lines += [f"• {r['worker_name'] or '?'} ({r['error_class']}): {_short(r['error_text'], 200)}" for r in counted[-3:]]
    lines.append(f"It reopens by itself at {reopen_at} (UTC); release or close it in the UI"
                 + (f": {policy.base_url.rstrip('/')}/tasks/{task_id}" if policy.base_url else "."))
    notifications.enqueue(conn, "task_paused", f"task_paused:{task_id}:{task['pause_count'] + 1}", "\n".join(lines), now)
    return True


def _reopen(conn: sqlite3.Connection, task_id: int, now: datetime, how: str, tier: int = 1) -> None:
    """Make a paused or closed task pending again with a clean slate: no failures counted, at tier 1 (visible to
    everyone) or, for a manual retry, starting at ``tier`` (only workers of that tier and above see it)."""
    last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM attempts WHERE task_id = ?", (task_id,)).fetchone()[0]
    try:
        conn.execute("UPDATE tasks SET state = 'pending', tier = ?, min_tier = ?, tier_since = ?, not_before = NULL, "
                     "pause_reason = NULL, reopen_at = NULL, reset_attempt_id = ?, failures = 0, finished_at = NULL, updated_at = ? "
                     "WHERE id = ?", (tier, tier, ts(now), last, ts(now), task_id))
    except sqlite3.IntegrityError:
        raise BadState("another scan of this podcast is already open") from None
    _event(conn, task_id, now, "reopened", how + (f", starting at tier {tier}" if tier > 1 else ""))


def reopen_due(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Reopen paused tasks whose timer has run out."""
    now = now or utcnow()
    with transaction(conn):
        rows = conn.execute("SELECT id FROM tasks WHERE state = 'paused' AND reopen_at IS NOT NULL AND reopen_at <= ?",
                            (ts(now),)).fetchall()
        for r in rows:
            _reopen(conn, r["id"], now, "the reopen timer ran out")
        return len(rows)


# --- lease expiry ----------------------------------------------------------------------------------

def _expire(conn: sqlite3.Connection, now: datetime, policy: Policy = DEFAULT_POLICY) -> int:
    stamp = ts(now)
    rows = conn.execute("SELECT id FROM tasks WHERE state = 'claimed' AND lease_until < ?", (stamp,)).fetchall()
    for r in rows:
        conn.execute("UPDATE tasks SET state = 'pending', claimed_by = NULL, lease_until = NULL, progress = '', "
                     "failures = failures + 1, tier_since = ?, updated_at = ? WHERE id = ?", (stamp, stamp, r["id"]))
        conn.execute("UPDATE attempts SET ended_at = ?, result = 'lease_expired', error_class = 'timeout', "
                     "error_text = 'The lease expired: no heartbeat from the worker' "
                     "WHERE task_id = ? AND result IS NULL", (stamp, r["id"]))
        _assess(conn, r["id"], now, policy)
    return len(rows)


def expire_leases(conn: sqlite3.Connection, now: datetime | None = None, policy: Policy = DEFAULT_POLICY) -> int:
    """Return tasks with an expired lease to pending and record a ``lease_expired`` attempt."""
    with transaction(conn):
        return _expire(conn, now or utcnow(), policy)


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
          now: datetime | None = None, dropped: tuple[int, ...] | list[int] = ()) -> Claim:
    """Atomically hand the oldest eligible task to the worker, or say when to ask again.

    Rules: one task at a time per worker; only pending tasks that are due (not_before) of active
    podcasts and of the requested types; scans before downloads, oldest first; for downloads the
    pacing limits (downloads per hour, pause between downloads) apply, the worker's value capping
    the podcast's. Tier rule: a task is visible to workers of its current tier and below; it moves up
    when a worker of that tier failed it (see ``fail``) or nobody took it within the deadline. A worker
    does not get back a task it reported as failed in the last ten minutes, as long as another worker
    could take it (a crash that only expired the lease is not held against it; a manual retry resets it).
    """
    now = now or utcnow()
    policy = Policy.from_settings(defaults)
    with transaction(conn):
        _expire(conn, now, policy)
        _escalate_overdue(conn, now, policy)
        _escalate_blocked(conn, now)
        # Tasks the worker says it dropped (it could not deliver the result): take them back at once.
        for task_id in dropped:
            row = conn.execute("SELECT id FROM tasks WHERE id = ? AND claimed_by = ? AND state = 'claimed'",
                               (task_id, worker["id"])).fetchone()
            if row:
                conn.execute("UPDATE tasks SET state = 'pending', claimed_by = NULL, lease_until = NULL, progress = '', "
                             "tier_since = ?, updated_at = ? WHERE id = ?", (ts(now), ts(now), task_id))
                _close_open_attempt(conn, task_id, now, "the worker dropped this task (it could not deliver the result)")
        if conn.execute("SELECT 1 FROM tasks WHERE claimed_by = ? AND state = 'claimed'", (worker["id"],)).fetchone():
            return Claim(None, IDLE_HINT)
        blocked = conn.execute("SELECT download_blocked_until FROM workers WHERE id = ?", (worker["id"],)).fetchone()
        if blocked and blocked[0] and blocked[0] > ts(now):
            types = [t for t in types if t != "download"]
        if not types:
            return Claim(None, IDLE_HINT)
        marks = ",".join("?" * len(types))
        candidates = conn.execute(
            "SELECT t.*, p.pause_min_s AS p_pause_min, p.pause_max_s AS p_pause_max, p.downloads_per_hour AS p_dph "
            f"FROM tasks t JOIN podcasts p ON p.id = t.podcast_id WHERE t.state = 'pending' AND t.type IN ({marks}) "
            "AND (t.not_before IS NULL OR t.not_before <= ?) AND p.active = 1 AND t.tier >= ? AND t.min_tier <= ? "
            # cool-down: not a task this worker just failed, but only if some other worker could take it instead
            "AND NOT (EXISTS (SELECT 1 FROM attempts a WHERE a.task_id = t.id AND a.worker_id = ? AND a.id > t.reset_attempt_id "
            "AND a.result = 'failed' AND a.ended_at > ?) "
            "AND EXISTS (SELECT 1 FROM workers o WHERE o.active = 1 AND o.id != ? AND o.tier <= t.tier)) "
            "ORDER BY (t.type != 'scan'), t.id LIMIT 100",
            [*types, ts(now), worker["tier"], worker["tier"], worker["id"], ts(now - timedelta(seconds=COOLDOWN_SECONDS)), worker["id"]]).fetchall()
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
            cur = conn.execute("UPDATE tasks SET state = 'claimed', claimed_by = ?, lease_until = ?, progress = '', updated_at = ? "
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
              now: datetime | None = None, progress: str | None = None) -> str:
    """Extend the lease. Raises NotOwner if the task was taken away (lease expired and handed on).

    ``progress`` (what the worker is doing) replaces the stored text when given.
    """
    now = now or utcnow()
    with transaction(conn):
        _owned(conn, task_id, worker_id)
        lease_until = ts(now + timedelta(seconds=lease_seconds))
        if progress is None:
            conn.execute("UPDATE tasks SET lease_until = ?, updated_at = ? WHERE id = ?", (lease_until, ts(now), task_id))
        else:
            conn.execute("UPDATE tasks SET lease_until = ?, progress = ?, updated_at = ? WHERE id = ?",
                         (lease_until, clean(progress, 200), ts(now), task_id))
        return lease_until


def owned_attempt(conn: sqlite3.Connection, task_id: int, worker_id: int) -> tuple[sqlite3.Row, int]:
    """The claimed task and the id of its running attempt, or NotOwner."""
    task = _owned(conn, task_id, worker_id)
    row = conn.execute("SELECT id FROM attempts WHERE task_id = ? AND worker_id = ? AND result IS NULL ORDER BY id DESC LIMIT 1",
                       (task_id, worker_id)).fetchone()
    if row is None:
        raise NotOwner(task_id)
    return task, row["id"]


def complete_download(conn: sqlite3.Connection, task_id: int, worker_id: int, fields: dict,
                      now: datetime | None = None, policy: Policy = DEFAULT_POLICY) -> None:
    """Publish a verified package: create (or revive) the episode row and mark the task done, in one transaction.

    The files are already in the media directory; the feed only lists rows, so nothing shows up before this.
    """
    now = now or utcnow()
    with transaction(conn):
        task = _owned(conn, task_id, worker_id)
        if task["type"] != "download":
            raise BadState("not a download task")
        existing = conn.execute("SELECT state FROM episodes WHERE podcast_id = ? AND video_id = ?",
                                (task["podcast_id"], task["video_id"])).fetchone()
        if existing and existing["state"] == "available":
            raise BadState("this video already is an episode")
        cols = ["title", "description", "author", "audio_file", "thumb_file", "square_file", "chapters_file", "info_file",
                "size_bytes", "duration_s", "published_at"]
        if existing:  # known from an import or an earlier deletion: bring it back
            sets = ", ".join(f"{c} = ?" for c in cols)
            conn.execute(f"UPDATE episodes SET {sets}, state = 'available', updated_at = ? WHERE podcast_id = ? AND video_id = ?",
                         [*(fields[c] for c in cols), ts(now), task["podcast_id"], task["video_id"]])
        else:
            conn.execute(f"INSERT INTO episodes (podcast_id, video_id, {', '.join(cols)}, state, created_at, updated_at) "
                         f"VALUES (?, ?, {', '.join('?' * len(cols))}, 'available', ?, ?)",
                         [task["podcast_id"], task["video_id"], *(fields[c] for c in cols), ts(now), ts(now)])
        conn.execute("UPDATE tasks SET state = 'done', claimed_by = NULL, lease_until = NULL, progress = '', finished_at = ?, "
                     "updated_at = ? WHERE id = ?", (ts(now), ts(now), task_id))
        conn.execute("UPDATE attempts SET ended_at = ?, result = 'ok', note = ? WHERE task_id = ? AND result IS NULL",
                     (ts(now), f"published {fields['audio_file']} ({fields['size_bytes']} bytes)", task_id))
        if policy.notify_new_episode:
            name = conn.execute("SELECT name FROM podcasts WHERE id = ?", (task["podcast_id"],)).fetchone()["name"]
            link = f"\n{policy.base_url.rstrip('/')}/{task['podcast_id']}.xml" if policy.base_url else ""
            notifications.enqueue(conn, "episode_new", f"episode_new:{task['podcast_id']}:{task['video_id']}",
                                  f"🎧 New episode: {name} – {fields['title']}{link}", now)


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
        conn.execute("UPDATE tasks SET state = 'done', claimed_by = NULL, lease_until = NULL, progress = '', finished_at = ?, "
                     "updated_at = ? WHERE id = ?", (ts(now), ts(now), task_id))
        conn.execute("UPDATE attempts SET ended_at = ?, result = 'ok', note = ? WHERE task_id = ? AND result IS NULL",
                     (ts(now), note, task_id))
        return {"created": created, "known": known, "skipped": skipped}


def backoff_seconds(failures: int) -> int:
    return min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_START * 2 ** max(0, failures - 1))


def fail(conn: sqlite3.Connection, task_id: int, worker_id: int, error_class: str, message: str,
         retry_after: int | None = None, now: datetime | None = None,
         extra_secrets: tuple[str, ...] = (), policy: Policy = DEFAULT_POLICY) -> tuple[str, str | None]:
    """Record a failed attempt. Returns ``(new state, not_before)``.

    - ``not_yet_available`` (premiere/live, downloads only): not an error, the task waits until
      ``not_before`` and the failure counter does not move;
    - ``private`` / ``removed``: the task is closed, no retry;
    - everything else is retried after a growing backoff (the same worker is not offered the
      task before it is due again). ``bot_check``, ``internal`` and ``incomplete`` also make the task
      visible to the next tier at once (if the failing worker belongs to the task's current tier);
      then the task is paused if it has failed too often (see ``_assess``).
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
        conn.execute("UPDATE tasks SET state = ?, claimed_by = NULL, lease_until = NULL, progress = '', not_before = ?, failures = ?, "
                     "updated_at = ?, finished_at = ? WHERE id = ?", (state, not_before, failures, ts(now), finished, task_id))
        conn.execute("UPDATE attempts SET ended_at = ?, result = ?, error_class = ?, error_text = ? "
                     "WHERE task_id = ? AND result IS NULL", (ts(now), result, error_class, text, task_id))
        if state == "pending":
            conn.execute("UPDATE tasks SET tier_since = ? WHERE id = ?", (ts(now), task_id))
            if error_class in ESCALATING:
                row = conn.execute("SELECT tier FROM workers WHERE id = ?", (worker_id,)).fetchone()
                if row is None or row["tier"] >= task["tier"]:  # a failure of the current (or a higher) tier
                    new = _escalate(conn, task_id, task["tier"], now, f"a tier {row['tier'] if row else task['tier']} worker "
                                                                      f"failed it ({error_class})")
                    if new is not None:  # handed over: the next tier may take it right away, the backoff is for the same tier
                        conn.execute("UPDATE tasks SET not_before = NULL WHERE id = ?", (task_id,))
                        not_before = None
            if error_class == "bot_check" and task["type"] == "download":
                _maybe_block(conn, worker_id, now, policy)
            if result == "failed" and _assess(conn, task_id, now, policy):
                state, not_before = "paused", None
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
    """Take a claimed task away from its worker, or reopen a paused one; either way it is pending again."""
    now = now or utcnow()
    with transaction(conn):
        state = _get(conn, task_id)["state"]
        if state == "paused":
            _reopen(conn, task_id, now, "released manually")
            return
        if state != "claimed":
            raise BadState("only a claimed or paused task can be released")
        conn.execute("UPDATE tasks SET state = 'pending', claimed_by = NULL, lease_until = NULL, progress = '', tier_since = ?, "
                     "updated_at = ? WHERE id = ?", (ts(now), ts(now), task_id))
        _close_open_attempt(conn, task_id, now, "released manually")


def active_tiers(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT DISTINCT tier FROM workers WHERE active = 1 ORDER BY tier")]


def suggested_tier(conn: sqlite3.Connection, task_id: int, now: datetime | None = None) -> int:
    """Where a retry should start: the tier above the highest one that recently failed this task with a bot check
    (or another error that escalates), if some active worker is there; otherwise tier 1."""
    now = now or utcnow()
    marks = ",".join("?" * len(ESCALATING))
    top = conn.execute(f"SELECT MAX(w.tier) FROM attempts a JOIN workers w ON w.id = a.worker_id WHERE a.task_id = ? "
                       f"AND a.result = 'failed' AND a.error_class IN ({marks}) AND a.ended_at > ?",
                       [task_id, *sorted(ESCALATING), ts(now - timedelta(hours=SUGGEST_HOURS))]).fetchone()[0]
    return (_next_tier(conn, top) if top else None) or 1


def retry(conn: sqlite3.Connection, task_id: int, now: datetime | None = None, tier: int | None = None) -> int:
    """Make a pending (waiting), paused or closed task pending again and due now. Returns the tier it starts at.

    ``tier`` (default: ``suggested_tier``) is where it starts: above 1 only workers of that tier see it first, so a
    tier that is known to be blocked is skipped. Raises BadState if no active worker has that tier.
    """
    now = now or utcnow()
    with transaction(conn):
        task = _get(conn, task_id)
        if task["state"] not in ("pending", "paused", "closed"):
            raise BadState("only a waiting, paused or closed task can be retried")
        if tier is None:
            tier = suggested_tier(conn, task_id, now) if task["type"] == "download" else 1
        if tier != 1 and tier not in active_tiers(conn):
            raise BadState(f"no active worker has tier {tier}")
        if task["state"] == "pending":  # waiting for not_before or a backoff: make it due now, with a clean slate
            last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM attempts WHERE task_id = ?", (task_id,)).fetchone()[0]
            conn.execute("UPDATE tasks SET not_before = NULL, tier = ?, min_tier = ?, tier_since = ?, reset_attempt_id = ?, "
                         "updated_at = ? WHERE id = ?", (tier, tier, ts(now), last, ts(now), task_id))
            _event(conn, task_id, now, "reopened", "retried manually" + (f", starting at tier {tier}" if tier > 1 else ""))
        else:
            _reopen(conn, task_id, now, "retried manually", tier)
        return tier


def close(conn: sqlite3.Connection, task_id: int, now: datetime | None = None) -> None:
    """Close an open task by hand."""
    now = now or utcnow()
    with transaction(conn):
        if _get(conn, task_id)["state"] not in OPEN_STATES:
            raise BadState("only an open task can be closed")
        conn.execute("UPDATE tasks SET state = 'closed', claimed_by = NULL, lease_until = NULL, progress = '', finished_at = ?, "
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


def events_of(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY id DESC", (task_id,)).fetchall()


def error_stats(conn: sqlite3.Connection, now: datetime | None = None, days: int = 14) -> dict:
    """Download success per worker (last 7 days) and bot_check failures per day, for the dashboard."""
    now = now or utcnow()
    week = ts(now - timedelta(days=7))
    per_worker = conn.execute(
        "SELECT a.worker_name AS name, SUM(a.result = 'ok') AS ok, SUM(a.result IN ('failed', 'lease_expired')) AS failed "
        "FROM attempts a JOIN tasks t ON t.id = a.task_id WHERE t.type = 'download' AND a.started_at >= ? "
        "AND a.result IN ('ok', 'failed', 'lease_expired') GROUP BY a.worker_name ORDER BY a.worker_name", (week,)).fetchall()
    since = (now - timedelta(days=days - 1)).date().isoformat()
    per_day = {r["d"]: r["n"] for r in conn.execute(
        "SELECT substr(started_at, 1, 10) AS d, COUNT(*) AS n FROM attempts WHERE error_class = 'bot_check' AND started_at >= ? "
        "GROUP BY d", (since,))}
    return {"workers": [dict(r) for r in per_worker], "bot_check_per_day": per_day}


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


def requeue_download(conn: sqlite3.Connection, podcast_id: str, video_id: str, title: str = "",
                     now: datetime | None = None) -> int:
    """Queue a fresh download for a video that has been fetched (or tried) before. Returns the task id.

    There is one download task per video, so a finished or closed one is reused with a clean slate.
    Raises BadState if the download is still open.
    """
    now = now or utcnow()
    with transaction(conn):
        task = conn.execute("SELECT * FROM tasks WHERE type = 'download' AND podcast_id = ? AND video_id = ?",
                            (podcast_id, video_id)).fetchone()
        if task is None:
            cur = conn.execute("INSERT INTO tasks (type, podcast_id, video_id, title, created_at, updated_at) "
                               "VALUES ('download', ?, ?, ?, ?, ?)", (podcast_id, video_id, title, ts(now), ts(now)))
            return cur.lastrowid
        if task["state"] in OPEN_STATES:
            raise BadState("a download of this video is already queued or running")
        last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM attempts WHERE task_id = ?", (task["id"],)).fetchone()[0]
        conn.execute("UPDATE tasks SET state = 'pending', tier = 1, tier_since = ?, not_before = NULL, claimed_by = NULL, "
                     "lease_until = NULL, pause_reason = NULL, reopen_at = NULL, reset_attempt_id = ?, failures = 0, "
                     "min_tier = 1, progress = '', finished_at = NULL, title = ?, updated_at = ? WHERE id = ?",
                     (ts(now), last, title or task["title"], ts(now), task["id"]))
        _event(conn, task["id"], now, "reopened", "download requested again")
        return task["id"]
