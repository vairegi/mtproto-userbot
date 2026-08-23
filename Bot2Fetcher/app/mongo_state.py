"""
mongo_state.py — Bot 0 `galleries` collection state machine (self-contained).

THIS IS A DELIBERATE COPY of repo-root gallery_state.py semantics
(v12.34l). Bot2Fetcher does NOT import repo-root modules so it can deploy
standalone on its own Render service; the COLLECTION NAME, FIELD NAMES and
STATUS VALUES below are the contract and must never drift:

    collection : galleries
    _id        : str(gallery_id)
    status     : PROCESSING | COMPLETED | PARTIAL | FAILED_TIMEOUT |
                 FAILED_BOT2_ERROR | FAILED_SCRAPE | FAILED_OTHER
    on done    : db_cover_msg_id, db_pdf_msg_id, open_link  (Bot 0 reads
                 these three to forward instantly — nothing else matters)

Claim policy for this bot: NEVER-TRIED ONLY.
  * no doc                    -> claim (insert PROCESSING)
  * PROCESSING, claim expired -> re-claim (stuck job from a crashed worker)
  * PROCESSING, fresh         -> someone (Bot 0 or our other slot) is on it
  * COMPLETED / PARTIAL       -> already in DB channel, skip
  * FAILED_*                  -> operator said: never retry, skip

All writes are find_one_and_update / insert_one with DuplicateKey handling
— atomic, so Bot 0's worker and both of our slots can race the same gid
and exactly ONE wins.
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
    """One process-wide client (same pattern as repo-root db.py)."""
    global _client
    with _lock:
        if _client is None:
            _client = MongoClient(
                uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
                socketTimeoutMS=10000, retryWrites=True, maxPoolSize=8,
            )
            _client.admin.command("ping")
            log.info("mongo connected (db=%s)", db_name)
    return _client[db_name]


class Galleries:
    def __init__(self, db, stale_s: int):
        self.coll = db["galleries"]
        self.stale_s = stale_s

    # ------------------------------------------------------------------
    def claim(self, gid: str) -> str:
        """Return 'claimed' | 'done' | 'failed' | 'busy'."""
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
            if exp < now:  # stale -> atomic re-claim
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

    # ------------------------------------------------------------------
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

    def release_claim(self, gid: str) -> None:
        """Give up a claim without tombstoning (e.g. shutdown mid-job)."""
        try:
            self.coll.delete_one({"_id": str(gid), "status": STATUS_PROCESSING,
                                  "source": "bot2fetcher"})
        except Exception:
            pass
