"""
mongo_state.py — Bot 0 `galleries` collection state machine.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from pymongo import MongoClient
from pymongo import ReturnDocument
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
            },
            # v12.44: successful delivery wipes the v12.42 retry counter
            # so this gid is permanently clean.
            "$unset": {"bot2_no_images_count": "", "bot2_last_error": ""}},
            upsert=True,
        )

    # v12.42: persistent counter for Gallery_DLBot's permanent
    # "No images found after download or ZIP extraction" failure.
    # Lives on the galleries doc so it survives the claim being released —
    # unlike drop_claim(), which deletes the row and used to reset all
    # memory of the failure (the infinite re-send loop Ryan reported).
    def note_bot2_no_images(self, gid: str, error: str,
                            max_retries: int = 3) -> str:
        """Increment the per-gallery 'no images' counter.
        Returns "retry" (attempts 1..max_retries-1: claim released as stale
        so the next scan cycle re-claims it) or "skip" (attempt >=
        max_retries: doc marked FAILED_BOT2_ERROR, never sent again)."""
        gid = str(gid)
        now = time.time()
        try:
            doc = self.coll.find_one_and_update(
                {"_id": gid},
                {"$inc": {"bot2_no_images_count": 1},
                 "$set": {"bot2_last_error": (error or "")[:300],
                          "updated_at": now}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            count = int((doc or {}).get("bot2_no_images_count") or 1)
        except Exception as e:
            log.warning("note_bot2_no_images(%s) failed: %s — "
                        "falling back to drop_claim", gid, e)
            self.drop_claim(gid)
            return "retry"
        if count >= max_retries:
            self.mark_failed(
                gid, status=STATUS_FAILED_BOT2,
                error=f"Gallery_DLBot: no images after {count} attempts: "
                      f"{(error or '')[:200]}")
            return "skip"
        # Release the claim WITHOUT deleting the row: claim_expires=0 makes
        # the doc immediately stale so claim()'s stale-recovery path
        # re-claims it on the next scan cycle (counter preserved).
        try:
            self.coll.update_one(
                {"_id": gid, "status": STATUS_PROCESSING,
                 "source": "bot2fetcher"},
                {"$set": {"claim_expires": 0, "updated_at": now}},
            )
        except Exception as e:
            log.warning("note_bot2_no_images(%s) stale-release failed: %s",
                        gid, e)
        return "retry"

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
            },
            # v12.44: terminal failure rows clear the retry counter (a
            # later force-rescrape recreates the doc from zero anyway).
            "$unset": {"bot2_no_images_count": ""}},
            upsert=True,
        )

    def defer_claim(self, gid: str, cooldown_s: int = 3600) -> None:
        """v12.45: park a claim instead of dropping it. Keeps the row with
        claim_expires = now + cooldown so claim() returns 'busy' until the
        cooldown lapses — used for transient upstream failures (403/429/
        5xx) where drop_claim's row-DELETE would either hammer nhentai
        every cycle or (pre-v12.44) lose all memory of the failure."""
        try:
            self.coll.update_one(
                {"_id": str(gid), "status": STATUS_PROCESSING,
                 "source": "bot2fetcher"},
                {"$set": {"claim_expires": time.time() + int(cooldown_s),
                          "updated_at": time.time()}},
            )
            log.info("⏳ deferred claim for %s (%ds cooldown)",
                     gid, int(cooldown_s))
        except Exception as e:
            log.warning("defer_claim(%s) failed: %s", gid, e)

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

    def get_known_states(self, gids: list) -> dict:
        """v12.44: batch status lookup — ONE Mongo round-trip for a whole
        scan cycle instead of one claim() attempt per gid. Returns
        {gid: status} for rows that exist; missing gids are simply absent.
        Used to pre-seed the producer's known-done / known-failed sets."""
        ids = [str(g) for g in gids if g]
        if not ids:
            return {}
        try:
            cur = self.coll.find({"_id": {"$in": ids}}, {"status": 1})
            return {str(d["_id"]): (d.get("status") or "") for d in cur}
        except Exception as e:
            log.warning("get_known_states failed: %s", e)
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

