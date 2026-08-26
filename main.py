"""
main.py — FastAPI entrypoint

Boots the Mini App backend. Responsibilities:
  * Serve the frontend static files from /static
  * Auto-mount every route file under app/routes/
  * Serve index.html at "/" so Telegram opens the Mini App directly
  * SPA fallback: any non-API GET returns index.html (hash routing safe)
  * Health check at /healthz for Render / UptimeRobot
  * No-cache headers on index.html so the shim upgrade lands immediately
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.routes import mount_all

log = logging.getLogger("miniapp")
logging.basicConfig(level=os.environ.get("MINIAPP_LOG_LEVEL", "INFO").upper())

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"

# Sanity-check at boot — if the frontend folder is missing, LOUD warning in
# logs. Prevents the "silent blank screen" mystery: you'll see it in Render's
# log stream immediately after deploy.
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

app = FastAPI(title="Doujinshi Universe Mini App", version="0.2.0")

# CORS — Telegram Mini Apps run same-origin; permissive here for local dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------
# Mounted at /static — matches every `/static/css/...` and `/static/js/...`
# reference in index.html and the importmap.
#
# v12.34j: no-cache middleware for /static. index.html already ships
# no-store (see _serve_index below), but the JS/CSS MODULES it imports
# were served with default headers — Telegram's WebView heuristic-caches
# those for days, so a deployed frontend change (v12.34i "Similar to
# this", v12.34h toast move, ...) never reached users until they
# hard-refreshed. This middleware stamps the same no-cache headers on
# every /static response so any deploy is live on the next app open.
@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path.startswith("/js/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

if FRONTEND_DIR.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=str(FRONTEND_DIR), html=False),
        name="static",
    )

# Mount every /api/* route file automatically.
mount_all(app)


# ---------------------------------------------------------------------------
# Startup: kick off the auto-delete background loop (feature 1)
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def _ensure_turso_schema_on_startup():
    """Run Turso schema migration at startup.

    v12.51: prefers the local `turso_schema` module when present (full schema),
    otherwise falls back to the shared HTTP client `ensure_schema()` so that the
    cache tables always exist — never raises, never blocks boot.
    """
    import logging
    lg = logging.getLogger("miniapp.main")
    # Try the full local schema module first
    try:
        from app.services import turso_schema  # type: ignore
        await turso_schema.ensure_schema()
        lg.info("turso schema migration OK (local turso_schema)")
        return
    except ImportError:
        pass  # fall through to shared-client fallback
    except Exception as e:  # noqa: BLE001
        lg.warning("turso_schema.ensure_schema failed, falling back: %s", e)
    # Fallback: shared HTTP client schema
    try:
        from app.services import turso_client
        ok = await turso_client.ensure_schema()
        if ok:
            lg.info("turso schema migration OK (turso_client fallback)")
        else:
            lg.warning("turso schema migration unavailable (turso_client returned False)")
    except Exception as e:  # noqa: BLE001
        lg.warning("turso schema migration failed (non-fatal): %s", e)

@app.on_event("startup")
async def _start_deletion_scheduler() -> None:
    """Start the background loop that deletes DM'd content after N hours.
    Idempotent — safe on every (re)boot."""
    try:
        from app.services import deletion_scheduler
        deletion_scheduler.start_background_loop()
        log.info("deletion_scheduler background loop started")
    except Exception as e:  # noqa: BLE001
        log.warning("deletion_scheduler failed to start (non-fatal): %s", e)


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
            # Telegram's WebView aggressively caches HTML. No-cache guarantees
            # the user sees the latest shell after every deploy — without this
            # they might keep the old broken importmap-only shell for days.
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/", include_in_schema=False)
def root_index() -> Response:
    return _serve_index()


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Render + UptimeRobot health check.  Cheap: no DB call."""
    return {"ok": True, "service": "miniapp", "frontend": FRONTEND_DIR.is_dir()}


# ---------------------------------------------------------------------------
# SPA catch-all — MUST be registered LAST so /api/* and /static/* win.
# ---------------------------------------------------------------------------
# The frontend uses hash routing (#search, #bookmarks, ...) so this is
# mostly future-proofing: any accidental deep-link like /profile or
# /admin gets the app shell instead of a 404. Anything under /api/* or
# /static/* is untouched because those routes were registered above and
# FastAPI matches in registration order.
@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str, request: Request) -> Response:
    # Don't shadow the API or static routes if someone hits a bad path.
    if full_path.startswith(("api/", "static/", "healthz")):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return _serve_index()
