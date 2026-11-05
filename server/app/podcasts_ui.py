"""Admin UI: podcasts (list, create, edit, pause/resume, delete)."""

from fastapi import Depends, Path, Request
from fastapi.responses import HTMLResponse

from . import episodes, podcasts, queue, settings
from .web import Session, db, env_defaults, protected, redirect, render, require_session

FLASH = {
    "created": "Podcast created.",
    "updated": "Podcast saved.",
    "paused": "Podcast paused.",
    "resumed": "Podcast resumed.",
    "deleted": "Podcast deleted.",
    "scan_open": "A scan of this podcast is already queued or running.",
    "scan_paused": "This podcast is paused. Resume it first, then scan.",
}
SLUG = Path(pattern=r"^[a-z0-9-]{1,64}$")
FORM_KEYS = ["id", "name", "author", "description", "playlist_url", "image_url", "scan_interval_min", "scan_depth",
             "pause_min_s", "pause_max_s", "downloads_per_hour", "keep_last", "max_age_days"]


def _values(row) -> dict:
    """Form values from a database row (None becomes an empty field)."""
    v = {k: ("" if row[k] is None else row[k]) for k in FORM_KEYS}
    v["active"] = bool(row["active"])
    return v


def _form_page(request, session, *, creating: bool, values: dict, errors: dict, podcast_id: str | None = None,
               status_code: int = 200):
    with db(request) as conn:
        defaults = settings.get_all(conn, env_defaults(request))
        recent = episodes.recent(conn, podcast_id, 10) if podcast_id else []
    return render(request, "podcast_form.html", session, status_code=status_code, active="podcasts",
                  title="New podcast" if creating else "Edit podcast", creating=creating, v=values,
                  errors=errors, defaults=defaults, podcast_id=podcast_id, recent=recent)


@protected.get("/podcasts", response_class=HTMLResponse, include_in_schema=False)
def podcast_list(request: Request, msg: str | None = None, session: Session = Depends(require_session)):
    with db(request) as conn:
        items = podcasts.list_all(conn)
        stats = episodes.stats_by_podcast(conn)
        defaults = settings.get_all(conn, env_defaults(request))
    return render(request, "podcasts.html", session, active="podcasts", title="Podcasts", podcasts=items,
                  stats=stats, defaults=defaults, flash=FLASH.get(msg or ""))


@protected.get("/podcasts/new", response_class=HTMLResponse, include_in_schema=False)
def podcast_new(request: Request, session: Session = Depends(require_session)):
    values = {k: "" for k in FORM_KEYS} | {"scan_depth": podcasts.DEFAULT_SCAN_DEPTH, "active": True}
    return _form_page(request, session, creating=True, values=values, errors={})


@protected.post("/podcasts", response_class=HTMLResponse, include_in_schema=False)
async def podcast_create(request: Request, session: Session = Depends(require_session)):
    form = await request.form()
    with db(request) as conn:
        defaults = settings.get_all(conn, env_defaults(request))
        data, errors = podcasts.validate(form, defaults, creating=True)
        if "id" not in errors and podcasts.get(conn, data["id"]):
            errors["id"] = "is already used by another podcast"  # reported together with other errors
        if not errors:
            try:
                podcasts.create(conn, data)
                return redirect("/podcasts?msg=created")
            except podcasts.SlugExists:
                errors["id"] = "is already used by another podcast"
    values = {k: str(form.get(k) or "") for k in FORM_KEYS} | {"active": bool(form.get("active"))}
    return _form_page(request, session, creating=True, values=values, errors=errors, status_code=422)


@protected.get("/podcasts/{podcast_id}", response_class=HTMLResponse, include_in_schema=False)
def podcast_edit(request: Request, podcast_id: str = SLUG, session: Session = Depends(require_session)):
    with db(request) as conn:
        row = podcasts.get(conn, podcast_id)
    if not row:
        return _not_found(request, session)
    return _form_page(request, session, creating=False, values=_values(row), errors={}, podcast_id=podcast_id)


@protected.post("/podcasts/{podcast_id}", response_class=HTMLResponse, include_in_schema=False)
async def podcast_update(request: Request, podcast_id: str = SLUG, session: Session = Depends(require_session)):
    form = await request.form()
    with db(request) as conn:
        if not podcasts.get(conn, podcast_id):
            return _not_found(request, session)
        defaults = settings.get_all(conn, env_defaults(request))
        data, errors = podcasts.validate(form, defaults, creating=False)
        if not errors:
            podcasts.update(conn, podcast_id, data)
            return redirect("/podcasts?msg=updated")
    values = {k: str(form.get(k) or "") for k in FORM_KEYS} | {"id": podcast_id, "active": bool(form.get("active"))}
    return _form_page(request, session, creating=False, values=values, errors=errors, podcast_id=podcast_id,
                      status_code=422)


def _toggle(request: Request, podcast_id: str, session: Session, active: bool):
    with db(request) as conn:
        if not podcasts.set_active(conn, podcast_id, active):
            return _not_found(request, session)
    return redirect("/podcasts?msg=" + ("resumed" if active else "paused"))


@protected.post("/podcasts/{podcast_id}/pause", include_in_schema=False)
def podcast_pause(request: Request, podcast_id: str = SLUG, session: Session = Depends(require_session)):
    return _toggle(request, podcast_id, session, False)


@protected.post("/podcasts/{podcast_id}/resume", include_in_schema=False)
def podcast_resume(request: Request, podcast_id: str = SLUG, session: Session = Depends(require_session)):
    return _toggle(request, podcast_id, session, True)


@protected.post("/podcasts/{podcast_id}/scan", include_in_schema=False)
def podcast_scan(request: Request, podcast_id: str = SLUG, session: Session = Depends(require_session)):
    """'Scan now': queue a scan task immediately (the scheduler does it on the interval)."""
    with db(request) as conn:
        podcast = podcasts.get(conn, podcast_id)
        if not podcast:
            return _not_found(request, session)
        if not podcast["active"]:
            return redirect("/podcasts?msg=scan_paused")
        task_id = queue.create_scan(conn, podcast_id)
    return redirect(f"/tasks/{task_id}") if task_id else redirect("/podcasts?msg=scan_open")


@protected.get("/podcasts/{podcast_id}/delete", response_class=HTMLResponse, include_in_schema=False)
def podcast_delete_confirm(request: Request, podcast_id: str = SLUG, session: Session = Depends(require_session)):
    with db(request) as conn:
        row = podcasts.get(conn, podcast_id)
        n_episodes = episodes.count_for(conn, podcast_id) if row else 0
    if not row:
        return _not_found(request, session)
    return render(request, "podcast_delete.html", session, active="podcasts", title="Delete podcast", p=row,
                  n_episodes=n_episodes)


@protected.post("/podcasts/{podcast_id}/delete", include_in_schema=False)
def podcast_delete(request: Request, podcast_id: str = SLUG, session: Session = Depends(require_session)):
    with db(request) as conn:
        row = podcasts.get(conn, podcast_id)
        if not row:
            return _not_found(request, session)
        n_episodes = episodes.count_for(conn, podcast_id)
        if n_episodes:  # refuse; episode and file removal comes with the episode management
            return render(request, "podcast_delete.html", session, status_code=409, active="podcasts",
                          title="Delete podcast", p=row, n_episodes=n_episodes)
        podcasts.delete(conn, podcast_id)
    return redirect("/podcasts?msg=deleted")


def _not_found(request: Request, session: Session):
    return render(request, "not_found.html", session, status_code=404, active="podcasts", title="Not found")
