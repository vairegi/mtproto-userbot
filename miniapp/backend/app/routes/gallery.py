"""
gallery.py — /api/gallery/{id} + /api/gallery/{id}/status

- GET /api/gallery/{id}
    Full metadata for a single gallery. Frontend uses this when the user
    taps a card and opens the detail sheet (so we can show more tags than
    the search grid carries).

- GET /api/gallery/{id}/status
    V2 dedup lookup — is this gallery already in the library (COMPLETED /
    PARTIAL), currently downloading (PROCESSING), previously failed
    (FAILED_*), or unseen? Powers the "Queue" ⇄ "Open Post" swap on cards
    and in the detail sheet without ever mutating server state.

Neither endpoint touches the `galleries` collection in a mutating way —
that job stays with relay_v2.process_job (single-writer invariant).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user
from ..services import scraper_bridge, queue_bridge

router = APIRouter(prefix="/api/gallery", tags=["gallery"])


@router.get("/{gallery_id}")
def get_gallery(gallery_id: str, _user: dict = Depends(get_current_user)) -> dict:
    detail = scraper_bridge.gallery_detail(gallery_id)
    if not detail or not detail.get("id"):
        raise HTTPException(404, "Gallery not found")

    # Best-effort: enrich the detail payload with V2 status so the frontend
    # can render the correct primary action button in one round trip.
    try:
        status = queue_bridge.gallery_status(str(gallery_id))
    except Exception:
        status = {"known": False}
    detail["v2_status"] = status
    return detail


@router.get("/{gallery_id}/status")
def gallery_status(gallery_id: str, _user: dict = Depends(get_current_user)) -> dict:
    """Return the V2 dedup state for a gallery.

    Response shape:
      {
        "gallery_id": "304307",
        "known": true|false,
        "status": "COMPLETED"|"PROCESSING"|"PARTIAL"|"FAILED_*"|null,
        "open_link": "https://t.me/c/.../123"|null,   # only when COMPLETED/PARTIAL
        "title":  "...",
        "pages":  65,
        "completed_at": 1717430400.0,                  # epoch seconds
        "failed_reason": ""                            # non-empty on FAILED_*
      }

    Frontend rules:
      - known && status in (COMPLETED, PARTIAL) → show "Open Post" (open_link)
      - known && status == PROCESSING           → show "Downloading…" (disabled)
      - known && status starts with FAILED_     → show "Queue" (retry) +
                                                   surface `failed_reason` in a
                                                   small subtitle if desired
      - !known                                  → show "Queue"
    """
    try:
        info = queue_bridge.gallery_status(str(gallery_id))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"gallery_status unavailable: {e}")
    # Always return 200 with a well-defined shape; the frontend distinguishes
    # "not seen yet" from a hard error via `known` / `error`.
    if not isinstance(info, dict):
        raise HTTPException(500, "gallery_status returned unexpected shape")
    return info
