"""FastAPI application."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .config import Config
from .db import connect
from .migrations import migrate

log = logging.getLogger(__name__)


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(config.db_path, config.backup_dir)  # no-op if already done at startup
        log.info("PodcasterNG server %s started", __version__)
        yield

    app = FastAPI(title="PodcasterNG", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config

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

    return app
