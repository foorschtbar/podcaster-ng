"""Admin UI: podcasts (list, create, edit, pause/resume, delete)."""

from fastapi import Depends, Path, Request
from fastapi.responses import HTMLResponse

from starlette.concurrency import run_in_threadpool

from . import covers, episodes, podcasts, queue, retention, settings
from .web import Session, db, env_defaults, protected, redirect, render, require_session

FLASH = {
    "created": "Podcast created.",
    "updated": "Podcast saved.",
    "paused": "Podcast paused.",
    "resumed": "Podcast resumed.",
    "deleted": "Podcast deleted.",
    "scan_open": "A scan of this podcast is already queued or running.",
    "scan_paused": "This podcast is paused. Resume it first, then scan.",
    "cover_refreshed": "The cover image was fetched again.",
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
        stored = podcasts.get(conn, podcast_id) if podcast_id else None
        retention_plan = retention.plan(conn, defaults, podcast_id)[0] if podcast_id and podcasts.get(conn, podcast_id) else None
    return render(request, "podcast_form.html", session, status_code=status_code, active="podcasts",
                  title="New podcast" if creating else "Edit podcast", creating=creating, v=values,
                  errors=errors, defaults=defaults, podcast_id=podcast_id, recent=recent, retention_plan=retention_plan,
                  cover_file=stored["cover_file"] if stored else None)


async def _cover_plan(request: Request, form, data: dict, stored) -> tuple[str | None, tuple | None, dict]:
    """Decide what the form does to the cover: ``(action, payload, errors)``.

    An uploaded file wins over everything; *remove* wins over a URL; a URL is fetched when it is new or changed
    (so saving an unrelated change never depends on a remote server); emptying the URL of a fetched cover removes it.
    """
    upload = form.get("cover_upload")
    if getattr(upload, "filename", ""):
        raw = await upload.read(covers.MAX_BYTES + 1)
        try:
            covers.check_bytes(raw)
        except covers.CoverError as exc:
            return None, None, {"cover_upload": f"cannot be used: {exc}"}
        return "store", (raw, ""), {}
    if form.get("cover_remove"):
        return "remove", None, {}
    url, old_url = data["image_url"], (stored["image_url"] if stored else "")
    if url and url != old_url:
        try:
            raw = await run_in_threadpool(covers.fetch, url, request.app.state.http_transport)
        except covers.CoverError as exc:
            return None, None, {"image_url": f"could not be fetched: {exc}"}
        return "store", (raw, url), {}
    if not url and old_url and stored and stored["cover_file"]:
        return "remove", None, {}
    return None, None, {}


def _apply_cover(request: Request, conn, podcast_id: str, action, payload) -> None:
    media = request.app.state.config.media_dir
    if action == "store":
        covers.store(conn, media, podcast_id, payload[0], payload[1])
    elif action == "remove":
        covers.remove(conn, media, podcast_id)


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
        action = payload = None
        if not errors:
            action, payload, cover_errors = await _cover_plan(request, form, data, None)
            errors.update(cover_errors)
        if not errors:
            try:
                podcasts.create(conn, data)
                _apply_cover(request, conn, data["id"], action, payload)
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
            action, payload, errors = await _cover_plan(request, form, data, podcasts.get(conn, podcast_id))
        if not errors:
            podcasts.update(conn, podcast_id, data)
            _apply_cover(request, conn, podcast_id, action, payload)
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


@protected.post("/podcasts/{podcast_id}/cover/refresh", response_class=HTMLResponse, include_in_schema=False)
async def podcast_cover_refresh(request: Request, podcast_id: str = SLUG, session: Session = Depends(require_session)):
    """Fetch the cover from its source URL again."""
    with db(request) as conn:
        row = podcasts.get(conn, podcast_id)
    if not row:
        return _not_found(request, session)
    try:
        if not row["image_url"]:
            raise covers.CoverError("this podcast has no image URL to fetch from")
        raw = await run_in_threadpool(covers.fetch, row["image_url"], request.app.state.http_transport)
    except covers.CoverError as exc:
        return _form_page(request, session, creating=False, values=_values(row), errors={"image_url": f"could not be fetched: {exc}"},
                          podcast_id=podcast_id, status_code=502)
    with db(request) as conn:
        covers.store(conn, request.app.state.config.media_dir, podcast_id, raw, row["image_url"])
    return redirect("/podcasts?msg=cover_refreshed")


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
        covers.remove(conn, request.app.state.config.media_dir, podcast_id)
        podcasts.delete(conn, podcast_id)
    return redirect("/podcasts?msg=deleted")


def _not_found(request: Request, session: Session):
    return render(request, "not_found.html", session, status_code=404, active="podcasts", title="Not found")
