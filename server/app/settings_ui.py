"""Admin UI: global settings."""

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from . import settings
from .web import Session, db, env_defaults, protected, redirect, render, require_session


def _page(request: Request, session: Session, values: dict, errors: dict, *, secrets_set: dict,
          flash: str | None = None, status_code: int = 200):
    return render(request, "settings.html", session, status_code=status_code, active="settings",
                  title="Settings", groups=settings.groups(), v=values, errors=errors,
                  secrets_set=secrets_set, flash=flash)


def _display(values: dict) -> dict:
    # Secrets are never rendered back; None becomes an empty field.
    return {k: ("" if val is None or settings.BY_KEY[k].kind == "secret" else val) for k, val in values.items()}


@protected.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_form(request: Request, saved: int = 0, session: Session = Depends(require_session)):
    with db(request) as conn:
        values = settings.get_all(conn, env_defaults(request))
    return _page(request, session, _display(values), {}, flash="Settings saved." if saved else None,
                 secrets_set={k: bool(values[k]) for k, f in settings.BY_KEY.items() if f.kind == "secret"})


@protected.post("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_save(request: Request, session: Session = Depends(require_session)):
    form = await request.form()
    with db(request) as conn:
        changes, errors = settings.validate(form, env_defaults(request))
        if not errors:
            settings.save(conn, changes)
            return redirect("/settings?saved=1")
        current = settings.get_all(conn, env_defaults(request))
    values = {f.key: str(form.get(f.key) or "") for f in settings.FIELDS if f.kind != "secret"}
    return _page(request, session, values, errors, status_code=422,
                 secrets_set={k: bool(current[k]) for k, f in settings.BY_KEY.items() if f.kind == "secret"})
