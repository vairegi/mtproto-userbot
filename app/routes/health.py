"""health.py — /, /healthz — cheap probes for UptimeRobot + Render."""
from __future__ import annotations

import time

from fastapi import APIRouter

from ..config import settings

router = APIRouter()

_STARTED = time.time()


@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def root() -> dict:
    # HEAD is included so UptimeRobot's default HEAD probe stops logging
    # 405 Method Not Allowed on every ping.
    from ..services import list_sweeper, details_sweeper
    return {
        "service": "ScraperBot (BOT 1)",
        "ok": True,
        "uptime_sec": int(time.time() - _STARTED),
        "scraper_enabled": settings.scraper_enabled,
        "list_last_run":    list_sweeper.status().get("last_run", 0),
        "details_last_run": details_sweeper.status().get("last_run", 0),
    }


@router.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
def healthz() -> dict:
    return {"ok": True, "uptime_sec": int(time.time() - _STARTED)}
