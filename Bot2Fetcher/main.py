"""
main.py — Bot2Fetcher entrypoint (Render web service).
"""
from __future__ import annotations

import asyncio
import os as _os
import logging
import sys
import threading

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from app.config import load
from app.mongo_state import connect as mongo_connect, Galleries, PeerCache, FallbackLease
from app.turso_store import Turso
from app.fetcher import Fetcher, Stats
from app.dashboard import Dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
# HTTP INFO logs include token-bearing Bot API URLs. Never publish them.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
class _RedactBotToken(logging.Filter):
    def filter(self, record):
        import re
        record.msg = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", record.getMessage())
        record.args = ()
        return True
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RedactBotToken())
log = logging.getLogger("bot2fetcher")

settings = load()
try:  # v12.83: RAM visibility in Render logs from first boot
    import psutil as _ps
    log.info("🧠 boot RAM: %.0f MB RSS (Render free tier: 512 MB)",
             _ps.Process().memory_info().rss / (1024 * 1024))
except Exception:
    log.info("🧠 boot RAM: psutil unavailable")
stats = Stats()
_db = mongo_connect(settings.mongo_uri, settings.mongo_db)
galleries = Galleries(_db, settings.stale_processing_s)
turso = Turso(settings.turso_url, settings.turso_token)
dashboard = Dashboard(settings, turso, stats)
dashboard.galleries = galleries
# v13.12: Mongo-backed log channel (/addlog) wins over LOG_CHANNEL_ID env.
from app import workers as _workers_mod
dashboard._config_provider = lambda: _workers_mod.get_log_channel(_db)
fetcher = Fetcher(settings, galleries, turso, stats, dashboard=dashboard)
# v12.41: per-slot InputPeer cache (Mongo). Warm restarts skip the
# 200-dialog iter_dialogs scan; session rotation auto-invalidates via
# fingerprint mismatch. Falls back to live resolution on any miss.
fetcher.peer_cache = PeerCache(_db)
# v12.49: Layer B single-flight lease for the fallback downloader bot
# (covers the multi-instance / restart window; Layer A lock lives in fetcher).
fetcher.fallback_lease = FallbackLease(_db, settings.fallback_lease_s)
# v13.10: Mongo-backed workers + admin command listener (/addworker,
# /listworker, /removeworker, /changeapiandhash on BOT2_TOKEN).
fetcher.workers_db = _db
from app.admin_bot import WorkerAdminBot
_admin_bot = WorkerAdminBot(_db, notify_reload=fetcher.request_reconcile,
                            status_provider=fetcher.worker_status)

app = FastAPI(title="Bot2Fetcher")


def _health():
    return {"ok": True, "service": "bot2fetcher", "slots": len(fetcher.clients)}


@app.get("/healthz")
def healthz():
    return _health()


# v13.13: UptimeRobot default monitors send HEAD; without these a HEAD probe
# gets 405 and Render marks the free service idle -> it spins down.
@app.head("/healthz")
def healthz_head():
    return JSONResponse({})


@app.get("/ping")
def ping():
    return _health()


@app.head("/ping")
def ping_head():
    return JSONResponse({})


@app.get("/status")
def status():
    return JSONResponse(stats.snapshot())


@app.head("/")
def index_head():
    return JSONResponse({})


@app.get("/", response_class=HTMLResponse)
def index():
    s = stats.snapshot()
    rows = "".join(
        f"<tr><td>{k}</td><td><b>{v}</b></td></tr>"
        for k, v in s.items() if k != "in_flight"
    )
    flight = ", ".join(s["in_flight"].keys()) or "—"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="15"><title>Bot2Fetcher</title>
<style>body{{font-family:system-ui;background:#0f1115;color:#e8eaf0;max-width:560px;margin:40px auto;padding:0 16px}}
h1{{font-size:20px}}table{{width:100%;border-collapse:collapse}}td{{padding:6px 4px;border-bottom:1px solid #262b36}}</style>
</head><body><h1>🤖 Bot2Fetcher — live</h1>
<table>{rows}</table><p>📥 in flight: {flight}</p>
<p style="opacity:.6">auto-refresh 15s · JSON at /status</p></body></html>"""


def _run_api() -> None:
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="warning")


async def _main() -> None:
    log.info("🚀 Bot2Fetcher v13.13 booting (Mongo-backed workers; "
             "standby until /addworker)")
    if _os.environ.get("BOT2_TURSO_OFF", "0").strip() in ("1", "true", "yes"):
        log.warning("🚫 [TURSO OFF] Bot2Fetcher running MONGO-ONLY "
                    "(producer scans + gallery rows + state -> Mongo; "
                    "nightly IST sync owns Turso)")
    fetcher._main_loop = asyncio.get_running_loop()
    _admin_bot.start()
    await turso.ensure_schema()
    dash_task = asyncio.create_task(dashboard.run(fetcher))
    try:
        await fetcher.run()
    finally:
        dash_task.cancel()
        await fetcher.stop()


if __name__ == "__main__":
    t = threading.Thread(target=_run_api, daemon=True)
    t.start()
    log.info("🌐 web status on :%d — starting fetcher", settings.port)
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
