"""Admin UI: import assistant (dry run, confirmation, background import)."""

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from . import episodes, importer
from .web import Session, db, redirect, render, require_session, protected

DEFAULT_CONFIG = "/import/podcaster/config.yml"
DEFAULT_DOWNLOADS = "/import/podcaster/data/downloads"


def _blocked(request: Request) -> bool:
    with db(request) as conn:
        return bool(episodes.count_all(conn)) and not request.app.state.import_job.running


def _page(request: Request, session: Session, *, status_code: int = 200, **ctx):
    job: importer.ImportJob = request.app.state.import_job
    with db(request) as conn:
        has_episodes = bool(episodes.count_all(conn))
    ctx.setdefault("config_file", DEFAULT_CONFIG)
    ctx.setdefault("downloads_dir", DEFAULT_DOWNLOADS)
    return render(request, "import.html", session, status_code=status_code, active="settings",
                  title="Import assistant", job=job, has_episodes=has_episodes,
                  import_root=request.app.state.config.import_dir, **ctx)


@protected.get("/settings/import", response_class=HTMLResponse, include_in_schema=False)
def import_form(request: Request, session: Session = Depends(require_session)):
    return _page(request, session)


@protected.get("/settings/import/status", response_class=HTMLResponse, include_in_schema=False)
def import_status(request: Request, session: Session = Depends(require_session)):
    return render(request, "_import_status.html", session, job=request.app.state.import_job)


@protected.post("/settings/import/preview", response_class=HTMLResponse, include_in_schema=False)
async def import_preview(request: Request, session: Session = Depends(require_session)):
    form = await request.form()
    config_file, downloads = str(form.get("config_file") or ""), str(form.get("downloads_dir") or "")
    if _blocked(request):
        return _page(request, session, status_code=409, config_file=config_file, downloads_dir=downloads,
                     error="The import is only possible while there are no episodes.")
    try:
        with db(request) as conn:
            plan = importer.build_plan(config_file, downloads, request.app.state.config.import_dir, conn)
    except importer.ImportSourceError as exc:
        return _page(request, session, status_code=422, config_file=config_file, downloads_dir=downloads,
                     error=str(exc))
    return _page(request, session, config_file=config_file, downloads_dir=downloads, plan=plan)


@protected.post("/settings/import/run", response_class=HTMLResponse, include_in_schema=False)
async def import_run(request: Request, session: Session = Depends(require_session)):
    form = await request.form()
    config_file, downloads = str(form.get("config_file") or ""), str(form.get("downloads_dir") or "")
    if _blocked(request):
        return _page(request, session, status_code=409, config_file=config_file, downloads_dir=downloads,
                     error="The import is only possible while there are no episodes.")
    try:
        with db(request) as conn:
            plan = importer.build_plan(config_file, downloads, request.app.state.config.import_dir, conn)
    except importer.ImportSourceError as exc:
        return _page(request, session, status_code=422, config_file=config_file, downloads_dir=downloads,
                     error=str(exc))
    config = request.app.state.config
    started = request.app.state.import_job.start(plan, config.db_path, config.media_dir,
                                                 take_base_url=bool(form.get("take_base_url")))
    if not started:
        return _page(request, session, status_code=409, error="An import is already running.")
    return redirect("/settings/import")
