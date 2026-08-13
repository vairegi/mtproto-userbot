"""status.py — /status: detailed sweep + connectivity report."""
from __future__ import annotations

import time

from fastapi import APIRouter

from .. import mongo_client, turso_client
from ..config import settings
from ..services import list_sweeper, details_sweeper

router = APIRouter()


@router.get("/status")
async def status() -> dict:
    mongo_ok = mongo_client.db() is not None
    turso_ok = turso_client.turso_available()

    return {
        "service": "ScraperBot",
        "now": int(time.time()),
        "connectivity": {
            "mongo":  mongo_ok,
            "turso":  turso_ok,
        },
        "config": {
            "scraper_enabled": settings.scraper_enabled,
            "list_sorts":      settings.list_sorts,
            "list_max_pages":  settings.list_max_pages,
            "list_tick_sec":   settings.list_tick_sec,
            "details_tick_sec": settings.details_tick_sec,
            "details_per_tick": settings.details_per_tick,
            "details_page_cap": settings.details_page_cap,
        },
        "list_sweeper":    list_sweeper.status(),
        "details_sweeper": details_sweeper.status(),
    }
