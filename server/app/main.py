"""FastAPI application."""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import LoginLimiter, RateLimiter, load_session_secret
from .config import Config
from .importer import ImportJob
from .db import connect
from .middleware import HitCounter, SecurityHeaders
from .migrations import migrate
from .public import router as public_router
from .routes import router as admin_router
from .scheduler import scheduler_loop
from .stats import prune_loop
from .worker_api import router as worker_router
from .web import CsrfError, LoginRequired, NotConfigured, locked_response

log = logging.getLogger(__name__)


def create_app(config: Config | None = None, limiter: LoginLimiter | None = None) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(config.db_path, config.backup_dir)  # no-op if already done at startup
        app.state.secret = load_session_secret(config)
        if not config.admin_configured:
            log.warning("ADMIN_USER/ADMIN_PASSWORD not set: admin UI is locked")
        log.info("PodcasterNG server %s started", __version__)
        background = [asyncio.create_task(prune_loop(config.db_path))]
        if config.run_scheduler:
            background.append(asyncio.create_task(scheduler_loop(config.db_path, config.scheduler_tick_seconds)))
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            for task in background:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="PodcasterNG", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.limiter = limiter or LoginLimiter()
    app.state.import_job = ImportJob()
    # Invalid worker tokens: per client address. Valid workers: per worker.
    app.state.worker_auth_limiter = LoginLimiter(max_failures=10, window=300, lockout=300, delay=0)
    app.state.worker_rate_limiter = RateLimiter(limit=120, window=60)

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
    app.include_router(public_router)
    app.include_router(worker_router)
    app.include_router(admin_router)
    # Outermost last: the hit counter sees every request, the security headers every response.
    app.add_middleware(HitCounter, db_path=config.db_path)
    app.add_middleware(SecurityHeaders)
    return app
