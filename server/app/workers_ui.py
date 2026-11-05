"""Admin UI: workers (create, edit, activate, regenerate token, delete)."""

from fastapi import Depends, Path, Request
from fastapi.responses import HTMLResponse

from . import queue, settings, workers
from .web import Session, db, env_defaults, protected, redirect, render, require_session, templates

FLASH = {"updated": "Worker saved.", "activated": "Worker activated.", "deactivated": "Worker deactivated.",
         "deleted": "Worker deleted."}
FORM_KEYS = ["name", "tier", "pause_min_s", "pause_max_s", "downloads_per_hour"]
ID = Path(ge=1, le=2**31 - 1)


def ago(seconds) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 90:
        return f"{max(seconds, 0)} s ago"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


templates.env.filters["ago"] = ago


def worker_cards(conn, defaults: dict) -> list[dict]:
    """Workers with their computed status; also used by the dashboard."""
    online = defaults["worker_online_seconds"]
    cards = []
    for row in workers.list_all(conn):
        cards.append({"row": row, "status": workers.status(row, online), "seen": workers.seconds_since_seen(row),
                      "capabilities": workers.capabilities_of(row), "tools": workers.tools_of(row),
                      "blocked_until": row["download_blocked_until"] if (row["download_blocked_until"] or "") > queue.ts(queue.utcnow()) else None})
    return cards


def _values(row) -> dict:
    v = {k: ("" if row[k] is None else row[k]) for k in FORM_KEYS}
    v["active"] = bool(row["active"])
    return v


def _form_page(request, session, *, creating, values, errors, worker=None, status_code=200):
    with db(request) as conn:
        defaults = settings.get_all(conn, env_defaults(request))
    return render(request, "worker_form.html", session, status_code=status_code, active="workers",
                  title="New worker" if creating else "Edit worker", creating=creating, v=values, errors=errors,
                  defaults=defaults, worker=worker)


def _not_found(request, session):
    return render(request, "not_found.html", session, status_code=404, active="workers", title="Not found",
                  back="/workers", back_label="Back to workers")


def _token_page(request, session, worker, token: str, *, created: bool):
    with db(request) as conn:
        defaults = settings.get_all(conn, env_defaults(request))
    server_url = defaults["base_url"] or str(request.base_url).rstrip("/")
    return render(request, "worker_token.html", session, active="workers", title="Worker token", worker=worker,
                  token=token, created=created, server_url=server_url)


@protected.get("/workers", response_class=HTMLResponse, include_in_schema=False)
def worker_list(request: Request, msg: str | None = None, session: Session = Depends(require_session)):
    with db(request) as conn:
        defaults = settings.get_all(conn, env_defaults(request))
        cards = worker_cards(conn, defaults)
    return render(request, "workers.html", session, active="workers", title="Workers", cards=cards,
                  online_seconds=defaults["worker_online_seconds"], flash=FLASH.get(msg or ""))


@protected.get("/workers/new", response_class=HTMLResponse, include_in_schema=False)
def worker_new(request: Request, session: Session = Depends(require_session)):
    values = {k: "" for k in FORM_KEYS} | {"tier": 1, "active": True}
    return _form_page(request, session, creating=True, values=values, errors={})


@protected.post("/workers", response_class=HTMLResponse, include_in_schema=False)
async def worker_create(request: Request, session: Session = Depends(require_session)):
    form = await request.form()
    with db(request) as conn:
        defaults = settings.get_all(conn, env_defaults(request))
        data, errors = workers.validate(form, defaults)
        if not errors:
            try:
                worker_id, token = workers.create(conn, data)
                # The token is only ever shown in this response, never in a redirect or URL.
                return _token_page(request, session, workers.get(conn, worker_id), token, created=True)
            except workers.NameExists:
                errors["name"] = "is already used by another worker"
        elif data.get("name") and workers.NAME_RE.match(data["name"]) and conn.execute(
                "SELECT 1 FROM workers WHERE name = ?", (data["name"],)).fetchone():
            errors["name"] = "is already used by another worker"
    values = {k: str(form.get(k) or "") for k in FORM_KEYS} | {"active": bool(form.get("active"))}
    return _form_page(request, session, creating=True, values=values, errors=errors, status_code=422)


@protected.get("/workers/{worker_id}", response_class=HTMLResponse, include_in_schema=False)
def worker_edit(request: Request, worker_id: int = ID, session: Session = Depends(require_session)):
    with db(request) as conn:
        row = workers.get(conn, worker_id)
    if not row:
        return _not_found(request, session)
    return _form_page(request, session, creating=False, values=_values(row), errors={}, worker=row)


@protected.post("/workers/{worker_id}", response_class=HTMLResponse, include_in_schema=False)
async def worker_update(request: Request, worker_id: int = ID, session: Session = Depends(require_session)):
    form = await request.form()
    with db(request) as conn:
        row = workers.get(conn, worker_id)
        if not row:
            return _not_found(request, session)
        defaults = settings.get_all(conn, env_defaults(request))
        data, errors = workers.validate(form, defaults)
        if not errors:
            try:
                workers.update(conn, worker_id, data)
                return redirect("/workers?msg=updated")
            except workers.NameExists:
                errors["name"] = "is already used by another worker"
        elif data.get("name") and workers.NAME_RE.match(data["name"]) and conn.execute(
                "SELECT 1 FROM workers WHERE name = ? AND id != ?", (data["name"], worker_id)).fetchone():
            errors["name"] = "is already used by another worker"
    values = {k: str(form.get(k) or "") for k in FORM_KEYS} | {"active": bool(form.get("active"))}
    return _form_page(request, session, creating=False, values=values, errors=errors, worker=row, status_code=422)


def _toggle(request, worker_id, session, active):
    with db(request) as conn:
        if not workers.set_active(conn, worker_id, active):
            return _not_found(request, session)
    return redirect("/workers?msg=" + ("activated" if active else "deactivated"))


@protected.post("/workers/{worker_id}/deactivate", include_in_schema=False)
def worker_deactivate(request: Request, worker_id: int = ID, session: Session = Depends(require_session)):
    return _toggle(request, worker_id, session, False)


@protected.post("/workers/{worker_id}/activate", include_in_schema=False)
def worker_activate(request: Request, worker_id: int = ID, session: Session = Depends(require_session)):
    return _toggle(request, worker_id, session, True)


def _confirm(request, session, worker_id, *, action: str):
    with db(request) as conn:
        row = workers.get(conn, worker_id)
    if not row:
        return _not_found(request, session)
    return render(request, "worker_confirm.html", session, active="workers", title="Confirm", worker=row, action=action)


@protected.get("/workers/{worker_id}/regenerate", response_class=HTMLResponse, include_in_schema=False)
def worker_regenerate_confirm(request: Request, worker_id: int = ID, session: Session = Depends(require_session)):
    return _confirm(request, session, worker_id, action="regenerate")


@protected.post("/workers/{worker_id}/regenerate", response_class=HTMLResponse, include_in_schema=False)
def worker_regenerate(request: Request, worker_id: int = ID, session: Session = Depends(require_session)):
    with db(request) as conn:
        token = workers.regenerate_token(conn, worker_id)
        if token is None:
            return _not_found(request, session)
        return _token_page(request, session, workers.get(conn, worker_id), token, created=False)


@protected.get("/workers/{worker_id}/delete", response_class=HTMLResponse, include_in_schema=False)
def worker_delete_confirm(request: Request, worker_id: int = ID, session: Session = Depends(require_session)):
    return _confirm(request, session, worker_id, action="delete")


@protected.post("/workers/{worker_id}/delete", include_in_schema=False)
def worker_delete(request: Request, worker_id: int = ID, session: Session = Depends(require_session)):
    with db(request) as conn:
        if not workers.delete(conn, worker_id):
            return _not_found(request, session)
    return redirect("/workers?msg=deleted")
