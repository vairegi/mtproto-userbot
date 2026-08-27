"""backup_db.py — BackupDB (high-availability Database Channel) helpers.

v1.22 — shared logic for the Backup Database Channel feature.

A secondary private Telegram channel (BackupDB) receives a server-side
forward of every cover post + PDF that lands in the Main Database
Channel, and the resulting BackupDB message IDs are stamped onto the
SAME Mongo ``galleries`` doc (canonical single record per gallery):

    backup_channel_id     : int    — the BackupDB channel id
    backup_cover_msg_id   : int    — cover msg id inside BackupDB
    backup_pdf_msg_id     : int    — PDF msg id inside BackupDB
    backup_status         : str    — "ok" | "cover_only"
                                     | "missing_in_main" | "needs_review"
    backed_up_at          : float  — epoch seconds

Runtime toggle lives in Mongo ``backup_state`` (doc _id="state"):

    use_backup        : bool  — set by /usebackupDB on|off (admin_bot)
    backup_channel_id : int   — written by the backfill script when it
                                auto-creates the channel (or mirrored
                                from the BACKUP_DB_CHANNEL_ID env var)

When ``use_backup`` is ON, Bot 0's delivery path (auto-DM / Mini App
download) forwards from BackupDB instead of the Main channel, so a
banned/deleted Main channel no longer breaks user downloads.

Mirror failures are ALWAYS non-fatal: log + counters only, never block
the user-facing download pipeline.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

log = logging.getLogger("backup_db")

STATE_ID = "state"


# ---------------------------------------------------------------------------
# backup_state doc (toggle + channel id)
# ---------------------------------------------------------------------------

def get_state(conn) -> Dict[str, Any]:
    try:
        doc = conn.backup_state.find_one({"_id": STATE_ID})
        return dict(doc) if doc else {}
    except Exception as e:  # noqa: BLE001
        log.warning("backup_db: state read failed: %s", e)
        return {}


def use_backup_enabled(conn) -> bool:
    return bool(get_state(conn).get("use_backup"))


def set_use_backup(conn, on: bool) -> None:
    conn.backup_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"use_backup": bool(on), "use_backup_at": time.time()}},
        upsert=True,
    )


def get_backup_channel_id(conn, settings=None) -> int:
    """Env var (BACKUP_DB_CHANNEL_ID) wins when set; else Mongo state."""
    if settings is not None:
        env_id = int(getattr(settings, "backup_db_channel_id", 0) or 0)
        if env_id:
            return env_id
    return int(get_state(conn).get("backup_channel_id") or 0)


def set_backup_channel_id(conn, channel_id: int) -> None:
    conn.backup_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"backup_channel_id": int(channel_id)}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# galleries doc stamping
# ---------------------------------------------------------------------------

def stamp_gallery_backup(
    conn,
    gallery_id: str,
    *,
    backup_channel_id: int,
    backup_cover_msg_id: Optional[int] = None,
    backup_pdf_msg_id: Optional[int] = None,
    status: str = "ok",
) -> None:
    updates: Dict[str, Any] = {
        "backup_channel_id": int(backup_channel_id),
        "backup_status": status,
        "backed_up_at": time.time(),
    }
    if backup_cover_msg_id:
        updates["backup_cover_msg_id"] = int(backup_cover_msg_id)
    if backup_pdf_msg_id:
        updates["backup_pdf_msg_id"] = int(backup_pdf_msg_id)
    conn.galleries.update_one(
        {"_id": str(gallery_id)}, {"$set": updates}, upsert=False,
    )


def bump_stat(conn, key: str, n: int = 1) -> None:
    """Counters doc _id="backupdb": mirrored_ok / mirrored_fail."""
    try:
        conn.counters.update_one(
            {"_id": "backupdb"}, {"$inc": {key: int(n)}}, upsert=True,
        )
    except Exception:  # noqa: BLE001
        pass


def get_counters(conn) -> Dict[str, Any]:
    try:
        doc = conn.counters.find_one({"_id": "backupdb"})
        return dict(doc) if doc else {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Delivery-path source selection (/usebackupDB toggle)
# ---------------------------------------------------------------------------

def delivery_source(conn, gallery_id: str, main_channel_id: int,
                    cover_msg_id, pdf_msg_id):
    """Return (from_chat_id, [msg_ids]) honouring the /usebackupDB toggle.

    Toggle OFF  → (main_channel_id, [cover, pdf]) exactly as before.
    Toggle ON   → (backup_channel_id, [backup_cover, backup_pdf]) when the
                  galleries doc carries backup ids; otherwise falls back to
                  Main with a log line (never breaks delivery).
    """
    ids = [int(m) for m in (cover_msg_id, pdf_msg_id) if m]
    if not gallery_id or not use_backup_enabled(conn):
        return int(main_channel_id), ids
    try:
        doc = conn.galleries.find_one(
            {"_id": str(gallery_id)},
            {"backup_channel_id": 1, "backup_cover_msg_id": 1,
             "backup_pdf_msg_id": 1},
        )
    except Exception:  # noqa: BLE001
        doc = None
    if doc and doc.get("backup_channel_id") and (
        doc.get("backup_cover_msg_id") or doc.get("backup_pdf_msg_id")
    ):
        bids = [
            int(m)
            for m in (doc.get("backup_cover_msg_id"),
                      doc.get("backup_pdf_msg_id"))
            if m
        ]
        return int(doc["backup_channel_id"]), bids
    log.info("backup_db: gid=%s has no backup ids yet — using MAIN channel",
             gallery_id)
    return int(main_channel_id), ids


# ---------------------------------------------------------------------------
# Live mirror (best-effort, never raises)
# ---------------------------------------------------------------------------

async def mirror_pair_to_backup(
    client,
    conn,
    settings,
    gallery_id: str,
    main_channel,
    cover_msg_id: int,
    pdf_msg_id: int,
    *,
    log_prefix: str = "mirror",
) -> None:
    """Server-side forward of a JUST-POSTED cover+PDF pair into BackupDB.

    Called right after the Main-channel write succeeds. Failure policy
    (operator-approved): log loudly + bump counter, NEVER block or fail
    the user download. No backup channel configured yet → silent no-op.
    """
    try:
        bch_id = get_backup_channel_id(conn, settings)
        if not bch_id:
            return
        res = await client.forward_messages(
            bch_id, [int(cover_msg_id), int(pdf_msg_id)],
            main_channel, drop_author=True,
        )
        msgs = res if isinstance(res, list) else [res]
        bc = bp = None
        if len(msgs) > 0 and msgs[0] is not None:
            bc = int(getattr(msgs[0], "id", 0) or 0) or None
        if len(msgs) > 1 and msgs[1] is not None:
            bp = int(getattr(msgs[1], "id", 0) or 0) or None
        if bc or bp:
            stamp_gallery_backup(
                conn, gallery_id,
                backup_channel_id=bch_id,
                backup_cover_msg_id=bc,
                backup_pdf_msg_id=bp,
                status="ok" if bp else "cover_only",
            )
            bump_stat(conn, "mirrored_ok")
            log.info(
                "backup_db %s: gid=%s mirrored → backup cover=%s pdf=%s",
                log_prefix, gallery_id, bc, bp,
            )
        else:
            bump_stat(conn, "mirrored_fail")
            log.warning(
                "backup_db %s: gid=%s forward returned no message ids",
                log_prefix, gallery_id,
            )
    except Exception as e:  # noqa: BLE001
        bump_stat(conn, "mirrored_fail")
        log.warning(
            "backup_db %s: gid=%s mirror failed (non-fatal): %s",
            log_prefix, gallery_id, e,
        )
