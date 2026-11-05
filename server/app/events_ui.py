"""Admin UI: the event list."""

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from . import events, podcasts
from .web import Session, db, protected, render, require_session

PAGE_SIZE = 50
LABELS = {"task": "Tasks", "escalation": "Escalation", "retention": "Deletions", "message": "Telegram"}


@protected.get("/events", response_class=HTMLResponse, include_in_schema=False)
def event_list(request: Request, kind: str | None = None, podcast: str | None = None, page: int = 1,
               session: Session = Depends(require_session)):
    kind = kind if kind in events.KINDS else None
    podcast = podcast if podcast and podcasts.SLUG_RE.match(podcast) else None
    page = max(1, min(page, 10_000))
    with db(request) as conn:
        total = events.count(conn, kind, podcast)
        rows = events.list_events(conn, kind, podcast, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        all_podcasts = podcasts.list_all(conn)
    query = "&".join(f"{k}={v}" for k, v in (("kind", kind), ("podcast", podcast)) if v)
    return render(request, "events.html", session, active="events", title="Events", rows=rows, total=total, page=page,
                  page_count=max(1, -(-total // PAGE_SIZE)), kind=kind, podcast=podcast, query=query, labels=LABELS,
                  all_podcasts=all_podcasts)
