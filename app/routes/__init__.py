"""Route auto-mounter. Import each route module and expose its router
so main.py can include them without hard-coding names."""
from __future__ import annotations

from fastapi import FastAPI

from . import health, status as status_route, admin, telegram


def mount_all(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(status_route.router)
    app.include_router(admin.router)
    app.include_router(telegram.router)
