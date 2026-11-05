"""Admin UI: episodes (list, details, hide / delete files / re-download) and the retention page."""

from pathlib import Path as FsPath

from fastapi import Depends, Form, Path, Request
from fastapi.responses import HTMLResponse

from . import episodes, podcasts, queue, retention, settings
from .web import Session, db, env_defaults, protected, redirect, render, require_session

PAGE_SIZE = 50
SLUG = Path(pattern=r"^[a-z0-9-]{1,64}$")
VIDEO = Path(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")
FLASH = {"hidden": "Episode hidden: it is gone from the feed, the files are kept.",
         "unhidden": "Episode is in the feed again.",
         "deleted": "The files were deleted. The episode stays known and will not be downloaded again.",
         "redownload": "The files were deleted and a new download is queued."}


def _not_found(request, session, back="/episodes"):
    return render(request, "not_found.html", session, status_code=404, active="episodes", title="Not found",
                  back=back, back_label="Back to episodes")


@protected.get("/episodes", response_class=HTMLResponse, include_in_schema=False)
def episode_list(request: Request, podcast: str | None = None, state: str | None = None, page: int = 1,
                 session: Session = Depends(require_session)):
    podcast = podcast if podcast and podcasts.SLUG_RE.match(podcast) else None
    state = state if state in episodes.STATES else None
    page = max(1, min(page, 10_000))
    with db(request) as conn:
        total = episodes.count_filtered(conn, podcast, state)
        rows = episodes.list_filtered(conn, podcast, state, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        totals = episodes.totals(conn)
    query = "&".join(f"{k}={v}" for k, v in (("podcast", podcast), ("state", state)) if v)
    return render(request, "episodes.html", session, active="episodes", title="Episodes", rows=rows, total=total,
                  page=page, page_count=max(1, -(-total // PAGE_SIZE)), podcast=podcast, state=state, query=query,
                  states=episodes.STATES, totals=totals)


def _files(episode, media_dir: FsPath) -> list[dict]:
    out = []
    for column in episodes.FILE_COLUMNS:
        name = episode[column]
        if not name:
            continue
        path = media_dir / episode["podcast_id"] / name
        try:
            size = path.stat().st_size if path.is_file() and not path.is_symlink() else None
        except OSError:
            size = None
        out.append({"role": column.removesuffix("_file"), "name": name, "size": size})
    return out


def _detail(request, session, podcast_id, video_id, *, status_code=200, error=None, msg=None):
    with db(request) as conn:
        ep = episodes.get(conn, podcast_id, video_id)
        podcast = podcasts.get(conn, podcast_id)
        task = conn.execute("SELECT id, state FROM tasks WHERE type = 'download' AND podcast_id = ? AND video_id = ?",
                            (podcast_id, video_id)).fetchone()
        log = [r for r in retention.recent_log(conn, podcast_id, 200) if r["video_id"] == video_id][:5] if ep else []
    if not ep:
        return _not_found(request, session)
    return render(request, "episode_detail.html", session, status_code=status_code, active="episodes",
                  title=ep["title"] or video_id, e=ep, podcast=podcast, files=_files(ep, request.app.state.config.media_dir),
                  task=task, log=log, error=error, flash=FLASH.get(msg or ""), reasons=retention.REASONS)


@protected.get("/episodes/{podcast_id}/{video_id}", response_class=HTMLResponse, include_in_schema=False)
def episode_detail(request: Request, podcast_id: str = SLUG, video_id: str = VIDEO, msg: str | None = None,
                   session: Session = Depends(require_session)):
    return _detail(request, session, podcast_id, video_id, msg=msg)


def _hide(request, session, podcast_id, video_id, hidden):
    with db(request) as conn:
        changed = episodes.set_hidden(conn, podcast_id, video_id, hidden)
        known = episodes.get(conn, podcast_id, video_id)
    if not known:
        return _not_found(request, session)
    if not changed:
        return _detail(request, session, podcast_id, video_id, status_code=409,
                       error="Not possible: only an episode in the feed can be hidden, and only a hidden one shown again.")
    return redirect(f"/episodes/{podcast_id}/{video_id}?msg={'hidden' if hidden else 'unhidden'}")


@protected.post("/episodes/{podcast_id}/{video_id}/hide", include_in_schema=False)
def episode_hide(request: Request, podcast_id: str = SLUG, video_id: str = VIDEO, session: Session = Depends(require_session)):
    return _hide(request, session, podcast_id, video_id, True)


@protected.post("/episodes/{podcast_id}/{video_id}/unhide", include_in_schema=False)
def episode_unhide(request: Request, podcast_id: str = SLUG, video_id: str = VIDEO, session: Session = Depends(require_session)):
    return _hide(request, session, podcast_id, video_id, False)


@protected.get("/episodes/{podcast_id}/{video_id}/confirm", response_class=HTMLResponse, include_in_schema=False)
def episode_confirm(request: Request, podcast_id: str = SLUG, video_id: str = VIDEO, action: str = "delete",
                    session: Session = Depends(require_session)):
    if action not in ("delete", "redownload"):
        action = "delete"
    with db(request) as conn:
        ep = episodes.get(conn, podcast_id, video_id)
    if not ep:
        return _not_found(request, session)
    return render(request, "episode_confirm.html", session, active="episodes", title="Confirm", e=ep, action=action,
                  files=_files(ep, request.app.state.config.media_dir))


@protected.post("/episodes/{podcast_id}/{video_id}/delete", include_in_schema=False)
def episode_delete(request: Request, podcast_id: str = SLUG, video_id: str = VIDEO, session: Session = Depends(require_session)):
    with db(request) as conn:
        if not episodes.get(conn, podcast_id, video_id):
            return _not_found(request, session)
        result = retention.delete_files(conn, request.app.state.config.media_dir, podcast_id, video_id, "manual")
    if result is None:
        return _detail(request, session, podcast_id, video_id, status_code=409, error="Not possible: the files are already deleted.")
    return redirect(f"/episodes/{podcast_id}/{video_id}?msg=deleted")


@protected.post("/episodes/{podcast_id}/{video_id}/redownload", include_in_schema=False)
def episode_redownload(request: Request, podcast_id: str = SLUG, video_id: str = VIDEO, session: Session = Depends(require_session)):
    try:
        with db(request) as conn:
            task_id = retention.redownload(conn, request.app.state.config.media_dir, podcast_id, video_id)
    except queue.BadState as exc:
        if str(exc) == "no such episode":
            return _not_found(request, session)
        return _detail(request, session, podcast_id, video_id, status_code=409, error=f"Not possible: {exc}")
    return redirect(f"/tasks/{task_id}")


# --- retention -------------------------------------------------------------------------------------

def _retention_page(request, session, podcast_id, *, flash=None, status_code=200):
    with db(request) as conn:
        values = settings.get_all(conn, env_defaults(request))
        plans = retention.plan(conn, values, podcast_id)
        log = retention.recent_log(conn, podcast_id, 50)
    return render(request, "retention.html", session, active="episodes", title="Retention", plans=plans, log=log, v=values,
                  podcast=podcast_id, reasons=retention.REASONS, flash=flash, status_code=status_code,
                  to_delete=sum(len(p.candidates) for p in plans), to_free=sum(p.bytes for p in plans))


@protected.get("/retention", response_class=HTMLResponse, include_in_schema=False)
def retention_page(request: Request, podcast: str | None = None, session: Session = Depends(require_session)):
    podcast = podcast if podcast and podcasts.SLUG_RE.match(podcast) else None
    return _retention_page(request, session, podcast)


@protected.post("/retention/run", response_class=HTMLResponse, include_in_schema=False)
def retention_run(request: Request, podcast: str = Form(""), session: Session = Depends(require_session)):
    podcast = podcast if podcast and podcasts.SLUG_RE.match(podcast) else None
    with db(request) as conn:
        values = settings.get_all(conn, env_defaults(request))
        result = retention.run(conn, request.app.state.config.media_dir, values, podcast)
    from .web import human_size
    text = f"Deleted the files of {result['deleted']} episode(s), freed {human_size(result['bytes'])}."
    if result["capped"]:
        text += f" {result['capped']} more match the rules but are over the limit of {values['retention_max_per_run']} per run."
    return _retention_page(request, session, podcast, flash=text)
