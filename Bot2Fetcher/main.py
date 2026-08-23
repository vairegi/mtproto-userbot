"""
main.py — Bot2Fetcher entrypoint (Render web service).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from app.config import load
from app.mongo_state import connect as mongo_connect, Galleries
from app.turso_store import Turso
from app.fetcher import Fetcher, Stats
from app.dashboard import Dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bot2fetcher")

settings = load()
stats = Stats()
_db = mongo_connect(settings.mongo_uri, settings.mongo_db)
galleries = Galleries(_db, settings.stale_processing_s)
turso = Turso(settings.turso_url, settings.turso_token)
fetcher = Fetcher(settings, galleries, turso, stats)
dashboard = Dashboard(settings, turso, stats)

app = FastAPI(title="Bot2Fetcher")


@app.get("/healthz")
def healthz():
    return {"ok": True, "slots": len(settings.sessions)}


@app.get("/status")
def status():
    return JSONResponse(stats.snapshot())


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
    log.info("web status on :%d — starting fetcher (%d slot(s))",
             settings.port, len(settings.sessions))
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
