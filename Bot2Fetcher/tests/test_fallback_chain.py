"""v12.49 fallback-chain unit tests — pure fakes, no network.

Run from repo root:  python3 Bot2Fetcher/tests/test_fallback_chain.py
"""
from __future__ import annotations
import asyncio, pathlib, sys, time, types

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
B2 = ROOT / "Bot2Fetcher"
sys.path.insert(0, str(B2))

import importlib  # noqa: E402
ms = importlib.import_module("app.mongo_state")
ft = importlib.import_module("app.fetcher")


# --------------------------------------------------------------------------
# Fake Mongo collection — supports the operators our code actually uses
# --------------------------------------------------------------------------
class FakeColl:
    def __init__(self):
        self.docs = {}

    @staticmethod
    def _match(doc, filt):
        for k, v in filt.items():
            if k == "$or":
                if not any(FakeColl._match(doc, sub) for sub in v):
                    return False
                continue
            if isinstance(v, dict):
                if "$lt" in v:
                    dv = doc.get(k)
                    try:
                        if dv is None or float(dv) >= v["$lt"]:
                            return False
                    except (TypeError, ValueError):
                        return False
                elif "$exists" in v:
                    if (k in doc) != bool(v["$exists"]):
                        return False
                else:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def find_one(self, filt, proj=None):
        for d in self.docs.values():
            if self._match(d, filt):
                return dict(d)
        return None

    def find_one_and_update(self, filt, update, upsert=False, return_document=None):
        target = None
        for d in self.docs.values():
            if self._match(d, filt):
                target = d
                break
        if target is None:
            if not upsert:
                return None
            _id = filt.get("_id")
            if _id in self.docs:
                return None  # lost insert race
            target = {"_id": _id}
            self.docs[_id] = target
        for k, v in (update.get("$set") or {}).items():
            target[k] = v
        for k in (update.get("$unset") or {}):
            target.pop(k, None)
        return target

    def update_one(self, filt, update, upsert=False):
        for d in self.docs.values():
            if self._match(d, filt):
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
                for k in (update.get("$unset") or {}):
                    d.pop(k, None)
                return
        if upsert:
            _id = filt.get("_id")
            d = {"_id": _id}
            for k, v in (update.get("$set") or {}).items():
                d[k] = v
            self.docs[_id] = d

    def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError
        if doc["_id"] in self.docs:
            raise DuplicateKeyError("dup")
        self.docs[doc["_id"]] = dict(doc)

    def delete_one(self, filt):
        for k, d in list(self.docs.items()):
            if self._match(d, filt):
                self.docs.pop(k)
                return types.SimpleNamespace(deleted_count=1)
        return types.SimpleNamespace(deleted_count=0)


class FakeDB:
    def __init__(self):
        self._c = {}
    def __getitem__(self, name):
        return self._c.setdefault(name, FakeColl())


def _galleries():
    return ms.Galleries(FakeDB(), stale_s=900)


# --------------------------------------------------------------------------
# 1) claim_ex — legacy permanent-skip retry + fallback routing
# --------------------------------------------------------------------------
def test_claim_ex_legacy_failed_bot2():
    g = _galleries()
    # legacy v12.42-style permanent skip: no both_failed_at, old updated_at
    g.coll.docs["100"] = {"_id": "100", "status": ms.STATUS_FAILED_BOT2,
                          "error": "no images x3", "updated_at": time.time() - 90000}
    decision, _ = g.claim_ex("100")
    assert decision == "claimed_fallback", decision
    d = g.coll.docs["100"]
    assert d["status"] == ms.STATUS_PROCESSING and d["fallback_pending"] is True
    print("claim_ex: legacy FAILED_BOT2_ERROR → claimed_fallback OK")

    # fresh double-failure (parked <12h) → still failed
    g2 = _galleries()
    g2.coll.docs["200"] = {"_id": "200", "status": ms.STATUS_FAILED_BOT2,
                           "both_failed_at": time.time() - 3600,
                           "both_fail_park_s": 43200}
    decision, _ = g2.claim_ex("200")
    assert decision == "failed", decision
    print("claim_ex: parked <12h FAILED_BOT2_ERROR → failed OK")

    # parked PROCESSING doc with fallback_pending, park expired → fallback route
    g3 = _galleries()
    old = time.time() - 43200 - 1000
    g3.coll.docs["300"] = {"_id": "300", "status": ms.STATUS_PROCESSING,
                           "source": "bot2fetcher", "fallback_pending": True,
                           "claim_expires": old, "started_at": old,
                           "claimed_at": old, "created_at": old}
    decision, _ = g3.claim_ex("300")
    assert decision == "claimed_fallback", decision
    print("claim_ex: expired parked PROCESSING → claimed_fallback OK")

    # brand-new gid → claimed_new
    g4 = _galleries()
    decision, _ = g4.claim_ex("400")
    assert decision == "claimed_new", decision
    print("claim_ex: new gid → claimed_new OK")


# --------------------------------------------------------------------------
# 2) set_both_failed — park semantics
# --------------------------------------------------------------------------
def test_set_both_failed():
    g = _galleries()
    now = time.time()
    g.coll.docs["500"] = {"_id": "500", "status": ms.STATUS_PROCESSING,
                          "source": "bot2fetcher", "claimed_at": now,
                          "claim_expires": now + 900, "started_at": now,
                          "created_at": now}
    g.set_both_failed("500", primary_err="An error occurred: No images found",
                      fallback_err="no reply within 300s", park_s=43200)
    d = g.coll.docs["500"]
    assert d["fallback_pending"] is True
    assert d["both_failed_at"] > 0
    assert d["claim_expires"] > now + 43000 and d["started_at"] > now + 43000
    assert "No images" in d["primary_last_error"]
    # during the park, claim_ex must say busy (lease not expired)
    decision, _ = g.claim_ex("500")
    assert decision == "busy", decision
    print("set_both_failed: park + busy-during-park OK")


# --------------------------------------------------------------------------
# 3) FallbackLease — single owner across "processes"
# --------------------------------------------------------------------------
def test_fallback_lease_single_owner():
    db = FakeDB()
    lease = ms.FallbackLease(db, ttl_s=2)
    c1, c2 = object(), object()
    assert lease.acquire(1, c1) is True
    assert lease.acquire(2, c2) is False           # held by slot 1
    lease.release(1, c1)
    assert lease.acquire(2, c2) is True            # released → slot 2 wins
    # self-expiry
    lease2 = ms.FallbackLease(FakeDB(), ttl_s=0)
    # ttl 0 → expires immediately → anyone can take it
    assert lease2.acquire(1, c1) is True
    assert lease2.acquire(2, c2) is True
    print("FallbackLease: single-owner + release + expiry OK")


# --------------------------------------------------------------------------
# 4) _request_pdf against a scripted fake Telethon client
# --------------------------------------------------------------------------
class FakeMsg:
    def __init__(self, mid, text="", doc=False, out=False):
        self.id = mid
        self.raw_text = text
        self.document = object() if doc else None
        self.out = out


class FakeClient:
    def __init__(self, script):
        self.script = script   # entity -> list of msgs to return after send
        self.sent = []
        self._next_id = 1000
    async def send_message(self, entity, text):
        self._next_id += 1
        self.sent.append((entity, text))
        return FakeMsg(self._next_id, out=True)
    async def get_messages(self, entity, limit=15):
        return list(self.script.get(entity, []))


def _mk_fetcher():
    s = types.SimpleNamespace(
        bot2_username="Gallery_DLBot", fallback_username="pdfdownloadcinbot",
        fallback_timeout_s=0.4, both_fail_park_s=43200, fallback_lease_s=420,
        log_bot_token="", log_channel_id="",
        fetch_gap_min=0, fetch_gap_max=0, db_channel_id="-1001",
    )
    f = ft.Fetcher(s, galleries=None, turso=None, stats=ft.Stats(), dashboard=None)
    return f


def test_request_pdf_paths():
    ft.POLL_EVERY_S = 0.05  # speed up polling for tests
    f = _mk_fetcher()
    # document → returned
    cl = FakeClient({"B": [FakeMsg(9001, doc=True)]})
    r = asyncio.run(f._request_pdf(cl, "B", "1", 1.0))
    assert not isinstance(r, str) and r is not None and r.document is not None
    # progress text then document
    cl2 = FakeClient({"B": [FakeMsg(9001, text="Downloading…"),
                            FakeMsg(9002, doc=True)]})
    r = asyncio.run(f._request_pdf(cl2, "B", "1", 1.0))
    assert not isinstance(r, str) and r is not None
    # error text → str returned
    cl3 = FakeClient({"B": [FakeMsg(9001, text="An error occurred: No images found after download or ZIP extraction.")]})
    r = asyncio.run(f._request_pdf(cl3, "B", "1", 1.0))
    assert isinstance(r, str) and "No images found" in r
    # silence → None (timeout)
    cl4 = FakeClient({"B": []})
    r = asyncio.run(f._request_pdf(cl4, "B", "1", 0.2))
    assert r is None
    # msg-id floor: document OLDER than our sent msg must be ignored
    cl5 = FakeClient({"B": [FakeMsg(5, doc=True)]})  # stale doc from prior job
    r = asyncio.run(f._request_pdf(cl5, "B", "1", 0.2))
    assert r is None
    print("_request_pdf: doc/progress/error/timeout/stale-floor OK")


# --------------------------------------------------------------------------
# 5) _try_fallback_pdf — serialization across two slots (one at a time)
# --------------------------------------------------------------------------
def test_fallback_single_flight():
    f = _mk_fetcher()
    f._fallback_per_slot = {1: "FB", 2: "FB"}
    f.fallback_lease = ms.FallbackLease(FakeDB(), ttl_s=60)
    order = []

    class SlowClient(FakeClient):
        async def send_message(self, entity, text):
            r = await super().send_message(entity, text)
            order.append(("send", entity))
            return r
        async def get_messages(self, entity, limit=15):
            await asyncio.sleep(0.15)          # fallback bot is slow
            return list(self.script.get(entity, []))

    # both slots' scripted reply: a document (success)
    script = {"FB": [FakeMsg(9000, doc=True)]}
    c1, c2 = SlowClient(script), SlowClient(script)

    async def job(idx, client):
        msg, err = await f._try_fallback_pdf(idx, client, "900", "primary err")
        assert msg is not None, err
        order.append(("done", idx))

    async def main():
        await asyncio.gather(job(1, c1), job(2, c2))

    t0 = time.monotonic()
    asyncio.run(main())
    # each fallback wait ~0.2s+; serialized → total >= 2x serial floor
    assert time.monotonic() - t0 >= 0.35
    sends = [e for e in order if e[0] == "send"]
    assert len(sends) == 2
    # event-order check: sends happen INSIDE the lock, dones AFTER release —
    # strict alternation proves zero overlap (job-entry times legitimately
    # coincide since both slots start together).
    sends = [i for i, e in enumerate(order) if e[0] == "send"]
    dones = [i for i, e in enumerate(order) if e[0] == "done"]
    assert len(sends) == 2 and len(dones) == 2
    assert dones[0] < sends[1], f"second fallback DM overlapped first: {order}"
    print("fallback single-flight (lock + lease) OK")


def test_fallback_failure_result():
    f = _mk_fetcher()
    f._fallback_per_slot = {1: "FB"}
    f.fallback_lease = ms.FallbackLease(FakeDB(), ttl_s=60)
    cl = FakeClient({"FB": [FakeMsg(9000, text="❌ cannot process this")]})
    msg, err = asyncio.run(f._try_fallback_pdf(1, cl, "901", "primary"))
    assert msg is None and "cannot process" in err
    cl2 = FakeClient({"FB": []})
    msg, err = asyncio.run(f._try_fallback_pdf(1, cl2, "902", "primary"))
    assert msg is None and "no reply" in err
    print("fallback error/timeout result OK")


if __name__ == "__main__":
    test_claim_ex_legacy_failed_bot2()
    test_set_both_failed()
    test_fallback_lease_single_owner()
    test_request_pdf_paths()
    test_fallback_single_flight()
    test_fallback_failure_result()
    print("\nALL v12.49 fallback-chain tests PASS")
