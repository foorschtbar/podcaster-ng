"""Admin UI: dashboard (storage and hit statistics)."""

from datetime import timedelta

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from . import charts, queue, settings, stats
from .workers_ui import worker_cards
from .web import Session, db, env_defaults, protected, render, require_session, templates

DAYS = 30

templates.env.globals["rank_bar"] = charts.rank_bar
templates.env.globals["meter"] = charts.meter


def dashboard_context(conn, data_dir, env_defaults: dict | None = None) -> dict:
    """Everything the dashboard shows. Cards of later steps (workers, queue, errors) are added here."""
    today = stats.today()
    series = stats.daily_series(conn, DAYS, today)
    days = [row["day"] for row in series]
    feeds = [row["feed"] for row in series]
    files = [row["file"] for row in series]
    peak = max(series, key=lambda r: r["file"])
    storage = stats.storage_overview(conn, data_dir)
    worker_list = worker_cards(conn, settings.get_all(conn, env_defaults))
    top = stats.top_episodes(conn, DAYS, 10, today)
    return {
        "days": DAYS,
        "series": series,
        "totals": {"feed": sum(feeds), "file": sum(files), "bytes": sum(r["bytes"] for r in series)},
        "files_chart": charts.column_chart(days, files, label=f"Downloads per day, last {DAYS} days", unit="downloads"),
        "feeds_chart": charts.column_chart(days, feeds, label=f"Feed requests per day, last {DAYS} days", unit="feed requests"),
        "peak": peak if peak["file"] else None,
        "top_episodes": top,
        "top_max": top[0]["count"] if top else 1,
        "apps_files": _shares(stats.app_shares(conn, "file", DAYS, today)),
        "apps_feeds": _shares(stats.app_shares(conn, "feed", DAYS, today)),
        "queue": queue.counts(conn),
        "recent_errors": queue.recent_errors(conn, 5),
        "workers": worker_list,
        "workers_online": sum(1 for w in worker_list if w["status"] == "online"),
        "storage": storage,
        "storage_max": max([p["size"] for p in storage["podcasts"]], default=0) or 1,
        "disk_used_fraction": (storage["disk"]["used"] / storage["disk"]["total"]) if storage["disk"] else 0,
        "since": today - timedelta(days=DAYS - 1),
    }


def _shares(rows: list[tuple[str, int]]) -> dict:
    total = sum(n for _, n in rows)
    return {"rows": [(g, n, n / total) for g, n in rows], "total": total, "max": rows[0][1] if rows else 1}


@protected.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, session: Session = Depends(require_session)):
    with db(request) as conn:
        ctx = dashboard_context(conn, request.app.state.config.data_dir, env_defaults(request))
    return render(request, "dashboard.html", session, active="dashboard", title="Dashboard", **ctx)
