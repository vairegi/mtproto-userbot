"""
admin.py — /api/admin/* endpoints (admin-only)

Every route here uses require_admin, which returns 403 for non-admin callers.
Frontend's admin.js panel is powered by these endpoints.

Endpoints:
  GET  /api/admin/visibility                        → { public_mode }
  POST /api/admin/visibility  { public_mode }       → set visibility
  GET  /api/admin/ratelimit/defaults                → { daily, cooldown_s }
  POST /api/admin/ratelimit/defaults  { daily, cooldown_s }
  GET  /api/admin/users                             → list users + usage
  POST /api/admin/users/{uid}/reset                 → reset today's usage
  POST /api/admin/users/{uid}/limit  { daily }      → override daily limit
  POST /api/admin/users/{uid}/ban                   → ban user
  POST /api/admin/users/{uid}/unban                 → unban user
  GET  /api/admin/diag                              → scraper + queue probe
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import db
from ..auth import require_admin
from ..services import (
    broadcast, deletion_scheduler, force_join, queue_bridge, rescrape,
    scraper_bridge, share_guard,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---- Visibility ----
class VisibilityBody(BaseModel):
    public_mode: bool


@router.get("/visibility")
def get_visibility(_a: dict = Depends(require_admin)) -> dict:
    return {"public_mode": db.get_public_mode()}


@router.post("/visibility")
def set_visibility(body: VisibilityBody, _a: dict = Depends(require_admin)) -> dict:
    db.set_setting("public_mode", bool(body.public_mode))
    return {"ok": True, "public_mode": bool(body.public_mode)}


# ---- Rate limit defaults ----
class RLDefaultsBody(BaseModel):
    daily: int = 20
    cooldown_s: int = 0


@router.get("/ratelimit/defaults")
def get_rl_defaults(_a: dict = Depends(require_admin)) -> dict:
    return {
        "daily":      db.get_default_daily(),
        "cooldown_s": db.get_default_cooldown(),
    }


@router.post("/ratelimit/defaults")
def set_rl_defaults(body: RLDefaultsBody, _a: dict = Depends(require_admin)) -> dict:
    db.set_setting("default_daily_limit", max(0, int(body.daily)))
    db.set_setting("default_cooldown_s",  max(0, int(body.cooldown_s)))
    return {"ok": True, **body.model_dump()}


# ---- Card-grid layout (v12.10 #6+#7, v12.11 #4) ----
# Admin-only toggles stored in the miniapp_settings singleton:
#   layout_cards_per_row : 1..4        (default 2)
#   layout_card_gap      : 0/0.1/0.25/0.5/1     (default 0)
#   layout_app_pad_x     : 0/4/8/12/16/20/24 px (default 0) — v12.11 (#4)
# The PUBLIC read endpoint lives in routes/layout.py (no auth — every
# mini-app client needs these to paint the grid); only the WRITE side
# is gated here behind require_admin.
_ALLOWED_CARDS_PER_ROW = (1, 2, 3, 4)
_ALLOWED_GAPS = (0, 0.1, 0.25, 0.5, 1)
# v12.11 (#4): horizontal page padding, in pixels. 0 = flush against the
# viewport edge (fixes the right-edge card clip reported in v12.10).
_ALLOWED_APP_PAD_X = (0, 4, 8, 12, 16, 20, 24)


class LayoutBody(BaseModel):
    cards_per_row: int = 2
    card_gap: float = 0
    # v12.11 (#4): admin-controlled left/right breathing room. Optional so
    # older clients that don't send it keep working.
    app_pad_x: int = 0


@router.get("/layout")
def get_layout_admin(_a: dict = Depends(require_admin)) -> dict:
    return {
        "cards_per_row": db.get_setting("layout_cards_per_row", 2),
        "card_gap":      db.get_setting("layout_card_gap", 0),
        "app_pad_x":     db.get_setting("layout_app_pad_x", 0),
    }


@router.post("/layout")
def set_layout(body: LayoutBody, _a: dict = Depends(require_admin)) -> dict:
    cpr = int(body.cards_per_row)
    if cpr not in _ALLOWED_CARDS_PER_ROW:
        cpr = 2
    gap = float(body.card_gap)
    # Snap to the nearest allowed step (floats make exact match fragile).
    gap = min(_ALLOWED_GAPS, key=lambda g: abs(g - gap))
    # v12.11 (#4): snap horizontal padding to the nearest allowed step.
    pad = int(body.app_pad_x or 0)
    pad = min(_ALLOWED_APP_PAD_X, key=lambda p: abs(p - pad))
    db.set_setting("layout_cards_per_row", cpr)
    db.set_setting("layout_card_gap", gap)
    db.set_setting("layout_app_pad_x", pad)
    return {"ok": True, "cards_per_row": cpr, "card_gap": gap, "app_pad_x": pad}


# ---- Users ----
@router.get("/users")
def list_users(_a: dict = Depends(require_admin)) -> dict:
    rows = db.list_users(limit=200)
    items = []
    for r in rows:
        uid = int(r.get("_id"))
        items.append({
            "user_id":    uid,
            "first_name": r.get("first_name"),
            "username":   r.get("username"),
            "photo_url":  r.get("photo_url"),
            "banned":     bool(r.get("banned", False)),
            "limit":      db.get_user_daily_limit(uid),
            "used_today": db.get_used_today(uid),
            "last_seen":  r.get("last_seen").isoformat() if r.get("last_seen") else None,
        })
    return {"items": items}


class UserLimitBody(BaseModel):
    daily: int


@router.post("/users/{uid}/reset")
def reset_user(uid: int, _a: dict = Depends(require_admin)) -> dict:
    db.reset_used_today(uid)
    return {"ok": True}


@router.post("/users/{uid}/limit")
def set_user_limit(uid: int, body: UserLimitBody, _a: dict = Depends(require_admin)) -> dict:
    db.set_user_daily_limit(uid, max(0, int(body.daily)))
    return {"ok": True, "daily": body.daily}


@router.post("/users/{uid}/ban")
def ban(uid: int, _a: dict = Depends(require_admin)) -> dict:
    db.set_banned(uid, True)
    return {"ok": True}


@router.post("/users/{uid}/unban")
def unban(uid: int, _a: dict = Depends(require_admin)) -> dict:
    db.set_banned(uid, False)
    return {"ok": True}


# ---- Feature 1: Auto-delete DM'd content ----
class AutoDeleteBody(BaseModel):
    enabled: bool = False
    hours: int = 24


@router.get("/autodelete")
def get_autodelete(_a: dict = Depends(require_admin)) -> dict:
    return {
        "enabled": deletion_scheduler.is_enabled(),
        "hours":   deletion_scheduler.hours(),
    }


@router.post("/autodelete")
def set_autodelete(body: AutoDeleteBody, _a: dict = Depends(require_admin)) -> dict:
    db.set_setting("auto_delete_enabled", bool(body.enabled))
    db.set_setting("auto_delete_hours",   max(1, int(body.hours)))
    return {"ok": True, "enabled": bool(body.enabled),
            "hours": max(1, int(body.hours))}


# ---- Feature 2: Disable sharing (protect_content on every delivery) ----
class ShareGuardBody(BaseModel):
    enabled: bool


@router.get("/shareguard")
def get_shareguard(_a: dict = Depends(require_admin)) -> dict:
    return {"enabled": share_guard.is_enabled()}


@router.post("/shareguard")
def set_shareguard(body: ShareGuardBody, _a: dict = Depends(require_admin)) -> dict:
    db.set_setting("share_disabled", bool(body.enabled))
    return {"ok": True, "enabled": bool(body.enabled)}


# ---- Feature 3: Force-join channels ----
class ForceJoinAddBody(BaseModel):
    channel: str
    title: str = ""
    url: str = ""


class ForceJoinRemoveBody(BaseModel):
    channel: str


@router.get("/forcejoin")
def get_forcejoin(_a: dict = Depends(require_admin)) -> dict:
    chans = force_join.get_channels()
    return {"enabled": bool(chans), "channels": chans}


@router.post("/forcejoin/add")
def forcejoin_add(body: ForceJoinAddBody, _a: dict = Depends(require_admin)) -> dict:
    return force_join.add_channel(body.channel, title=body.title, url=body.url)


@router.post("/forcejoin/remove")
def forcejoin_remove(body: ForceJoinRemoveBody, _a: dict = Depends(require_admin)) -> dict:
    return force_join.remove_channel(body.channel)


# ---- Feature 4: Force Re-scrape (admin escape hatch for failed / stuck galleries) ----
class RescrapeBody(BaseModel):
    url: str = ""
    gallery_id: str = ""


@router.get("/rescrape/failed")
def list_failed(limit: int = 50, _a: dict = Depends(require_admin)) -> dict:
    """Return recent failed / partial galleries so the admin can pick one
    to re-scrape. Each row includes the specific `failed_reason` field so
    the admin knows WHY it failed (e.g. 'scrape returned nothing')."""
    return {
        "items": rescrape.list_failed_galleries(limit=int(max(1, min(500, limit)))),
    }


@router.post("/rescrape")
def rescrape_one(body: RescrapeBody, a: dict = Depends(require_admin)) -> dict:
    """Force a fresh scrape for a URL or gallery id: purges the dedup
    row + any lingering queue rows, then re-enqueues via queue_service."""
    target = (body.gallery_id or body.url or "").strip()
    if not target:
        return {"ok": False, "reason": "missing url / gallery_id"}
    return rescrape.force_rescrape(
        target,
        submitted_by=int(a.get("id") or 0),
        username=a.get("username") or "admin",
    )


@router.get("/rescrape/diag")
def rescrape_diag(target: str, _a: dict = Depends(require_admin)) -> dict:
    """Non-destructive lookup: current galleries doc + lingering queue
    rows for a URL or gallery id. Useful for the admin to inspect a
    stuck job before deciding to re-scrape."""
    return rescrape.diagnose(target)


# v11.3 — Force-Delete: purge a gallery from MongoDB WITHOUT re-enqueue.
# Unlike /rescrape (which always re-queues), this just hard-deletes the
# galleries doc + queue rows + progress/metrics events so the next
# enqueue of the same URL is a completely fresh job. Admin-only.
@router.delete("/purge/{gallery_id}")
def purge_gallery_by_id(gallery_id: str, a: dict = Depends(require_admin)) -> dict:
    """Hard-delete all MongoDB state for a gallery id (e.g. 650361).

    Use this when a gallery is stuck/poisoned and should NOT be
    re-queued automatically — the admin can re-add it later by just
    using it in the app like a brand-new link.
    """
    return rescrape.purge_gallery(gallery_id)


# ---- Feature 5: Broadcast to all users ----
class BroadcastBody(BaseModel):
    text: str
    button_text: str = ""
    button_url: str = ""


@router.post("/broadcast")
def broadcast_start(body: BroadcastBody, a: dict = Depends(require_admin)) -> dict:
    """Kick off a broadcast. Delivery happens in a background thread; the
    admin panel polls /broadcast/status/<run_id> for progress."""
    return broadcast.start_broadcast(
        text=body.text,
        button_text=body.button_text,
        button_url=body.button_url,
        initiated_by=int(a.get("id") or 0),
    )


@router.get("/broadcast/status/{run_id}")
def broadcast_status(run_id: str, _a: dict = Depends(require_admin)) -> dict:
    s = broadcast.status(run_id)
    if s is None:
        return {"ok": False, "reason": "run_id not found"}
    return {"ok": True, **s}


@router.get("/broadcast/recent")
def broadcast_recent(limit: int = 10,
                     _a: dict = Depends(require_admin)) -> dict:
    return {"items": broadcast.list_recent(limit=int(max(1, min(50, limit))))}


@router.get("/broadcast/preview")
def broadcast_preview(_a: dict = Depends(require_admin)) -> dict:
    """Return the number of users a broadcast would target (banned users
    excluded). Used by the admin panel to confirm before Send."""
    recipients = broadcast.list_recipients()
    return {"total": len(recipients)}


# ---- Feature 6: Default background theme (app-wide) ----
# v11.6: the admin UI for this feature has been removed (sectionBackground
# was deleted from admin.js); themes are now a per-device preference in
# Settings → Theme (9 palettes). These endpoints remain for backward-compat
# so any external tooling or older Mini App clients still calling them keep
# working. The allowlist has been widened to the v11.6 palette set.
#
# Users' explicit local Theme choice always wins over this server default.
_ALLOWED_BG_THEMES = {
    "dark", "ember",  # ember is a legacy alias of dark
    "light", "sepia", "dracula", "midnight", "amoled",
    "nord", "solarized", "forest",
}


class BackgroundThemeBody(BaseModel):
    theme: str = "dark"


@router.get("/background")
def get_background(_a: dict = Depends(require_admin)) -> dict:
    return {"theme": db.get_setting("default_background_theme", "dark") or "dark"}


@router.post("/background")
def set_background(body: BackgroundThemeBody,
                   _a: dict = Depends(require_admin)) -> dict:
    theme = (body.theme or "dark").strip().lower()
    if theme not in _ALLOWED_BG_THEMES:
        allowed = " | ".join(sorted(_ALLOWED_BG_THEMES))
        return {"ok": False, "reason": f"unknown theme (allowed: {allowed})"}
    db.set_setting("default_background_theme", theme)
    return {"ok": True, "theme": theme}


# ---- Diagnostics ----
@router.get("/diag")
def diag(_a: dict = Depends(require_admin)) -> dict:
    return {
        "scraper": scraper_bridge.route_status(),
        "queue":   queue_bridge.status_summary(),
        "settings": {
            "public_mode":  db.get_public_mode(),
            "default_daily": db.get_default_daily(),
            "default_cooldown_s": db.get_default_cooldown(),
        },
    }


# ---- v12.11 (#1): Detail scraper (details_prefetch_cron) ----
# Admin toggle + live status for the background gallery-detail scraper.
# The cron module itself reads ENABLED at import time; the runtime toggle
# lives in control_flags so the admin can flip it without a redeploy.
try:
    from ..services import details_prefetch_cron as _dpc  # noqa: WPS433
except Exception as _e_dpc:  # noqa: BLE001
    _dpc = None
    _dpc_err = _e_dpc
else:
    _dpc_err = None

_DPC_FLAG_KEY = "details_scraper_enabled"


class DetailsScraperBody(BaseModel):
    enabled: bool


@router.get("/details-scraper")
def get_details_scraper(_a: dict = Depends(require_admin)) -> dict:
    if _dpc is None:
        return {"ok": False, "import_error": str(_dpc_err)[:200], "enabled": False}
    snap = _dpc.last_run_summary()
    # Runtime override via control_flags wins over the env default.
    try:
        from .. import db as _midb
        flag = _midb.get_setting(_DPC_FLAG_KEY, None)
    except Exception:  # noqa: BLE001
        flag = None
    snap["enabled"] = bool(flag) if flag is not None else bool(_dpc.ENABLED)
    snap["ok"] = True
    return snap


@router.post("/details-scraper")
def set_details_scraper(body: DetailsScraperBody, _a: dict = Depends(require_admin)) -> dict:
    # v12.12 (autoscraper fix): the flag lives in Mongo ONLY. The cron in
    # the WORKER process re-reads it every tick via _read_enabled(), so
    # this single write reaches it — no cross-process in-memory flip
    # needed (and none would work anyway: separate Render services).
    db.set_setting(_DPC_FLAG_KEY, bool(body.enabled))
    return {"ok": True, "enabled": bool(body.enabled)}
