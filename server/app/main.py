"""FastAPI application."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .admin import CsrfError, LoginRequired, NotConfigured, locked_response, router as admin_router
from .auth import LoginLimiter, load_session_secret
from .config import Config
from .db import connect
from .migrations import migrate

log = logging.getLogger(__name__)

ADMIN_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; form-action 'self'",
}


def create_app(config: Config | None = None, limiter: LoginLimiter | None = None) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(config.db_path, config.backup_dir)  # no-op if already done at startup
        app.state.secret = load_session_secret(config)
        if not config.admin_configured:
            log.warning("ADMIN_USER/ADMIN_PASSWORD not set: admin UI is locked")
        log.info("PodcasterNG server %s started", __version__)
        yield

    app = FastAPI(title="PodcasterNG", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.limiter = limiter or LoginLimiter()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if getattr(request.state, "admin", False):
            for key, value in ADMIN_HEADERS.items():
                response.headers.setdefault(key, value)
        return response

    @app.exception_handler(NotConfigured)
    def not_configured(request: Request, exc: NotConfigured):
        return locked_response(request)

    @app.exception_handler(LoginRequired)
    def login_required(request: Request, exc: LoginRequired):
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(CsrfError)
    def csrf_error(request: Request, exc: CsrfError):
        return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)

    @app.get("/healthz")
    def healthz():
        try:
            conn = connect(config.db_path)
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception:
            log.exception("Health check failed")
            return JSONResponse({"status": "error"}, status_code=503)
        return {"status": "ok", "version": __version__}

    app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")
    app.include_router(admin_router)
    return app
