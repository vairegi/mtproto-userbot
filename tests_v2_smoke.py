"""
tests_v2_smoke.py — offline smoke tests for the V2 architecture.

Runs entirely in-process. No Mongo, no Telegram, no network. Fakes are
provided for the two dependencies gallery_state / relay_v2 / cover_poster /
bot2_client actually reach for:

  * `db.MongoHandle` — replaced with `FakeMongo`, a dict-of-dicts store
    that exposes the same `find_one_and_update` / `update_one` /
    `find_one` / `insert_one` / `delete_one` semantics we depend on.
  * `TelegramClient` — replaced with `FakeClient`, a coroutine surface
    that records `send_message` / `send_file` / `forward_messages` /
    `delete_messages` / `iter_messages` calls.
  * `hf_scraper.fetch_gallery_meta` — replaced with a canned
    `GalleryMeta` return value.

Usage:
    python3 tests_v2_smoke.py

Exits 0 if every assertion passes, non-zero on the first failure.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from typing import Any, Dict

# Make the project root importable regardless of CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


# ---------------------------------------------------------------------------
# Fake Mongo — a minimal in-memory pymongo-compatible collection.
# ---------------------------------------------------------------------------

class FakeCollection:
    def __init__(self):
        self.docs: Dict[str, Dict[str, Any]] = {}

    def find_one(self, filt):
        for d in self.docs.values():
            if _matches(d, filt):
                return dict(d)
        return None

    def insert_one(self, doc):
        _id = doc["_id"]
        if _id in self.docs:
            raise Exception("DuplicateKey")
        self.docs[_id] = dict(doc)

    def update_one(self, filt, upd, upsert=False):
        for d in self.docs.values():
            if _matches(d, filt):
                _apply_update(d, upd)
                return
        if upsert:
            new = {}
            _apply_update(new, upd)
            if "_id" not in new and "_id" in filt:
                new["_id"] = filt["_id"]
            self.docs[new["_id"]] = new

    def find_one_and_update(self, filt, upd, return_document=True):
        for d in self.docs.values():
            if _matches(d, filt):
                _apply_update(d, upd)
                return dict(d) if return_document else None
        return None

    def delete_one(self, filt):
        for k, d in list(self.docs.items()):
            if _matches(d, filt):
                del self.docs[k]
                return types.SimpleNamespace(deleted_count=1)
        return types.SimpleNamespace(deleted_count=0)

    def create_index(self, *a, **kw):
        return "idx"

    def aggregate(self, pipeline):
        for stage in pipeline:
            if "$group" in stage:
                counts: Dict[Any, int] = {}
                for d in self.docs.values():
                    counts[d.get("status")] = counts.get(d.get("status"), 0) + 1
                return ({"_id": k, "n": v} for k, v in counts.items())
        return iter([])

    def find(self, filt=None, projection=None):
        return [dict(d) for d in self.docs.values() if _matches(d, filt or {})]


def _matches(doc, filt):
    if not filt:
        return True
    if "$or" in filt:
        for sub in filt["$or"]:
            if _matches(doc, sub):
                return True
        return False
    for k, v in filt.items():
        if k == "$or":
            continue
        if isinstance(v, dict):
            if "$in" in v:
                if doc.get(k) not in v["$in"]:
                    return False
            elif "$exists" in v:
                if bool(k in doc) != bool(v["$exists"]):
                    return False
            elif "$lt" in v:
                if not (doc.get(k) is not None and doc[k] < v["$lt"]):
                    return False
            else:
                return False
        else:
            if doc.get(k) != v:
                return False
    return True


def _apply_update(doc, upd):
    if "$set" in upd:
        for k, v in upd["$set"].items():
            doc[k] = v
    if "$addToSet" in upd:
        for k, v in upd["$addToSet"].items():
            lst = doc.setdefault(k, [])
            if v not in lst:
                lst.append(v)


class FakeMongo:
    """Stand-in for db.MongoHandle."""
    def __init__(self):
        self._cols: Dict[str, FakeCollection] = {}

    def _c(self, name):
        return self._cols.setdefault(name, FakeCollection())

    @property
    def galleries(self):      return self._c("galleries")
    @property
    def queue(self):          return self._c("queue")
    @property
    def processed_urls(self): return self._c("processed_urls")

    @property
    def db(self):
        return self

    def command(self, *_a, **_k):
        return {"ok": 1}

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Wire the fakes into `db` BEFORE importing gallery_state.
# ---------------------------------------------------------------------------
import db as _bot_db  # noqa: E402

_FAKE = FakeMongo()

def _fake_connect() -> FakeMongo:
    return _FAKE

_bot_db.connect = _fake_connect
_bot_db.MongoHandle = FakeMongo

# Now import the modules under test.
import gallery_state as gs  # noqa: E402


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

_PASS = 0
_FAIL = 0


def _check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {label}")
    else:
        _FAIL += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def _reset():
    _FAKE._cols.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_extract_gallery_id():
    print("test_extract_gallery_id")
    _check("nhentai url",         gs.extract_gallery_id("https://nhentai.net/g/227910/") == "227910")
    _check("nhentai no trailing", gs.extract_gallery_id("https://nhentai.net/g/227910") == "227910")
    _check("bare id",             gs.extract_gallery_id("227910") == "227910")
    _check("hentaifox prefix",    gs.extract_gallery_id("https://hentaifox.com/gallery/9999/") == "hf_9999")
    _check("empty",               gs.extract_gallery_id("") is None)
    _check("garbage",             gs.extract_gallery_id("hello world") is None)
    _check("mixed prefix",        gs.extract_gallery_id("@alice https://nhentai.net/g/1234/") == "1234")


def test_dedup_gate_new_gallery():
    print("test_dedup_gate_new_gallery")
    _reset()
    conn = _bot_db.connect()
    d = gs.dedup_check(conn, "304307", url="https://nhentai.net/g/304307/",
                       url_hash="h304307", requested_by=42)
    _check("new -> proceed",              d.action == "proceed")
    _check("doc PROCESSING",              conn.galleries.docs["304307"]["status"] == "PROCESSING")
    _check("requester recorded",          42 in conn.galleries.docs["304307"]["requested_by"])


def test_dedup_gate_already_completed():
    print("test_dedup_gate_already_completed")
    _reset()
    conn = _bot_db.connect()
    conn.galleries.docs["500"] = {
        "_id": "500", "gallery_id": "500", "status": "COMPLETED",
        "title": "Old Gallery", "open_link": "https://t.me/c/999/1",
        "db_cover_msg_id": 1, "db_pdf_msg_id": 2,
        "requested_by": [11],
    }
    d = gs.dedup_check(conn, "500", url="", url_hash="", requested_by=99)
    _check("already_completed",           d.action == "already_completed")
    _check("open_link carried",           d.open_link == "https://t.me/c/999/1")
    _check("new requester appended",      99 in conn.galleries.docs["500"]["requested_by"])
    _check("original requester kept",     11 in conn.galleries.docs["500"]["requested_by"])


def test_dedup_gate_already_processing():
    print("test_dedup_gate_already_processing")
    _reset()
    conn = _bot_db.connect()
    conn.galleries.docs["777"] = {
        "_id": "777", "gallery_id": "777", "status": "PROCESSING",
        "started_at": time.time() - 5, "requested_by": [7],
    }
    d = gs.dedup_check(conn, "777", url="", url_hash="", requested_by=8)
    _check("already_processing",          d.action == "already_processing")
    _check("did NOT flip status",         conn.galleries.docs["777"]["status"] == "PROCESSING")


def test_dedup_gate_stale_reset():
    print("test_dedup_gate_stale_reset")
    _reset()
    conn = _bot_db.connect()
    conn.galleries.docs["888"] = {
        "_id": "888", "gallery_id": "888", "status": "PROCESSING",
        "started_at": time.time() - 3600, "requested_by": [],
    }
    d = gs.dedup_check(conn, "888", url="u", url_hash="h", requested_by=1)
    _check("stale_reset",                 d.action == "stale_reset")
    _check("started_at refreshed",        time.time() - conn.galleries.docs["888"]["started_at"] < 5)


def test_dedup_gate_failed_retry():
    print("test_dedup_gate_failed_retry")
    _reset()
    conn = _bot_db.connect()
    conn.galleries.docs["1000"] = {
        "_id": "1000", "gallery_id": "1000", "status": "FAILED_TIMEOUT",
        "failed_reason": "prior timeout", "requested_by": [],
    }
    d = gs.dedup_check(conn, "1000", url="u", url_hash="h", requested_by=1)
    _check("FAILED_TIMEOUT -> proceed",   d.action == "proceed")
    _check("flipped to PROCESSING",       conn.galleries.docs["1000"]["status"] == "PROCESSING")

    _reset()
    conn2 = _bot_db.connect()
    conn2.galleries.docs["1001"] = {
        "_id": "1001", "gallery_id": "1001", "status": "FAILED_RECOVERED",
        "failed_reason": "recovered by migration script", "requested_by": [],
    }
    d2 = gs.dedup_check(conn2, "1001", url="u", url_hash="h", requested_by=1)
    _check("FAILED_RECOVERED -> proceed", d2.action == "proceed")


def test_mark_completed_partial_failed():
    print("test_mark_completed / partial / failed")
    _reset()
    conn = _bot_db.connect()
    conn.galleries.docs["222"] = {
        "_id": "222", "gallery_id": "222", "status": "PROCESSING",
        "started_at": time.time(), "requested_by": [1],
    }
    gs.mark_completed(conn, "222", title="T", pages=10,
                      tags=[{"name": "a", "type": "tag"}],
                      cover_url="c", db_cover_msg_id=101, db_pdf_msg_id=102,
                      open_link="https://t.me/c/1/101", job_id=9)
    d = conn.galleries.docs["222"]
    _check("COMPLETED",                   d["status"] == "COMPLETED")
    _check("db_cover_msg_id",             d["db_cover_msg_id"] == 101)
    _check("db_pdf_msg_id",               d["db_pdf_msg_id"] == 102)
    _check("open_link",                   d["open_link"] == "https://t.me/c/1/101")

    conn.galleries.docs["333"] = {"_id": "333", "gallery_id": "333", "status": "PROCESSING"}
    gs.mark_partial(conn, "333", db_pdf_msg_id=5, open_link="l", reason="no cover")
    _check("PARTIAL",                     conn.galleries.docs["333"]["status"] == "PARTIAL")

    conn.galleries.docs["444"] = {"_id": "444", "gallery_id": "444", "status": "PROCESSING"}
    gs.mark_failed(conn, "444", status=gs.STATUS_FAILED_BOT2, reason="bad", purge=True)
    _check("purged doc removed",          "444" not in conn.galleries.docs)

    conn.galleries.docs["555"] = {"_id": "555", "gallery_id": "555", "status": "PROCESSING"}
    gs.mark_failed(conn, "555", status=gs.STATUS_FAILED_TIMEOUT, reason="no PDF", purge=False)
    _check("tombstoned kept",             conn.galleries.docs["555"]["status"] == "FAILED_TIMEOUT")


def test_counts_and_reset_doc():
    print("test_counts_by_status / reset_doc")
    _reset()
    conn = _bot_db.connect()
    for i, st in enumerate(["COMPLETED", "COMPLETED", "PROCESSING",
                             "FAILED_TIMEOUT", "PARTIAL"]):
        conn.galleries.docs[str(i)] = {"_id": str(i), "gallery_id": str(i), "status": st}
    c = gs.counts_by_status(conn)
    _check("COMPLETED count 2",           c.get("COMPLETED") == 2)
    _check("FAILED_TIMEOUT count 1",      c.get("FAILED_TIMEOUT") == 1)

    ok = gs.reset_doc(conn, "0")
    _check("reset_doc returns True",      ok is True)
    _check("doc gone after reset",        "0" not in conn.galleries.docs)


def test_bot2_client_classify_text():
    print("test_bot2_client._classify_text")
    import bot2_client
    _check("error keyword",   bot2_client._classify_text("Sorry, invalid URL") == "error")
    _check("progress msg",    bot2_client._classify_text("Downloading page 12/40") == "progress")
    _check("empty text",      bot2_client._classify_text("") == "progress")
    _check("case insensitive",bot2_client._classify_text("BAD LINK") == "error")


def test_cover_poster_build_open_link():
    print("test_cover_poster.build_open_link")
    import cover_poster as cp
    # The canonical -100XXXXXXXXXX Telegram channel ID -> strip the '100'.
    link = cp.build_open_link(-1001234567890, 42)
    _check("t.me/c -100 form",     link == "https://t.me/c/1234567890/42", f"got {link}")
    # Positive `100XXXXXXXXXX` (as e.g. `abs(-100XXXXXXXXXX)`) -> same result.
    link2 = cp.build_open_link(1001234567890, 7)
    _check("t.me/c 100-prefixed",  link2 == "https://t.me/c/1234567890/7", f"got {link2}")
    # A raw ID that does NOT begin with '100' passes through untouched.
    link3 = cp.build_open_link(1234567890, 5)
    _check("t.me/c raw (no 100)",  link3 == "https://t.me/c/1234567890/5", f"got {link3}")
    # Invalid input -> empty string.
    _check("invalid input",         cp.build_open_link("not-an-int", 3) == "")


def test_hashtagify():
    print("test_cover_poster._hashtagify")
    import cover_poster as cp
    _check("basic",       cp._hashtagify("big breasts") == "#big_breasts")
    _check("empty",       cp._hashtagify("") == "")
    _check("punctuation", cp._hashtagify("sole male!!") == "#sole_male")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    global _PASS, _FAIL
    _PASS = _FAIL = 0
    for t in [
        test_extract_gallery_id,
        test_dedup_gate_new_gallery,
        test_dedup_gate_already_completed,
        test_dedup_gate_already_processing,
        test_dedup_gate_stale_reset,
        test_dedup_gate_failed_retry,
        test_mark_completed_partial_failed,
        test_counts_and_reset_doc,
        test_bot2_client_classify_text,
        test_cover_poster_build_open_link,
        test_hashtagify,
    ]:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            _FAIL += 1
            print(f"  EXCEPTION in {t.__name__}: {e!r}")
    print()
    print(f"summary: {_PASS} passed, {_FAIL} failed")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
