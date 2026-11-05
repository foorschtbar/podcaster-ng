"""Admin UI: tasks (list, details with attempts, release / retry / close)."""

from datetime import datetime, timezone

from fastapi import Depends, Form, Path, Request
from fastapi.responses import HTMLResponse

from . import podcasts, queue
from .web import Session, db, protected, redirect, render, require_session, templates

STATES = ("pending", "claimed", "paused", "done", "closed")
TYPES = ("scan", "download")
PAGE_SIZE = 50
FLASH = {"released": "Task released: it is pending again.", "retried": "Task is pending again and due now.",
         "closed": "Task closed."}
ID = Path(ge=1, le=2**31 - 1)


def until(stamp: str | None) -> str:
    """'in 5 min' / '3 h ago' relative to now for UTC timestamps (blank if none)."""
    if not stamp:
        return ""
    seconds = (queue.parse(stamp) - datetime.now(timezone.utc)).total_seconds()
    future, seconds = seconds > 0, abs(int(seconds))
    for limit, unit, size in ((90, "s", 1), (90 * 60, "min", 60), (36 * 3600, "h", 3600), (10**9, "days", 86400)):
        if seconds < limit:
            text = f"{max(1, round(seconds / size))} {unit}"
            break
    return f"in {text}" if future else f"{text} ago"


templates.env.filters["until"] = until


def _filters(state, podcast, type_):
    """Whitelist query parameters; anything unknown simply means 'no filter'."""
    return (state if state in STATES else None,
            podcast if podcast and podcasts.SLUG_RE.match(podcast) else None,
            type_ if type_ in TYPES else None)


@protected.get("/tasks", response_class=HTMLResponse, include_in_schema=False)
def task_list(request: Request, state: str | None = None, podcast: str | None = None, type: str | None = None,
              page: int = 1, session: Session = Depends(require_session)):
    state, podcast, type_ = _filters(state, podcast, type)
    page = max(1, min(page, 10_000))
    with db(request) as conn:
        total = queue.count_tasks(conn, state, podcast, type_)
        rows = queue.list_tasks(conn, state, podcast, type_, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        all_podcasts = podcasts.list_all(conn)
        counts = queue.counts(conn)
    query = "&".join(f"{k}={v}" for k, v in (("state", state), ("podcast", podcast), ("type", type_)) if v)
    return render(request, "tasks.html", session, active="tasks", title="Tasks", tasks=rows, total=total, page=page,
                  page_count=max(1, -(-total // PAGE_SIZE)), state=state, podcast=podcast, type=type_, query=query,
                  states=STATES, types=TYPES, all_podcasts=all_podcasts, counts=counts)


def _detail(request, session, task_id, *, status_code=200, error=None, flash=None):
    with db(request) as conn:
        task = queue.get_task(conn, task_id)
        attempts = queue.attempts_of(conn, task_id) if task else []
        events = queue.events_of(conn, task_id) if task else []
        tiers = queue.active_tiers(conn)
        suggested = queue.suggested_tier(conn, task_id) if task and task["type"] == "download" else 1
    if not task:
        return render(request, "not_found.html", session, status_code=404, active="tasks", title="Not found",
                      back="/tasks", back_label="Back to tasks")
    return render(request, "task_detail.html", session, status_code=status_code, active="tasks",
                  title=f"Task {task_id}", task=task, attempts=attempts, events=events, error=error, flash=flash, tiers=tiers,
                  suggested=suggested)


@protected.get("/tasks/{task_id}", response_class=HTMLResponse, include_in_schema=False)
def task_detail(request: Request, task_id: int = ID, msg: str | None = None, session: Session = Depends(require_session)):
    return _detail(request, session, task_id, flash=FLASH.get(msg or ""))


def _action(request, session, task_id, fn, msg):
    try:
        with db(request) as conn:
            fn(conn, task_id)
    except queue.TaskNotFound:
        return _detail(request, session, task_id)
    except queue.BadState as exc:
        return _detail(request, session, task_id, status_code=409, error=f"Not possible: {exc}")
    return redirect(f"/tasks/{task_id}?msg={msg}")


@protected.post("/tasks/{task_id}/release", include_in_schema=False)
def task_release(request: Request, task_id: int = ID, session: Session = Depends(require_session)):
    return _action(request, session, task_id, queue.release, "released")


@protected.post("/tasks/{task_id}/retry", include_in_schema=False)
def task_retry(request: Request, task_id: int = ID, tier: str = Form(""), session: Session = Depends(require_session)):
    """Retry, optionally starting at a chosen tier (empty: the suggested one)."""
    if tier and not (tier.isdigit() and 1 <= int(tier) <= 99):
        return _detail(request, session, task_id, status_code=422, error="Not possible: the tier must be a number.")
    chosen = int(tier) if tier else None
    return _action(request, session, task_id, lambda conn, tid: queue.retry(conn, tid, tier=chosen), "retried")


@protected.post("/tasks/{task_id}/close", include_in_schema=False)
def task_close(request: Request, task_id: int = ID, session: Session = Depends(require_session)):
    return _action(request, session, task_id, queue.close, "closed")
