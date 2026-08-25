"""
mongo_state.py — Bot 0 `galleries` collection state machine.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

log = logging.getLogger("bot2fetcher.mongo")

STATUS_PROCESSING = "PROCESSING"
STATUS_COMPLETED = "COMPLETED"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILED_TIMEOUT = "FAILED_TIMEOUT"
STATUS_FAILED_BOT2 = "FAILED_BOT2_ERROR"
STATUS_FAILED_SCRAPE = "FAILED_SCRAPE"
STATUS_FAILED_OTHER = "FAILED_OTHER"

DONE_STATUSES = frozenset({STATUS_COMPLETED, STATUS_PARTIAL})

_lock = threading.Lock()
_client: Optional[MongoClient] = None


def connect(uri: str, db_name: str):
    global _client
    with _lock:
        if _client is None:
            _client = MongoClient(
                uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
                socketTimeoutMS=10000, retryWrites=True, maxPoolSize=8,
            )
            _client.admin.command("ping")
            log.info("✅ mongo connected (db=%s)", db_name)
    return _client[db_name]


class Galleries:
    def __init__(self, db, stale_s: int):
        self.coll = db["galleries"]
        self.stale_s = stale_s

    def claim(self, gid: str) -> str:
        gid = str(gid)
        now = time.time()
        doc = {
            "_id": gid,
            "status": STATUS_PROCESSING,
            "url": f"https://nhentai.net/g/{gid}/",
            "source": "bot2fetcher",
            "claimed_at": now,
            "claim_expires": now + self.stale_s,
            "created_at": now,
            "updated_at": now,
        }
        try:
            self.coll.insert_one(doc)
            return "claimed"
        except DuplicateKeyError:
            pass
        existing = self.coll.find_one({"_id": gid}, {"status": 1, "claim_expires": 1})
        if not existing:
            return "busy"
        st = existing.get("status")
        if st in DONE_STATUSES:
            return "done"
        if st and st.startswith("FAILED"):
            return "failed"
        if st == STATUS_PROCESSING:
            exp = float(existing.get("claim_expires") or 0)
            if exp < now:
                res = self.coll.find_one_and_update(
                    {"_id": gid, "status": STATUS_PROCESSING, "claim_expires": {"$lt": now}},
                    {"$set": {
                        "claimed_at": now, "claim_expires": now + self.stale_s,
                        "source": "bot2fetcher", "updated_at": now,
                    }},
                )
                return "claimed" if res else "busy"
            return "busy"
        return "busy"

    def refresh_claim(self, gid: str) -> None:
        try:
            self.coll.update_one(
                {"_id": str(gid), "status": STATUS_PROCESSING},
                {"$set": {"claim_expires": time.time() + self.stale_s,
                          "updated_at": time.time()}},
            )
        except Exception:
            pass

    def mark_completed(self, gid: str, *, title: str, cover_msg_id: int,
                       pdf_msg_id: int, open_link: str, pages: int = 0) -> None:
        now = time.time()
        self.coll.update_one(
            {"_id": str(gid)},
            {"$set": {
                "status": STATUS_COMPLETED,
                "title": (title or "")[:200],
                "url": f"https://nhentai.net/g/{gid}/",
                "pages": int(pages or 0),
                "db_cover_msg_id": int(cover_msg_id),
                "db_pdf_msg_id": int(pdf_msg_id),
                "open_link": open_link,
                "source": "bot2fetcher",
                "completed_at": now,
                "updated_at": now,
            }},
            upsert=True,
        )

    def mark_failed(self, gid: str, *, status: str, error: str) -> None:
        now = time.time()
        self.coll.update_one(
            {"_id": str(gid)},
            {"$set": {
                "status": status,
                "error": (error or "")[:300],
                "source": "bot2fetcher",
                "updated_at": now,
                "failed_at": now,
            }},
            upsert=True,
        )

    def drop_claim(self, gid: str) -> None:
        try:
            r = self.coll.delete_one({
                "_id": str(gid),
                "status": STATUS_PROCESSING,
                "source": "bot2fetcher",
            })
            if r.deleted_count:
                log.info("🧹 dropped claim for %s (no cover posted)", gid)
        except Exception:
            pass

    def release_claim(self, gid: str) -> None:
        self.drop_claim(gid)

    def count_by_status(self) -> dict:
        try:
            pipe = [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]
            return {r["_id"]: r["n"] for r in self.coll.aggregate(pipe)}
        except Exception as e:
            log.warning("count_by_status failed: %s", e)
            return {}

# ---------------------------------------------------------------------------
# v12.41: per-slot Telethon InputPeer cache.
#
# Telethon's peer table is PER SESSION — an InputPeer resolved under one
# StringSession is meaningless (PeerIdInvalidError) under another. The
# v12.40j fix made each slot warm its OWN cache via iter_dialogs() +
# get_input_entity() at boot; that works but costs ~30 s per slot on a
# cold restart (200-dialog scan + 2 entity lookups per slot).
#
# PeerCache persists the resolved entity in Mongo keyed by
# (slot_idx, kind, session_fingerprint). session_fingerprint is a sha256 of
# the slot's session string, so swapping a session INVALIDATES the row —
# this is the linchpin that prevents regressing to the v12.40i cross-
# session bug. On a warm restart (same session) the entity loads in one
# Mongo read; on any miss / mismatch / error we fall back to the live
# resolution path, so the cache is an optimization, never a hard dep.
# ---------------------------------------------------------------------------
import hashlib as _hashlib
import pickle as _pickle


class PeerCache:
    COLL = "bot2_peer_cache"

    def __init__(self, db):
        self.coll = db[self.COLL]

    @staticmethod
    def fp(client) -> str:
        """Fingerprint the slot's StringSession. Stable across restarts for
        the SAME session; changes the moment the env STRING_SESSION is
        rotated, which is exactly the invalidation trigger we want."""
        try:
            sess = client.session
            save = getattr(sess, "save", None)
            raw = save() if callable(save) else str(sess)
        except Exception:
            raw = repr(client)
        return _hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:32]

    def _id(self, slot: int, kind: str) -> str:
        return f"slot{int(slot)}:{kind}"

    def has(self, slot: int, kind: str, session_fingerprint: str) -> bool:
        try:
            doc = self.coll.find_one(
                {"_id": self._id(slot, kind),
                 "session_fingerprint": session_fingerprint},
                {"_id": 1},
            )
            return doc is not None
        except Exception:
            return False

    def get(self, slot: int, kind: str, session_fingerprint: str):
        """Return the pickled-entity bytes for this slot/kind IF the stored
        fingerprint matches the current session. None on miss/mismatch/err."""
        try:
            doc = self.coll.find_one({"_id": self._id(slot, kind)})
            if not doc:
                return None
            if doc.get("session_fingerprint") != session_fingerprint:
                return None
            blob = doc.get("entity_blob")
            return bytes(blob) if blob is not None else None
        except Exception as e:
            log.warning("peercache get(%s,%s) failed: %s", slot, kind, e)
            return None

    @staticmethod
    def loads(blob: bytes):
        return _pickle.loads(blob)

    def put(self, slot: int, kind: str, entity, session_fingerprint: str) -> None:
        try:
            blob = _pickle.dumps(entity)
            self.coll.update_one(
                {"_id": self._id(slot, kind)},
                {"$set": {
                    "entity_blob": blob,
                    "session_fingerprint": session_fingerprint,
                    "resolved_at": time.time(),
                }},
                upsert=True,
            )
        except Exception as e:
            log.warning("peercache put(%s,%s) failed: %s", slot, kind, e)

