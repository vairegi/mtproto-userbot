"""
mongo_state.py — Bot 0 `galleries` collection state machine.

v12.40h: added `drop_claim(gid)` — hard-delete the claim doc (only if
we own it) so a Bot-2 error/timeout does NOT tombstone the gallery.
This is the mechanic behind "no cover posted until PDF is confirmed":
if Bot 2 never returns a PDF, the job disappears from Mongo entirely
and can be reattempted later, and nothing lands in the DB channel.
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
        """Delete our PROCESSING claim so the gallery is untouched in Mongo.
        Only deletes if this bot owns the doc — never touches another
        worker's row."""
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
        # alias kept for backward-compat with earlier versions
        self.drop_claim(gid)
