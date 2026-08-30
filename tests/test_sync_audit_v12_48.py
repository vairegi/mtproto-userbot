"""v12.48 sync-audit unit tests — pure Python, no live services.

Covers the six behavioral fixes in the sync-audit zip. Each test exercises
the actual patched code path against a fake DB adapter that records
operations, so regressions get caught without needing Turso/Mongo.

Run from repo root:  python3 tests/test_sync_audit_v12_48.py
"""
from __future__ import annotations
import importlib.util, pathlib, sys, time, types

ROOT = pathlib.Path(__file__).resolve().parent.parent

def _load(rel: str, name: str, extra_globals: dict | None = None):
    """Load a repo file as a module, letting us inject its dependencies
    via `extra_globals` before exec (used to stub `db` for gallery_state).
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    if extra_globals:
        for k, v in extra_globals.items():
            setattr(mod, k, v)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# F1 — Bot 2 turso_store: canonical-list search payload extraction
# ---------------------------------------------------------------------------
def test_f1_extract_ids_from_search_payload():
    # turso_store lives under Bot2Fetcher/app/. It self-registers a `_ROOT`
    # onto sys.path at import time so its `from common.turso_http` fallback
    # works — that's fine for a unit test that only touches a pure helper.
    ts = _load("Bot2Fetcher/app/turso_store.py", "b2_turso_store")

    # canonical (post v1.16): list of card dicts
    canon = [
        {"id": "111", "title": "A", "cover": "x", "pages": 5, "tags": []},
        {"id": 222, "title": "B", "cover": "y", "pages": 3, "tags": []},
        "junk",             # dropped
        {"no_id": True},    # dropped
        None,               # dropped
        {"id": "abc"},      # dropped (non-digit)
    ]
    got = ts._extract_ids_from_search_payload(canon)
    assert got == ["111", "222"], got

    # legacy raw-v2 (pre v1.16): dict with "result"
    legacy = {"result": [{"id": 333, "media_id": "9"},
                         {"id": "444"}], "num_pages": 12}
    got = ts._extract_ids_from_search_payload(legacy)
    assert got == ["333", "444"], got

    # empty / unusable
    assert ts._extract_ids_from_search_payload(None) == []
    assert ts._extract_ids_from_search_payload("") == []
    assert ts._extract_ids_from_search_payload({"num_pages": 1}) == []
    assert ts._extract_ids_from_search_payload([]) == []
    print("F1 extract_ids_from_search_payload OK")


def test_f1_regression_canonical_list_produces_ids():
    """The v12.44 predecessor returned 0 ids from a canonical list payload
    (payload.get('result') on a list crashes and was try/except-swallowed).
    Guard against silent regression to that behavior."""
    ts = _load("Bot2Fetcher/app/turso_store.py", "b2_turso_store_regress")
    canon_only_list = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    assert ts._extract_ids_from_search_payload(canon_only_list) == ["1", "2", "3"]
    print("F1 canonical-list regression guard OK")


# ---------------------------------------------------------------------------
# F2 — Cross-writer stale reclaim (Bot 2 side)
# ---------------------------------------------------------------------------
def test_f2_doc_is_stale():
    ms = _load("Bot2Fetcher/app/mongo_state.py", "b2_mongo_state")
    now = time.time()
    STALE = 900.0

    # Bot 2 shape, fresh
    assert ms._doc_is_stale({"claim_expires": now + 60}, now, STALE) is False
    # Bot 2 shape, expired
    assert ms._doc_is_stale({"claim_expires": now - 60}, now, STALE) is True
    # Bot 0 shape (no claim_expires), fresh
    assert ms._doc_is_stale({"started_at": now - 30}, now, STALE) is False
    # Bot 0 shape, stale
    assert ms._doc_is_stale({"started_at": now - 3600}, now, STALE) is True
    # Bot 0 shape via claimed_at fallback
    assert ms._doc_is_stale({"claimed_at": now - 3600}, now, STALE) is True
    # Bot 0 shape via created_at fallback
    assert ms._doc_is_stale({"created_at": now - 3600}, now, STALE) is True
    # NO timestamps at all → stale (matches Bot 0 pre-fix behavior)
    assert ms._doc_is_stale({"status": "PROCESSING"}, now, STALE) is True
    # Doc absent
    assert ms._doc_is_stale(None, now, STALE) is False
    print("F2 _doc_is_stale (Bot 2) OK")


def test_f2_bot0_is_stale_processing():
    """Bot 0's _is_stale_processing must respect Bot 2's claim_expires
    lease (so an active Bot 2 job is never ripped out) AND accept Bot 2's
    timestamp shape as a fallback for staleness."""
    # gallery_state imports `db` — provide a stub with just what's touched.
    fake_db = types.ModuleType("db")
    class _H:
        galleries = None
    fake_db.MongoHandle = _H
    sys.modules["db"] = fake_db

    gs = _load("gallery_state.py", "gs_v12_48")

    now = time.time()
    # active Bot 2 lease → NOT stale, even if started_at is old
    doc = {"status": "PROCESSING",
           "claim_expires": now + 300,
           "started_at": now - 10_000}
    assert gs._is_stale_processing(doc) is False, "Bot 2 lease respected"
    # expired lease AND old started_at → stale
    doc = {"status": "PROCESSING",
           "claim_expires": now - 60,
           "started_at": now - 10_000}
    assert gs._is_stale_processing(doc) is True
    # No timestamps → stuck → stale
    assert gs._is_stale_processing({"status": "PROCESSING"}) is True
    # claimed_at fallback used when started_at absent
    assert gs._is_stale_processing(
        {"status": "PROCESSING", "claimed_at": now - 10_000}) is True
    # non-PROCESSING is never stale
    assert gs._is_stale_processing({"status": "COMPLETED"}) is False
    print("F2 Bot 0 _is_stale_processing OK")


# ---------------------------------------------------------------------------
# F3 — Bot 1 sentinel-aware freshness (details_sweeper)
# ---------------------------------------------------------------------------
def test_f3_cache_row_is_fresh():
    # details_sweeper imports scraperbot's own modules; we only need the
    # helper. Load the file text and exec just the two symbols we need to
    # avoid dragging in `from ..config import settings`.
    src = (ROOT / "ScraperBot/app/services/details_sweeper.py").read_text()
    ns: dict = {}
    # Build a minimal namespace: pull in _coerce_epoch and _cache_row_is_fresh
    # from the source text by exec'ing the small block that defines them.
    # (Both are top-level, pure, and depend only on stdlib.)
    exec("from typing import Any, Dict, Optional", ns)
    # extract the two defs by slicing between markers unique to the file
    start = src.index("def _coerce_epoch(")
    end = src.index("async def _gallery_is_fresh(")
    exec(src[start:end], ns)
    cache_row_is_fresh = ns["_cache_row_is_fresh"]

    now = time.time()
    # sentinel: expires_at exactly 0 → fresh
    assert cache_row_is_fresh({"payload": {"a": 1}, "expires_at": 0}, now) is True
    assert cache_row_is_fresh({"payload": {"a": 1}, "expires_at": 0.0}, now) is True
    # future epoch → fresh
    assert cache_row_is_fresh({"payload": "p", "expires_at": now + 100}, now) is True
    # past epoch → expired
    assert cache_row_is_fresh({"payload": "p", "expires_at": now - 100}, now) is False
    # missing expires_at → NOT the sentinel, treated as expired
    assert cache_row_is_fresh({"payload": "p"}, now) is False
    # None expires_at → NOT the sentinel
    assert cache_row_is_fresh({"payload": "p", "expires_at": None}, now) is False
    # non-numeric string "0" → NOT the sentinel (must be int/float)
    assert cache_row_is_fresh({"payload": "p", "expires_at": "0"}, now) is False
    # empty payload → not fresh
    assert cache_row_is_fresh({"payload": None, "expires_at": 0}, now) is False
    assert cache_row_is_fresh({}, now) is False
    assert cache_row_is_fresh(None, now) is False
    print("F3 _cache_row_is_fresh OK")


# ---------------------------------------------------------------------------
# F5 — note_bot2_no_images no longer creates status-less orphan docs
# ---------------------------------------------------------------------------
class _FakeColl:
    """Minimal in-memory collection covering the ops mongo_state uses."""
    def __init__(self):
        self.docs: dict = {}
        self.ops: list = []

    def _match(self, doc, filt):
        for k, v in filt.items():
            if isinstance(v, dict) and "$lt" in v:
                if float(doc.get(k, 0) or 0) >= v["$lt"]:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def find_one_and_update(self, filt, update, upsert=False, return_document=None):
        self.ops.append(("faou", filt, update, upsert))
        doc = None
        for d in self.docs.values():
            if self._match(d, filt):
                doc = d
                break
        if doc is None:
            if not upsert:
                return None
            doc = {"_id": filt.get("_id")}
            self.docs[doc["_id"]] = doc
        set_ = update.get("$set") or {}
        inc = update.get("$inc") or {}
        for k, v in set_.items():
            doc[k] = v
        for k, v in inc.items():
            doc[k] = int(doc.get(k, 0) or 0) + int(v)
        return doc

    def update_one(self, filt, update, upsert=False):
        self.ops.append(("u1", filt, update, upsert))
        for d in list(self.docs.values()):
            if self._match(d, filt):
                set_ = update.get("$set") or {}
                unset = update.get("$unset") or {}
                add = update.get("$addToSet") or {}
                for k, v in set_.items():
                    d[k] = v
                for k in unset:
                    d.pop(k, None)
                for k, v in add.items():
                    d.setdefault(k, [])
                    if v not in d[k]:
                        d[k].append(v)
                return
        if upsert:
            _id = filt.get("_id")
            d = {"_id": _id}
            for k, v in (update.get("$set") or {}).items():
                d[k] = v
            self.docs[_id] = d

    def find_one(self, filt, proj=None):
        for d in self.docs.values():
            ok = True
            for k, v in filt.items():
                if d.get(k) != v:
                    ok = False
                    break
            if ok:
                return dict(d)
        return None

    def delete_one(self, filt):
        for k, d in list(self.docs.items()):
            ok = all(d.get(kk) == vv for kk, vv in filt.items())
            if ok:
                self.docs.pop(k)
                class R: deleted_count = 1
                return R()
        class R: deleted_count = 0
        return R()

    def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError
        if doc["_id"] in self.docs:
            raise DuplicateKeyError("dup")
        self.docs[doc["_id"]] = dict(doc)


def test_f5_note_bot2_no_images_no_orphan_doc():
    ms = _load("Bot2Fetcher/app/mongo_state.py", "b2_mongo_state_f5")
    class _FakeDB:
        def __getitem__(self, name):
            if not hasattr(self, "_c"):
                self._c = {}
            return self._c.setdefault(name, _FakeColl())
    db = _FakeDB()
    g = ms.Galleries(db, stale_s=900)

    # Case A: no doc at all — pre-fix, upsert=True created a status-less
    # stub. Post-fix: mark_failed writes a proper FAILED_BOT2 tombstone.
    verdict = g.note_bot2_no_images("999", "no images", max_retries=3)
    assert verdict == "skip"
    doc = g.coll.find_one({"_id": "999"})
    assert doc is not None
    assert doc.get("status") == ms.STATUS_FAILED_BOT2, doc
    print("F5 note_bot2_no_images: no PROCESSING → FAILED_BOT2 tombstone OK")

    # Case B: doc exists and is PROCESSING — counter increments, doc stays
    # (attempt 1 < max_retries=3).
    g.coll.docs.clear()
    now = time.time()
    g.coll.docs["1"] = {
        "_id": "1", "status": ms.STATUS_PROCESSING, "source": "bot2fetcher",
        "started_at": now, "claim_expires": now + 900, "created_at": now,
    }
    verdict = g.note_bot2_no_images("1", "no images 1", max_retries=3)
    assert verdict == "retry"
    d = g.coll.find_one({"_id": "1"})
    assert d["bot2_no_images_count"] == 1
    assert d["status"] == ms.STATUS_PROCESSING     # doc kept
    assert d["claim_expires"] == 0                  # released as stale
    assert d["started_at"] == 0.0                   # F2: Bot 0 clock too
    print("F5 PROCESSING attempt 1 → retry, claim released OK")


# ---------------------------------------------------------------------------
# F6 — Doc-shape convergence: mark_completed stamps the union of fields
# ---------------------------------------------------------------------------
def test_f6_mark_completed_union_shape():
    ms = _load("Bot2Fetcher/app/mongo_state.py", "b2_mongo_state_f6")
    class _FakeDB:
        def __getitem__(self, name):
            if not hasattr(self, "_c"):
                self._c = {}
            return self._c.setdefault(name, _FakeColl())
    db = _FakeDB()
    g = ms.Galleries(db, stale_s=900)
    # Seed a Bot 0 fresh doc (dedup_check-style)
    now = time.time()
    g.coll.docs["7"] = {
        "_id": "7", "status": ms.STATUS_PROCESSING, "source": "bot0",
        "gallery_id": "7", "url_hash": "abc", "requested_by": [123],
        "started_at": now, "created_at": now,
    }
    g.mark_completed(
        "7", title="hello", cover_msg_id=101, pdf_msg_id=102,
        open_link="https://t.me/c/1/2", pages=42,
        cover_url="https://t.nhentai.net/galleries/9/cover.jpg",
        tags=[{"name": "sole female", "type": "tag"}],
        requested_by=456,
    )
    d = g.coll.docs["7"]
    # Delivery contract (already worked pre-fix)
    for k in ("db_cover_msg_id", "db_pdf_msg_id", "open_link", "pages",
              "title", "source", "completed_at", "updated_at",
              "status", "gallery_id"):
        assert k in d, f"missing {k} in {d}"
    # F6 additions
    assert d["cover_url"].startswith("https://t.nhentai.net/")
    assert d["tags"][0]["name"] == "sole female"
    assert 123 in d["requested_by"] and 456 in d["requested_by"], \
        "requested_by must be $addToSet'd, never clobbered"
    print("F6 mark_completed union shape OK")


def test_f6_claim_stamps_shared_fields():
    ms = _load("Bot2Fetcher/app/mongo_state.py", "b2_mongo_state_f6b")
    class _FakeDB:
        def __getitem__(self, name):
            if not hasattr(self, "_c"):
                self._c = {}
            return self._c.setdefault(name, _FakeColl())
    db = _FakeDB()
    g = ms.Galleries(db, stale_s=900)
    v = g.claim("42")
    assert v == "claimed"
    d = g.coll.docs["42"]
    # Bot 2 own fields
    for k in ("source", "claimed_at", "claim_expires"):
        assert k in d
    # F6 shared fields (previously Bot 0 only)
    for k in ("gallery_id", "url_hash", "started_at", "requested_by"):
        assert k in d, f"F6 shared field missing: {k}"
    assert d["gallery_id"] == "42"
    print("F6 claim() shared-fields shape OK")


def test_f2_reclaim_missing_claim_expires():
    """The livelock: Bot 0 wrote PROCESSING docs with no claim_expires,
    Bot 2's pre-fix claim() only reclaimed on claim_expires<now, so 0
    docs were ever adopted. Guard the fix."""
    ms = _load("Bot2Fetcher/app/mongo_state.py", "b2_mongo_state_f2c")
    class _FakeDB:
        def __getitem__(self, name):
            if not hasattr(self, "_c"):
                self._c = {}
            return self._c.setdefault(name, _FakeColl())
    db = _FakeDB()
    g = ms.Galleries(db, stale_s=900)

    now = time.time()
    # Bot 0 shape stuck doc: no claim_expires, very old started_at
    g.coll.docs["100"] = {
        "_id": "100", "status": ms.STATUS_PROCESSING,
        "source": "bot0", "started_at": now - 10_000,
        "created_at": now - 10_000, "gallery_id": "100",
    }
    v = g.claim("100")
    assert v == "claimed", "Bot 2 must be able to reclaim Bot 0's stuck doc"
    d = g.coll.docs["100"]
    assert d["source"] == "bot2fetcher"
    assert d["claim_expires"] > now
    assert d["started_at"] >= now - 1
    print("F2 cross-writer reclaim (Bot0 stuck → Bot 2 claim) OK")


if __name__ == "__main__":
    test_f1_extract_ids_from_search_payload()
    test_f1_regression_canonical_list_produces_ids()
    test_f2_doc_is_stale()
    test_f2_bot0_is_stale_processing()
    test_f3_cache_row_is_fresh()
    test_f5_note_bot2_no_images_no_orphan_doc()
    test_f6_mark_completed_union_shape()
    test_f6_claim_stamps_shared_fields()
    test_f2_reclaim_missing_claim_expires()
    print("\nALL v12.48 sync-audit tests PASS")
