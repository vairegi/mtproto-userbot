"""
health_routes.py — GET /api/health/routes  (v12.52)
Lists every mounted /api route so you can verify from the phone/browser
that all route modules survived boot. If this returns only a handful of
paths, a route module failed to import — check the boot log for
'Failed to import route module'.
"""
from __future__ import annotations
from fastapi import APIRouter, Request
router = APIRouter(prefix="/api/health", tags=["health"])
@router.get("/routes")
def list_routes(request: Request) -> dict:
    paths = sorted(
        {
            getattr(r, "path", "")
            for r in request.app.routes
            if getattr(r, "path", "").startswith("/api")
        }
    )
    # Expected modules — mirror of the files in app/routes/ (minus _-prefixed)
    expected = {
        "/api/search", "/api/recommendations", "/api/trending",
        "/api/gallery", "/api/profile", "/api/bookmarks", "/api/queue",
    }
    missing = sorted(p for p in expected if not any(x.startswith(p) for x in paths))
    return {
        "ok": len(missing) == 0,
        "mounted_api_paths": paths,
        "count": len(paths),
        "missing_expected": missing,
    }
