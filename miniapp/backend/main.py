"""
main.py — FastAPI entrypoint

Boots the Mini App backend. Responsibilities:
  * Serve the frontend static files from /static
  * Auto-mount every route file under app/routes/
  * Serve /index.html at "/" so Telegram opens the Mini App directly
  * Health check at /healthz for Render / UptimeRobot

The existing admin_bot.py can import this or run it as a sibling process.
See docs/INTEGRATION.md for the recommended start.sh recipe.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routes import mount_all

log = logging.getLogger("miniapp")
logging.basicConfig(level=os.environ.get("MINIAPP_LOG_LEVEL", "INFO").upper())

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Doujinshi Universe Mini App", version="0.1.0")

# CORS — Telegram Mini Apps run inside a webview; same-origin is enough, but
# leave this permissive so a dev browser (localhost) can also hit the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static assets — /static/{css|js|assets}/...
app.mount(
    "/static",
    StaticFiles(directory=str(FRONTEND_DIR), html=False),
    name="static",
)

# Mount every /api/* route file automatically.
mount_all(app)


@app.get("/", include_in_schema=False)
def root_index() -> FileResponse:
    """Serve index.html when Telegram opens the Mini App."""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Render + UptimeRobot health check.  Cheap: no DB call."""
    return {"ok": True, "service": "miniapp"}
