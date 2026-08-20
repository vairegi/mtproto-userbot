"""
main.py — FastAPI entrypoint for ScraperBot (BOT 1).

Boot sequence:
  1. Load env, validate required vars (soft-fail with LOUD log if missing).
  2. Connect Mongo (lazy), bootstrap Turso schema.
  3. Mount routes.
  4. Kick off list_sweeper + details_sweeper as background tasks.

Shutdown: set the shared stop-event; sweepers cooperate and exit.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.logging_setup import setup_logging
from app.routes import mount_all
from app import mongo_client, turso_client
from app.services import list_sweeper, details_sweeper, channel_dashboard

log = setup_logging("scraperbot")

app = FastAPI(title="ScraperBot (BOT 1)", version="1.19.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

mount_all(app)

_stop_event: asyncio.Event | None = None
_tasks: list[asyncio.Task] = []


@app.on_event("startup")
async def _startup() -> None:
    errs = settings.validate()
    if errs:
        for e in errs:
            log.error("CONFIG ERROR: %s", e)
        log.error("Continuing in DEGRADED mode — /healthz still works, "
                  "sweepers will idle until env is fixed.")

    log.info("ScraperBot boot: mongo=%s turso_url=%s",
             bool(settings.mongo_uri),
             "yes" if settings.turso_url else "no")

    # Warm up Mongo (lazy connect + indexes).
    try:
        mongo_client.cache_ensure_indexes()
    except Exception as e:  # noqa: BLE001
        log.warning("cache_ensure_indexes failed: %s", e)

    # Bootstrap Turso schema if reachable.
    try:
        await turso_client.bootstrap_schema()
    except Exception as e:  # noqa: BLE001
        log.warning("turso bootstrap failed: %s", e)

    global _stop_event
    _stop_event = asyncio.Event()

    # Spawn sweepers — fail-open: any startup exception here just gets
    # logged; the HTTP surface must stay up so UptimeRobot / admin routes
    # keep working even if a sweeper is misconfigured.
    if settings.mongo_uri and settings.turso_url:
        _tasks.append(asyncio.create_task(
            list_sweeper.run_forever(_stop_event), name="list_sweeper"))
        _tasks.append(asyncio.create_task(
            details_sweeper.run_forever(_stop_event), name="details_sweeper"))
        # v1.5: live channel dashboard — edits the pinned pair of messages
        # every channel_refresh_sec (default 5s, overridable with /time <n>).
        _tasks.append(asyncio.create_task(
            channel_dashboard.refresh_loop(_stop_event), name="dashboard"))
        log.info("sweepers + dashboard spawned: %s",
                 [t.get_name() for t in _tasks])
    else:
        log.error("Sweepers NOT started — missing MONGO_URI or TURSO_DATABASE_URL")


@app.on_event("shutdown")
async def _shutdown() -> None:
    log.info("ScraperBot shutting down…")
    if _stop_event is not None:
        _stop_event.set()
    # Give sweepers a moment to notice.
    for t in _tasks:
        try:
            await asyncio.wait_for(t, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            t.cancel()
        except Exception as e:  # noqa: BLE001
            log.warning("sweeper %s exit error: %s", t.get_name(), e)
    try:
        await turso_client.close()
    except Exception:  # noqa: BLE001
        pass
    log.info("bye.")
