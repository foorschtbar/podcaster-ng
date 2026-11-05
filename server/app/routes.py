"""Assembles the admin routes. Importing the page modules registers their routes."""

from fastapi import Depends

from . import admin, dashboard_ui, import_ui, podcasts_ui, settings_ui, tasks_ui, workers_ui  # noqa: F401
from .web import check_configured, protected, router

router.include_router(protected, dependencies=[Depends(check_configured)])
