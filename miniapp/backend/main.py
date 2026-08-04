"""
main.py — FastAPI entrypoint

Boots the Mini App backend. Responsibilities:
  * Serve the frontend static files from /static
  * Auto-mount every route file under app/routes/
  * Serve index.html at "/" so Telegram opens the Mini App directly
  * SPA fallback: any non-API GET returns index.html (hash routing safe)
  * Health check at /healthz for Render / UptimeRobot
  * No-cache headers on index.html so the shim upgrade lands immediately

v0.3 security change:
  CORS is no longer allow_origins=["*"] with allow_methods/headers=["*"].
  Origins come from MINIAPP_ALLOWED_ORIGINS plus a *.telegram.org regex;
  methods and headers are narrowed to what the app actually sends.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes import mount_all

log = logging.getLogger("miniapp")
logging.basicConfig(level=os.environ.get("MINIAPP_LOG_LEVEL", "INFO").upper())

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"

if not FRONTEND_DIR.is_dir():
    log.error(
        "FRONTEND_DIR does not exist: %s — Mini App will 404 on every request.",
        FRONTEND_DIR,
    )
elif not INDEX_HTML.is_file():
    log.error(
        "index.html not found at %s — Mini App will 404. Check your git commit.",
        INDEX_HTML,
    )
else:
    log.info("Serving frontend from %s", FRONTEND_DIR)

# Loud boot-time warnings for the two config mistakes that matter most.
if not settings.bot_token and not settings.dev_bypass_enabled:
    log.error("BOT_TOKEN is empty and dev mode is off — all API calls will 503.")
if settings.dev_bypass_enabled:
    log.warning(
        "DEV AUTH BYPASS IS ACTIVE (MINIAPP_DEV_MODE=1, no BOT_TOKEN). "
        "Never run this configuration on a public host."
    )
if not settings.admin_user_ids:
    log.warning("No ADMIN_USER_IDS configured — every /api/admin/* call will 403.")

app = FastAPI(title="Doujinshi Universe Mini App", version="0.3.0")

# ---------------------------------------------------------------------------
# CORS — Telegram Mini Apps run same-origin, so this only matters for browser
# testing and any external front-end you deliberately allow.
# ---------------------------------------------------------------------------
_cors_kwargs = {
    "allow_origins": settings.allowed_origins,
    "allow_origin_regex": r"^https://([a-z0-9-]+\.)*telegram\.org$",
    "allow_methods": ["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    "allow_headers": ["Content-Type", "X-Telegram-Init-Data"],
    "max_age": 600,
}
app.add_middleware(CORSMiddleware, **_cors_kwargs)
log.info("CORS allow-list: %s (+ *.telegram.org)", settings.allowed_origins or "[]")

# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------
if FRONTEND_DIR.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=str(FRONTEND_DIR), html=False),
        name="static",
    )

# Mount every /api/* route file automatically.
mount_all(app)


# ---------------------------------------------------------------------------
# HTML shell — served at "/" and also as an SPA fallback for unknown paths
# ---------------------------------------------------------------------------
def _serve_index() -> Response:
    """Serve index.html with no-cache headers so shim upgrades land instantly."""
    if not INDEX_HTML.is_file():
        return JSONResponse(
            status_code=500,
            content={
                "error": "index.html missing",
                "expected_at": str(INDEX_HTML),
                "hint": "Ensure the miniapp/frontend/ folder was committed to Git.",
            },
        )
    return FileResponse(
        str(INDEX_HTML),
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
        },
    )


@app.get("/", include_in_schema=False)
def root_index() -> Response:
    return _serve_index()


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Render + UptimeRobot health check.  Cheap: no DB call."""
    return {
        "ok": True,
        "service": "miniapp",
        "frontend": FRONTEND_DIR.is_dir(),
        "auth_configured": bool(settings.bot_token),
        "admins": len(settings.admin_user_ids),
    }


# ---------------------------------------------------------------------------
# SPA catch-all — MUST be registered LAST so /api/* and /static/* win.
# ---------------------------------------------------------------------------
@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str, request: Request) -> Response:
    if full_path.startswith(("api/", "static/", "healthz")):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return _serve_index()
