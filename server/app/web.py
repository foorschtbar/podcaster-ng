"""Shared building blocks of the admin UI: templates, dependencies, routers.

Routes are added to ``router`` (login and other pre-session routes) or to
``protected`` (session and CSRF required). ``routes.py`` assembles them.
"""

import hmac
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, settings
from .db import connect

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


templates.env.filters["human_size"] = human_size

COOKIE = "pn_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PAGES = [
    ("dashboard", "Dashboard"),
    ("podcasts", "Podcasts"),
    ("episodes", "Episodes"),
    ("workers", "Workers"),
    ("tasks", "Tasks"),
    ("events", "Events"),
    ("settings", "Settings"),
]


class NotConfigured(Exception):
    pass


class LoginRequired(Exception):
    pass


class CsrfError(Exception):
    pass


@dataclass
class Session:
    user: str
    csrf: str


def client_of(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def locked_response(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "locked.html", status_code=503)


def check_configured(request: Request) -> None:
    request.state.admin = True  # marks the response for the admin-only security headers
    if not request.app.state.config.admin_configured:
        raise NotConfigured


def require_session(request: Request) -> Session:
    payload = auth.unsign(request.app.state.secret, "session", request.cookies.get(COOKIE))
    if not payload or payload.get("u") != request.app.state.config.admin_user:
        raise LoginRequired
    return Session(user=payload["u"], csrf=payload["csrf"])


async def csrf_protect(request: Request, session: Session = Depends(require_session)) -> None:
    if request.method in SAFE_METHODS:
        return
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        sent = (await request.form()).get("csrf_token")
    if not isinstance(sent, str) or not hmac.compare_digest(sent, session.csrf):
        raise CsrfError


def render(request: Request, name: str, session: Session | None = None, status_code: int = 200, **ctx):
    return templates.TemplateResponse(
        request, name, {"session": session, "pages": PAGES, **ctx}, status_code=status_code
    )


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@contextmanager
def db(request: Request):
    """One short-lived connection per use, opened in the calling thread."""
    conn = connect(request.app.state.config.db_path)
    try:
        yield conn
    finally:
        conn.close()


def env_defaults(request: Request) -> dict:
    return {"base_url": request.app.state.config.base_url}


router = APIRouter(dependencies=[Depends(check_configured)])
protected = APIRouter(dependencies=[Depends(require_session), Depends(csrf_protect)])
