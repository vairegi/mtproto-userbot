"""
gallery_state.py — MongoDB state-machine helpers for the V2 dedup + delivery
collection (`galleries`). See docs/ARCHITECTURE_V2.md.

Design principles
-----------------
1. This module NEVER imports Telethon or hf_scraper — it is a pure Mongo
   adapter. `relay.py`, `queue_service.py` and the mini-app backend all
   depend on it, but it depends on nothing in the runtime stack. That keeps
   the state machine testable in isolation and unbreakable by refactors
   elsewhere.

2. Every mutation goes through a `find_one_and_update` — no read/modify/write
   loops in Python. This preserves atomic dedup even when several worker
   processes / mini-app requests race for the same gallery_id.

3. All timestamps use `time.time()` (float seconds since epoch). We do NOT
   store BSON datetimes; the rest of the codebase already uses epoch floats
   (`db.now_ts`) and mixing types would break the admin dashboard.

4. All doc IDs are STRINGS. Even though nhentai gallery IDs are numeric, we
   store them as strings because (a) other providers may use non-numeric
   IDs later, (b) MongoDB _id equality survives int/str drift, and (c)
   Python's dict + Mongo's document model both prefer strings for IDs.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import db  # local module — the MongoHandle-returning connect()

log = logging.getLogger("gallery_state")

# ---------------------------------------------------------------------------
# Status constants (mirror docs/ARCHITECTURE_V2.md §4)
# ---------------------------------------------------------------------------

STATUS_PROCESSING       = "PROCESSING"
STATUS_COMPLETED        = "COMPLETED"
STATUS_PARTIAL          = "PARTIAL"            # cover posted, PDF forwarded, but scrape returned no title
STATUS_FAILED_TIMEOUT   = "FAILED_TIMEOUT"     # Bot 2 never replied
STATUS_FAILED_BOT2      = "FAILED_BOT2_ERROR"  # Bot 2 replied with plain text
STATUS_FAILED_SCRAPE    = "FAILED_SCRAPE"      # in-house cover scrape returned None
STATUS_FAILED_OTHER     = "FAILED_OTHER"
STATUS_FAILED_RECOVERED = "FAILED_RECOVERED"   # written by scripts/migrate_v2_recover_stuck.py

TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED, STATUS_PARTIAL,
    STATUS_FAILED_TIMEOUT, STATUS_FAILED_BOT2,
    STATUS_FAILED_SCRAPE, STATUS_FAILED_OTHER,
    STATUS_FAILED_RECOVERED,
})

# ---------------------------------------------------------------------------
# Config knobs (env-driven, defaults from docs/ARCHITECTURE_V2.md §6)
# ---------------------------------------------------------------------------

def _stale_processing_seconds() -> int:
    try:
        v = int(os.getenv("MINIAPP_STALE_PROCESSING_S", "900") or "900")
        return v if v > 0 else 900
    except (ValueError, TypeError):
        return 900


# ---------------------------------------------------------------------------
# gallery_id extraction — accepts an nhentai URL, hentaifox URL, or bare id
# ---------------------------------------------------------------------------

_NHENTAI_RE  = re.compile(r"nhentai\.net/g/(\d+)",    re.IGNORECASE)
_HENTAIFOX_RE = re.compile(r"hentaifox\.com/gallery/(\d+)", re.IGNORECASE)
_BARE_ID_RE  = re.compile(r"^\d+$")


def extract_gallery_id(url_or_id: str) -> Optional[str]:
    """Return the canonical gallery_id as a string, or None if none found.

    Accepts full URLs (with or without trailing slash / query / fragment),
    plain numeric IDs, and mixed input like "@user https://nhentai.net/g/1234/".
    """
    if not url_or_id:
        return None
    s = str(url_or_id).strip()
    if _BARE_ID_RE.match(s):
        return s
    m = _NHENTAI_RE.search(s)
    if m:
        return m.group(1)
    m = _HENTAIFOX_RE.search(s)
    if m:
        # Hentaifox IDs live in a separate namespace from nhentai IDs. We
        # prefix them so the two never collide in the galleries collection.
        return f"hf_{m.group(1)}"
    return None


# ---------------------------------------------------------------------------
# Result payload — the shape every caller (miniapp, admin bot, relay) returns
# ---------------------------------------------------------------------------

@dataclass
class DedupDecision:
    """Outcome of a dedup-gate lookup, ready to serialize to the mini-app."""
    action: str                                # "proceed" | "already_completed"
                                               # | "already_processing" | "stale_reset"
    gallery_id: str
    status: Optional[str] = None
    open_link: Optional[str] = None            # deep-link to DB channel post
    db_cover_msg_id: Optional[int] = None
    db_pdf_msg_id: Optional[int] = None
    title: Optional[str] = None
    reason: Optional[str] = None               # human-readable, for admin_bot / logs

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "action": self.action,
            "gallery_id": self.gallery_id,
        }
        if self.status is not None:        d["status"] = self.status
        if self.open_link is not None:     d["open_link"] = self.open_link
        if self.db_cover_msg_id is not None: d["db_cover_msg_id"] = self.db_cover_msg_id
        if self.db_pdf_msg_id is not None:   d["db_pdf_msg_id"] = self.db_pdf_msg_id
        if self.title is not None:         d["title"] = self.title
        if self.reason is not None:        d["reason"] = self.reason
        return d


# ---------------------------------------------------------------------------
# Core read + atomic writes
# ---------------------------------------------------------------------------

def get(conn: db.MongoHandle, gallery_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the current gallery doc, or None if it doesn't exist."""
    if not gallery_id:
        return None
    return conn.galleries.find_one({"_id": str(gallery_id)})


def _is_stale_processing(doc: Dict[str, Any]) -> bool:
    if not doc or doc.get("status") != STATUS_PROCESSING:
        return False
    started = float(doc.get("started_at") or doc.get("created_at") or 0.0)
    if started <= 0:
        return True   # doc has no timestamp → treat as stuck
    return (time.time() - started) > _stale_processing_seconds()


def dedup_check(
    conn: db.MongoHandle,
    gallery_id: str,
    *,
    url: str = "",
    url_hash: str = "",
    requested_by: Optional[int] = None,
) -> DedupDecision:
    """The single entry point for "should this request do any work?".

    Atomically decides one of four things:

      - "already_completed"  : return the existing DB-channel deep-link.
      - "already_processing" : another worker is on it right now.
      - "stale_reset"        : the previous PROCESSING doc is >15 min old;
                                we reset it to a fresh PROCESSING claim and
                                the caller may proceed.
      - "proceed"            : brand-new gallery_id; we inserted a fresh
                                PROCESSING doc and the caller must run the
                                cover-scrape + Bot 2 flow.

    In all "may proceed" cases the doc's `requested_by` array is appended to
    (deduplicated) so admins can see who asked.
    """
    gid = str(gallery_id)
    now = time.time()

    existing = get(conn, gid)

    # ---- Terminal COMPLETED -------------------------------------------------
    if existing and existing.get("status") == STATUS_COMPLETED:
        _append_requester(conn, gid, requested_by)
        return DedupDecision(
            action="already_completed",
            gallery_id=gid,
            status=STATUS_COMPLETED,
            open_link=existing.get("open_link"),
            db_cover_msg_id=existing.get("db_cover_msg_id"),
            db_pdf_msg_id=existing.get("db_pdf_msg_id"),
            title=existing.get("title"),
        )

    # ---- Terminal PARTIAL (has PDF but no proper cover) ---------------------
    # We treat PARTIAL as "already delivered" for dedup purposes — the PDF is
    # already forwarded into the channel; a second run would just duplicate
    # it. Admin can /resetdoc <gid> to re-run.
    if existing and existing.get("status") == STATUS_PARTIAL:
        _append_requester(conn, gid, requested_by)
        return DedupDecision(
            action="already_completed",
            gallery_id=gid,
            status=STATUS_PARTIAL,
            open_link=existing.get("open_link"),
            db_pdf_msg_id=existing.get("db_pdf_msg_id"),
            title=existing.get("title"),
            reason="cover-less delivery on file",
        )

    # ---- Live PROCESSING ----------------------------------------------------
    if existing and existing.get("status") == STATUS_PROCESSING:
        if _is_stale_processing(existing):
            # Reset stale processing atomically, then proceed as if new.
            reset = conn.galleries.find_one_and_update(
                {"_id": gid, "status": STATUS_PROCESSING},
                {"$set": {
                    "status": STATUS_PROCESSING,
                    "started_at": now,
                    "url": url or existing.get("url", ""),
                    "url_hash": url_hash or existing.get("url_hash", ""),
                }},
                return_document=True,
            )
            _append_requester(conn, gid, requested_by)
            log.warning("stale PROCESSING reset for gallery_id=%s", gid)
            return DedupDecision(
                action="stale_reset",
                gallery_id=gid,
                status=STATUS_PROCESSING,
                reason=f"prior PROCESSING was >{_stale_processing_seconds()}s old",
            )
        # Fresh in-flight — tell caller to back off.
        _append_requester(conn, gid, requested_by)
        return DedupDecision(
            action="already_processing",
            gallery_id=gid,
            status=STATUS_PROCESSING,
            reason="another worker is downloading this gallery",
        )

    # ---- FAILED_* / not-yet-seen: claim a fresh PROCESSING slot -------------
    # Atomic upsert with a filter: only accept the write if there's no doc,
    # or if the existing doc is in a FAILED_* state (retryable).
    retryable_filter = {
        "$or": [
            {"_id": gid, "status": {"$in": [
                STATUS_FAILED_TIMEOUT, STATUS_FAILED_BOT2,
                STATUS_FAILED_SCRAPE,  STATUS_FAILED_OTHER,
                STATUS_FAILED_RECOVERED,
            ]}},
            {"_id": gid, "status": {"$exists": False}},
        ]
    }

    fresh_doc = {
        "_id": gid,
        "gallery_id": gid,
        "url": url,
        "url_hash": url_hash,
        "status": STATUS_PROCESSING,
        "created_at": now,
        "started_at": now,
        "completed_at": None,
        "failed_reason": "",
        "requested_by": [int(requested_by)] if requested_by else [],
    }

    if existing is None:
        # No doc at all — insert. Race-safe because _id is unique.
        try:
            conn.galleries.insert_one(fresh_doc)
        except Exception as e:  # noqa: BLE001 — likely DuplicateKey (another worker beat us)
            log.info("insert race for gallery_id=%s (%s); re-checking", gid, e)
            return dedup_check(
                conn, gid, url=url, url_hash=url_hash, requested_by=requested_by,
            )
        return DedupDecision(action="proceed", gallery_id=gid, status=STATUS_PROCESSING)

    # Existing doc is FAILED_*: retry by overwriting the terminal state.
    updated = conn.galleries.find_one_and_update(
        retryable_filter,
        {"$set": {
            "status": STATUS_PROCESSING,
            "url": url or existing.get("url", ""),
            "url_hash": url_hash or existing.get("url_hash", ""),
            "started_at": now,
            "failed_reason": "",
            "completed_at": None,
        }},
        return_document=True,
    )
    _append_requester(conn, gid, requested_by)
    if updated:
        return DedupDecision(action="proceed", gallery_id=gid, status=STATUS_PROCESSING,
                             reason=f"retry after {existing.get('status')}")
    # Someone else changed the state between our read and update — recurse.
    return dedup_check(conn, gid, url=url, url_hash=url_hash, requested_by=requested_by)


def _append_requester(
    conn: db.MongoHandle, gallery_id: str, user_id: Optional[int]
) -> None:
    if not user_id:
        return
    try:
        conn.galleries.update_one(
            {"_id": str(gallery_id)},
            {"$addToSet": {"requested_by": int(user_id)}},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("failed to append requester %s for %s: %s", user_id, gallery_id, e)


# ---------------------------------------------------------------------------
# Terminal transitions (called by relay after the flow finishes)
# ---------------------------------------------------------------------------

def mark_completed(
    conn: db.MongoHandle,
    gallery_id: str,
    *,
    title: Optional[str] = None,
    pages: Optional[int] = None,
    tags: Optional[List[Dict[str, Any]]] = None,
    cover_url: Optional[str] = None,
    db_cover_msg_id: Optional[int] = None,
    db_pdf_msg_id: Optional[int] = None,
    open_link: Optional[str] = None,
    job_id: Optional[int] = None,
) -> None:
    """Move a PROCESSING doc to COMPLETED and stamp delivery details."""
    updates = {
        "status": STATUS_COMPLETED,
        "completed_at": time.time(),
        "failed_reason": "",
    }
    if title is not None:           updates["title"] = title
    if pages is not None:            updates["pages"] = int(pages)
    if tags is not None:             updates["tags"] = tags
    if cover_url is not None:        updates["cover_url"] = cover_url
    if db_cover_msg_id is not None:  updates["db_cover_msg_id"] = int(db_cover_msg_id)
    if db_pdf_msg_id is not None:    updates["db_pdf_msg_id"] = int(db_pdf_msg_id)
    if open_link is not None:        updates["open_link"] = open_link
    if job_id is not None:           updates["job_id"] = int(job_id)
    conn.galleries.update_one({"_id": str(gallery_id)}, {"$set": updates}, upsert=False)


def mark_partial(
    conn: db.MongoHandle,
    gallery_id: str,
    *,
    db_pdf_msg_id: Optional[int] = None,
    open_link: Optional[str] = None,
    reason: str = "cover-less delivery",
    job_id: Optional[int] = None,
) -> None:
    updates = {
        "status": STATUS_PARTIAL,
        "completed_at": time.time(),
        "failed_reason": reason,
    }
    if db_pdf_msg_id is not None: updates["db_pdf_msg_id"] = int(db_pdf_msg_id)
    if open_link is not None:     updates["open_link"] = open_link
    if job_id is not None:        updates["job_id"] = int(job_id)
    conn.galleries.update_one({"_id": str(gallery_id)}, {"$set": updates}, upsert=False)


def mark_failed(
    conn: db.MongoHandle,
    gallery_id: str,
    *,
    status: str = STATUS_FAILED_OTHER,
    reason: str = "",
    purge: bool = False,
) -> None:
    """Move a PROCESSING doc to a FAILED_* tombstone, OR delete it entirely.

    - `purge=True` deletes the doc (used for FAILED_BOT2_ERROR per the spec:
      "delete cover post + purge doc so user can retry cleanly").
    - `purge=False` keeps the doc as a tombstone so a lazy `dedup_check` can
      surface a helpful "prior attempt failed: <reason>" (default).
    """
    if purge:
        conn.galleries.delete_one({"_id": str(gallery_id)})
        return
    conn.galleries.update_one(
        {"_id": str(gallery_id)},
        {"$set": {
            "status": status,
            "failed_reason": (reason or "")[:500],
            "completed_at": time.time(),
        }},
        upsert=False,
    )


# ---------------------------------------------------------------------------
# Admin / diag queries
# ---------------------------------------------------------------------------

def counts_by_status(conn: db.MongoHandle) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in conn.galleries.aggregate([
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
    ]):
        out[str(row.get("_id") or "unknown")] = int(row.get("n") or 0)
    return out


def list_by_status(
    conn: db.MongoHandle, status: str, *, limit: int = 20
) -> List[Dict[str, Any]]:
    cur = (
        conn.galleries
        .find({"status": status})
        .sort([("started_at", -1)])
        .limit(int(limit))
    )
    return list(cur)


def reset_doc(conn: db.MongoHandle, gallery_id: str) -> bool:
    """Admin escape hatch: delete a doc so the next request re-runs cleanly."""
    r = conn.galleries.delete_one({"_id": str(gallery_id)})
    return bool(r.deleted_count)
