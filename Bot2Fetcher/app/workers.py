"""
workers.py — v13.10 Mongo-backed worker (Telethon userbot) store for Bot 2.

Single source of truth for downloader workers. Replaces the old
STRING_SESSION / STRING_SESSION_N env vars (v12.82) — those are no longer
read. Workers and the shared API credentials live in Mongo so they survive
restarts and can be added/removed at runtime via the admin commands
(/addworker /listworker /removeworker /changeapiandhash) without a redeploy.

Collections (in the same Bot-2 Mongo db as `galleries` etc.):
    bot2_workers   {_id: "creds", api_id: str, api_hash: str, updated_at}
    bot2_workers   {_id: "slot:<n>", slot: n, session: str, api_id, api_hash,
                    added_by, added_at}

Reads are fail-open (return empty on error) so a Mongo hiccup never crashes
the fetcher — it just idles. Writes return a bool.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

_CREDS_ID = "creds"
_SLOT_PREFIX = "slot:"


def _col(db):
    return db["bot2_workers"]


# ---------------------------------------------------------------------------
# API credentials (shared by all workers; api_id/hash are per Telegram app,
# not per userbot session)
# ---------------------------------------------------------------------------
def get_creds(db) -> Optional[Tuple[str, str]]:
    try:
        d = _col(db).find_one({"_id": _CREDS_ID})
        if not d:
            return None
        api_id, api_hash = str(d.get("api_id", "")), str(d.get("api_hash", ""))
        if not api_id or not api_hash:
            return None
        return api_id, api_hash
    except Exception:  # noqa: BLE001
        return None


def set_creds(db, api_id: str, api_hash: str) -> bool:
    try:
        _col(db).update_one(
            {"_id": _CREDS_ID},
            {"$set": {"api_id": str(api_id), "api_hash": str(api_hash),
                      "updated_at": time.time()}},
            upsert=True,
        )
        # Keep every worker row's stored creds in sync (creds are shared;
        # the per-slot copy is informational only — the live reconnect reads
        # the global creds, so this just keeps the docs consistent).
        _col(db).update_many(
            {"_id": {"$regex": f"^{_SLOT_PREFIX}"}},
            {"$set": {"api_id": str(api_id), "api_hash": str(api_hash)}},
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Worker slots (always contiguous 1..N; removal renumbers down)
# ---------------------------------------------------------------------------
def _normalize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = [r for r in rows if isinstance(r.get("slot"), int) and r.get("session")]
    out.sort(key=lambda r: r["slot"])
    return out


def list_workers(db) -> List[Dict[str, Any]]:
    try:
        rows = [d for d in _col(db).find({"_id": {"$regex": f"^{_SLOT_PREFIX}"}})]
        return _normalize(rows)
    except Exception:  # noqa: BLE001
        return []


def sessions(db) -> List[str]:
    return [w["session"] for w in list_workers(db)]


def add_worker(db, session: str, api_id: str, api_hash: str, added_by: int = 0) -> int:
    """Append a worker at the next slot. Returns the new slot number (0 on error)."""
    try:
        slot = len(list_workers(db)) + 1
        _col(db).update_one(
            {"_id": f"{_SLOT_PREFIX}{slot}"},
            {"$set": {"slot": slot, "session": str(session),
                      "api_id": str(api_id), "api_hash": str(api_hash),
                      "added_by": int(added_by), "added_at": time.time()}},
            upsert=True,
        )
        return slot
    except Exception:  # noqa: BLE001
        return 0


def _renumber(db, keep: List[Dict[str, Any]]) -> None:
    """Rewrite the kept workers as contiguous slots 1..len(keep)."""
    col = _col(db)
    col.delete_many({"_id": {"$regex": f"^{_SLOT_PREFIX}"}})
    for i, w in enumerate(keep, 1):
        w = dict(w)
        w.pop("_id", None)  # Mongo _id cannot change when slots renumber.
        w["slot"] = i
        col.update_one({"_id": f"{_SLOT_PREFIX}{i}"}, {"$set": w}, upsert=True)


def remove_workers(db, nums: List[int]) -> List[int]:
    """Remove the given 1-based slot numbers, then renumber the rest down.
    Returns the list of slot numbers actually removed."""
    workers = list_workers(db)
    if not workers:
        return []
    total = len(workers)
    to_remove = sorted({n for n in nums if 1 <= n <= total})
    if not to_remove:
        return []
    keep = [w for w in workers if w["slot"] not in to_remove]
    try:
        _renumber(db, keep)
        return to_remove
    except Exception:  # noqa: BLE001
        return []


def clear_workers(db) -> int:
    workers = list_workers(db)
    try:
        _col(db).delete_many({"_id": {"$regex": f"^{_SLOT_PREFIX}"}})
        return len(workers)
    except Exception:  # noqa: BLE001
        return 0

# ---------------------------------------------------------------------------
# Log channel (v13.12) — Mongo-backed, set via /addlog, no env dependency.
# Same collection as the workers; doc id never collides with "slot:N".
# ---------------------------------------------------------------------------
_LOG_ID = "log_channel"


def get_log_channel(db) -> Optional[str]:
    try:
        d = _col(db).find_one({"_id": _LOG_ID})
        if not d:
            return None
        v = str(d.get("channel_id", "")).strip()
        return v or None
    except Exception:  # noqa: BLE001
        return None


def set_log_channel(db, channel_id: str) -> bool:
    try:
        _col(db).update_one(
            {"_id": _LOG_ID},
            {"$set": {"channel_id": str(channel_id).strip(),
                      "updated_at": time.time()}},
            upsert=True,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def clear_log_channel(db) -> bool:
    try:
        _col(db).delete_one({"_id": _LOG_ID})
        return True
    except Exception:  # noqa: BLE001
        return False
