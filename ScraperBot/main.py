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
from app.services import turso_schema
from app.services import list_sweeper, details_sweeper, channel_dashboard
from app.services import discovery_digest  # v1.25: daily 10:00 IST admin digest
from app.services import turso_backup  # v1.28: Turso -> 2nd Mongo backup

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

    # v1.22.4: Mongo index creation + Turso bootstrap can block for tens of
    # seconds on a cold Atlas connection. They used to run inline here,
    # stalling the event loop so Render's 5s /healthz probe timed out mid-boot
    # (the "Deploy failed — timed out waiting for internal health check" and
    # "Instance failed: HTTP health check timed out" events). Now they run in
    # the background AFTER uvicorn is already serving — /healthz answers from
    # the first second and Render never marks a boot as failed.
    _tasks.append(asyncio.create_task(_bg_db_warmup(), name="db_warmup"))

    # v1.22.8: RAM watchdog — logs VmRSS every 60s so we can SEE the 512MB
    # ceiling being approached before Render SIGKILLs (status 137) instead
    # of finding out from the events page afterwards. ~0 cost.
    _tasks.append(asyncio.create_task(_ram_watchdog(), name="ram_watchdog"))

    global _stop_event
    _stop_event = asyncio.Event()

    # v1.22.2: webhook keeper — auto-register the Telegram webhook from
    # BOT1_PUBLIC_BASE_URL at boot, then re-verify every 6h. Runs even in
    # degraded mode so /status /health /pause etc. always work.
    if settings.public_base_url and settings.bot_token:
        _tasks.append(asyncio.create_task(
            _webhook_keeper(), name="webhook_keeper"))
        log.info("webhook keeper armed for %s", settings.public_base_url)
    elif settings.bot_token:
        log.warning("BOT1_PUBLIC_BASE_URL unset — webhook NOT auto-managed; "
                    "set it to this service's public URL to stop manual "
                    "setWebhook steps.")

    # Spawn sweepers — fail-open: any startup exception here just gets
    # logged; the HTTP surface must stay up so UptimeRobot / admin routes
    # keep working even if a sweeper is misconfigured.
    if settings.mongo_uri and settings.turso_url:
        # v1.22.8: staggered starts — the old code spawned all three at the
        # same instant as db warmup, so the boot memory peak (Mongo index
        # build + Turso bootstrap + first sweep page + dashboard) landed in
        # the same seconds and tripped the 512MB ceiling (status 137 kills
        # in the Render events). Now each component starts well after the
        # previous one has settled.
        _tasks.append(asyncio.create_task(
            _delayed(list_sweeper.run_forever, 30, _stop_event),
            name="list_sweeper"))
        _tasks.append(asyncio.create_task(
            _delayed(details_sweeper.run_forever, 60, _stop_event),
            name="details_sweeper"))
        _tasks.append(asyncio.create_task(
            _delayed(channel_dashboard.refresh_loop, 45, _stop_event),
            name="dashboard"))
        # v1.25: daily admin digest (10:00 IST) — per sort/tag per page
        # new-item counts. Zero scraping cost; observability only.
        _tasks.append(asyncio.create_task(
            _delayed(discovery_digest.run_forever, 50, _stop_event),
            name="discovery_digest"))
        # v1.28: Turso -> second-Mongo backup (every 12h; idles w/o URI)
        _tasks.append(asyncio.create_task(
            _delayed(turso_backup.run_forever, 120, _stop_event),
            name="turso_backup"))
        log.info("sweepers + dashboard spawned: %s",
                 [t.get_name() for t in _tasks])
    else:
        log.error("Sweepers NOT started — missing MONGO_URI or TURSO_DATABASE_URL")


async def _delayed(fn, delay_s: int, *args) -> None:
    """v1.22.8: run fn(*args) after delay_s so boot memory peaks stagger."""
    await asyncio.sleep(delay_s)
    log.info("staggered start: %s (after %ds)", fn.__qualname__, delay_s)
    await fn(*args)


async def _bg_db_warmup() -> None:
    """v1.22.4/1.22.8: non-blocking Mongo index ensure + Turso bootstrap,
    split into two stages so the two cold-connection peaks never overlap."""
    await asyncio.sleep(5)  # let uvicorn bind + first health checks pass
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, mongo_client.cache_ensure_indexes)
        log.info("db warmup stage 1/2 done (mongo indexes)")
    except Exception as e:  # noqa: BLE001
        log.warning("cache_ensure_indexes failed: %s", e)
    await asyncio.sleep(20)  # v1.22.8: let the Mongo peak subside first
    try:
        await turso_client.bootstrap_schema()
        try:
            await turso_schema.ensure_schema()
        except Exception as e:
            log.warning("turso schema migration failed (non-fatal): %s", e)
    except Exception as e:  # noqa: BLE001
        log.warning("turso bootstrap failed: %s", e)
    log.info("db warmup finished (mongo indexes + turso schema)")


async def _ram_watchdog() -> None:
    """v1.22.8: log VmRSS every 60s. Thresholds: warn at 380MB, critical at
    450MB (Render free ceiling is 512MB; status 137 arrives without warning
    otherwise). Same numbers feed the /checkram bot command."""
    while True:
        try:
            mb = 0.0
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        mb = int(line.split()[1]) / 1024.0
                        break
            if mb >= 450:
                log.warning("🧠 RAM CRITICAL: %.0f MB / 512 MB", mb)
            elif mb >= 380:
                log.info("🧠 RAM high: %.0f MB / 512 MB", mb)
            else:
                log.info("🧠 RAM: %.0f MB / 512 MB", mb)
        except Exception as e:  # noqa: BLE001
            log.info("ram watchdog read failed: %s", e)
        await asyncio.sleep(60)


async def _webhook_keeper() -> None:
    """v1.22.2: point Telegram's webhook at BOT1_PUBLIC_BASE_URL and keep
    it there. set_webhook() builds `<url>/telegram?s=<BOT1_WEBHOOK_SECRET>`
    from the env vars themselves, so the path/secret can never drift from
    what the /telegram route checks (the v1.22.1 OCR 0-vs-O incident).
    """
    from app.services import telegram_bot
    await asyncio.sleep(5)  # let uvicorn bind the port first
    while True:
        try:
            info = await telegram_bot._api("getWebhookInfo", {})
            cur = ((info.get("result") or {}).get("url") or "")
            want = f"{settings.public_base_url.rstrip('/')}/telegram"
            if not cur.startswith(want):
                r = await telegram_bot.set_webhook(settings.public_base_url)
                if r.get("ok"):
                    log.info("\U0001f517 webhook auto-registered \u2192 %s?s=\u2022\u2022\u2022",
                             want)
                else:
                    log.warning("webhook auto-register failed: %s",
                                str(r)[:300])
        except Exception as e:  # noqa: BLE001
            log.warning("webhook keeper tick failed (retry in 6h): %s", e)
        await asyncio.sleep(6 * 3600)


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
