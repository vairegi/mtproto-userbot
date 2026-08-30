"""
mongo_state.py — Bot 0 `galleries` collection state machine.

v12.48 (sync-audit):
  * F2  Cross-writer stale reclaim.
        Before: claim() only recovered a stale PROCESSING doc when
        `claim_expires` was strictly less than now. Bot 0's dedup_check
        writes PROCESSING docs WITHOUT `claim_expires` (it measures
        staleness by `started_at`+MINIAPP_STALE_PROCESSING_S), so a
        `$lt` predicate against a MISSING field never matched — Bot 2
        returned "busy" forever on every Bot-0-authored stuck doc.
        Confirmed live pre-fix: 14 PROCESSING docs, oldest ~23 days old,
        none of them ever reclaimable by Bot 2.
        Fix: staleness is now the disjunction (claim_expires < now) OR
        ((now - max(started_at, claimed_at, created_at)) > stale_s) —
        i.e. either writer's timestamp shape marks the doc stale, and
        the atomic update accepts BOTH filters.
  * F5  note_bot2_no_images: the pre-fix version did `find_one_and_update`
        with `upsert=True`, so if the doc had been purged first the $inc
        created a bare doc with `bot2_no_images_count:1` and NO `status`
        field. claim() then treated it as "busy" forever (the fall-
        through at the end of the state machine). Now: use `upsert=False`
        and only run the counter path when a matching PROCESSING doc
        exists; otherwise mark the gid FAILED_BOT2 directly.
  * F6  Converge the doc contract so both writers stamp the same shape:
        - claim() also sets `gallery_id` and `url_hash` (and preserves
          `started_at` for Bot 0 compatibility).
        - mark_completed() writes `gallery_id`, `cover_url`, `tags`,
          `completed_at` alongside the existing delivery keys, and
          $addToSet-s `requested_by` (never clobbers Bot 0's list).
        - mark_failed() writes `failed_reason` in addition to `error`
          so Bot 0's admin dashboard/dedup surface both.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

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


# v12.48 (F2): shared helper — is this PROCESSING doc stale?
#
# Semantics (lease-first, then fallback age check — mirrors Bot 0):
#   1. If `claim_expires` is present and still in the future, the doc has
#      a LIVE lease — never stale regardless of any missing timestamps.
#   2. Otherwise, check age against the newest of started_at / claimed_at /
#      created_at. If any of them is within stale_s, the doc is fresh.
#   3. If none of those signals is usable at all (no live lease AND no
#      usable timestamp), treat as stale — matches Bot 0's pre-fix rule
#      "doc has no timestamp → treat as stuck" and only fires for genuinely
#      broken rows.
def _doc_is_stale(doc: Optional[Dict[str, Any]], now: float, stale_s: float) -> bool:
    if not doc:
        return False
    # 1) Live-lease short-circuit: an unexpired lease keeps the doc fresh.
    exp = doc.get("claim_expires")
    if exp is not None:
        try:
            if float(exp) >= now:
                return False
            lease_seen = True
        except (TypeError, ValueError):
            lease_seen = False
    else:
        lease_seen = False
    # 2) Age check across Bot 0 timestamp shapes.
    ts = 0.0
    for k in ("started_at", "claimed_at", "created_at"):
        v = doc.get(k)
        try:
            fv = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            fv = 0.0
        if fv > ts:
            ts = fv
    if ts > 0.0:
        return (now - ts) > float(stale_s)
    # 3) No usable timestamp — either lease was seen and expired (stale)
    # or no signal at all (stuck). Either way: stale.
    del lease_seen  # kept above for readability of intent; not needed here
    return True


class Galleries:
    def __init__(self, db, stale_s: int):
        self.coll = db["galleries"]
        self.stale_s = stale_s

    # ------------------------------------------------------------------ claim
    def claim(self, gid: str) -> str:
        gid = str(gid)
        now = time.time()
        # v12.48 (F6): fresh doc also carries `gallery_id` and `url_hash`
        # (empty until Bot 0 fills it in) so schema-drift-sensitive Bot 0
        # queries keyed on gallery_id still see this row.
        doc = {
            "_id": gid,
            "gallery_id": gid,
            "url": f"https://nhentai.net/g/{gid}/",
            "url_hash": "",
            "status": STATUS_PROCESSING,
            "source": "bot2fetcher",
            "claimed_at": now,
            "claim_expires": now + self.stale_s,
            "created_at": now,
            "started_at": now,      # F2: also present so Bot 0 sees a fresh doc
            "updated_at": now,
            "requested_by": [],     # F6: mirror Bot 0's shape
        }
        try:
            self.coll.insert_one(doc)
            return "claimed"
        except DuplicateKeyError:
            pass
        existing = self.coll.find_one(
            {"_id": gid},
            {"status": 1, "claim_expires": 1, "started_at": 1,
             "claimed_at": 1, "created_at": 1, "source": 1},
        )
        if not existing:
            return "busy"
        st = existing.get("status")
        if st in DONE_STATUSES:
            return "done"
        if st and st.startswith("FAILED"):
            return "failed"
        if st == STATUS_PROCESSING:
            # v12.48 (F2): decide staleness in code (against the shapes both
            # writers use), then take the claim atomically via a CAS on the
            # OBSERVED timestamps. If a concurrent worker updates any of them
            # between our read and our write, the filter no-matches and we
            # simply report "busy" — the classic optimistic-concurrency win.
            full = self.coll.find_one({"_id": gid})
            if not _doc_is_stale(full, now, float(self.stale_s)):
                return "busy"
            cas_filter: Dict[str, Any] = {
                "_id": gid, "status": STATUS_PROCESSING,
            }
            for key in ("claim_expires", "started_at",
                        "claimed_at", "created_at"):
                if full.get(key) is not None:
                    cas_filter[key] = full.get(key)
            res = self.coll.find_one_and_update(
                cas_filter,
                {"$set": {
                    "claimed_at": now,
                    "claim_expires": now + self.stale_s,
                    "started_at": now,           # F2: refresh Bot 0's clock
                    "source": "bot2fetcher",
                    "updated_at": now,
                    "gallery_id": gid,           # F6: heal missing field
                }},
            )
            return "claimed" if res else "busy"
        return "busy"

    def refresh_claim(self, gid: str) -> None:
        try:
            now = time.time()
            self.coll.update_one(
                {"_id": str(gid), "status": STATUS_PROCESSING},
                # v12.48 (F2): bump BOTH clocks so Bot 0's staleness
                # heuristic ALSO sees the claim as fresh. Without touching
                # started_at, a long PDF download can look stale to Bot 0
                # even while Bot 2 keeps extending claim_expires — that's
                # the double-download race path.
                {"$set": {"claim_expires": now + self.stale_s,
                          "started_at": now,
                          "updated_at": now}},
            )
        except Exception:
            pass

    # ------------------------------------------------------ terminal writes
    def mark_completed(self, gid: str, *, title: str, cover_msg_id: int,
                       pdf_msg_id: int, open_link: str, pages: int = 0,
                       cover_url: str = "",
                       tags: Optional[Iterable[Any]] = None,
                       requested_by: Optional[int] = None) -> None:
        """v12.48 (F6): stamp the union of Bot 0 + Bot 2 delivery keys.

        The extra fields (cover_url, tags, gallery_id, completed_at) are
        exactly what Bot 0's mark_completed() writes. Writing them here
        keeps admin dashboards / mini-app queries / stats that filter or
        project on those keys consistent across BOTH pipelines.
        """
        now = time.time()
        updates: Dict[str, Any] = {
            "status": STATUS_COMPLETED,
            "gallery_id": str(gid),
            "title": (title or "")[:200],
            "url": f"https://nhentai.net/g/{gid}/",
            "pages": int(pages or 0),
            "db_cover_msg_id": int(cover_msg_id),
            "db_pdf_msg_id": int(pdf_msg_id),
            "open_link": open_link,
            "source": "bot2fetcher",
            "completed_at": now,
            "updated_at": now,
            "failed_reason": "",
        }
        if cover_url:
            updates["cover_url"] = str(cover_url)
        if tags is not None:
            # tolerate typed list-of-dicts OR bare list-of-names
            try:
                tag_list: List[Any] = list(tags)
            except TypeError:
                tag_list = []
            if tag_list:
                updates["tags"] = tag_list
        update_doc: Dict[str, Any] = {
            "$set": updates,
            # v12.44: successful delivery wipes the v12.42 retry counter
            "$unset": {"bot2_no_images_count": "", "bot2_last_error": ""},
        }
        if requested_by:
            # F6: never clobber Bot 0's requested_by list.
            update_doc["$addToSet"] = {"requested_by": int(requested_by)}
        self.coll.update_one({"_id": str(gid)}, update_doc, upsert=True)

    # v12.42/v12.48 (F5): persistent counter for Gallery_DLBot's permanent
    # "No images found after download or ZIP extraction" failure.
    def note_bot2_no_images(self, gid: str, error: str,
                            max_retries: int = 3) -> str:
        """Increment the per-gallery 'no images' counter atomically.

        Returns:
          "retry" — attempt count still below max_retries; claim released
                    as stale so the next scan re-claims it.
          "skip"  — attempt count >= max_retries; doc marked FAILED_BOT2_ERROR
                    and will never be sent to @Gallery_DLBot again.
        """
        gid = str(gid)
        now = time.time()
        try:
            # v12.48 (F5): the pre-fix version passed upsert=True. If the
            # doc had already been deleted (e.g. an admin reset), the $inc
            # would create a stub with `bot2_no_images_count:1` and NO
            # `status` field — a state the claimer treats as "busy"
            # forever. Now the counter path is strictly no-upsert.
            doc = self.coll.find_one_and_update(
                {"_id": gid, "status": STATUS_PROCESSING},
                {"$inc": {"bot2_no_images_count": 1},
                 "$set": {"bot2_last_error": (error or "")[:300],
                          "updated_at": now}},
                upsert=False,
                return_document=ReturnDocument.AFTER,
            )
            if doc is None:
                # No PROCESSING doc to count against — this Gallery_DLBot
                # reply is orphan. Record a terminal failure directly.
                log.warning("note_bot2_no_images(%s): no PROCESSING doc — "
                            "marking FAILED_BOT2_ERROR directly", gid)
                self.mark_failed(
                    gid, status=STATUS_FAILED_BOT2,
                    error=f"Gallery_DLBot: no images "
                          f"(no PROCESSING claim): {(error or '')[:200]}")
                return "skip"
            count = int(doc.get("bot2_no_images_count") or 1)
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
        # re-claims it on the next scan cycle (counter preserved). Also
        # backdate started_at so the Bot-0-flavored staleness check agrees.
        try:
            self.coll.update_one(
                {"_id": gid, "status": STATUS_PROCESSING,
                 "source": "bot2fetcher"},
                {"$set": {"claim_expires": 0,
                          "started_at": 0.0,   # F2: agrees with Bot 0 too
                          "updated_at": now}},
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
                "gallery_id": str(gid),                # F6
                "error": (error or "")[:300],
                "failed_reason": (error or "")[:500], # F6: Bot 0 shape
                "source": "bot2fetcher",
                "updated_at": now,
                "failed_at": now,
                "completed_at": now,                   # F6: Bot 0 shape
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
            now = time.time()
            self.coll.update_one(
                {"_id": str(gid), "status": STATUS_PROCESSING,
                 "source": "bot2fetcher"},
                # v12.48 (F2): also push started_at into the future by the
                # same cooldown so Bot 0's staleness clock respects the
                # deferral instead of forcing an early stale_reset.
                {"$set": {"claim_expires": now + int(cooldown_s),
                          "started_at": now + int(cooldown_s),
                          "updated_at": now}},
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
