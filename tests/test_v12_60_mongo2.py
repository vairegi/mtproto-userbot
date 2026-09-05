"""v12.60 / v1.30 — Mongo-2 dual-write + Mongo-first read — unit tests.
Pure Python, no network. Run from repo root: python3 tests/test_v12_60_mongo2.py
"""
from __future__ import annotations
import importlib, importlib.util, os, pathlib, sys, types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FAILED = []

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: FAILED.append(name)

# ---- fake pymongo BEFORE importing the client ----
class FakeColl:
    def __init__(self): self.docs = {}; self.indexes = []
    def create_index(self, field, **kw): self.indexes.append((field, kw))
    def replace_one(self, filt, doc, upsert=False): self.docs[filt["_id"]] = doc; return True
    def find_one(self, filt, proj=None):
        d = self.docs.get(filt.get("_id"))
        if not d: return None
        return {k: v for k, v in d.items() if not proj or k in proj or k == "_id"}
    def find(self, filt=None, proj=None):
        rx = (filt or {}).get("_id", {})
        out = []
        for k, d in self.docs.items():
            if isinstance(rx, dict):
                if "$regex" in rx and not k.startswith("gallery:"): continue
                if "$ne" in rx and k == rx["$ne"]: continue
            out.append(d)
        class Cur(list):
            def limit(self, n): return Cur(out[:n])
        return Cur(out)

FAKE = FakeColl()
class FakeDB:
    def __getitem__(self, coll_name):
        return FAKE
class FakeClient:
    def __getitem__(self, db):
        return FakeDB()
    def close(self): ...

pm = types.ModuleType("pymongo")
pm.MongoClient = lambda *a, **k: FakeClient()
pm.ReplaceOne = lambda *a, **k: None
sys.modules["pymongo"] = pm

m2 = importlib.import_module("common.mongo2_client")

# ---- env resolution ----
os.environ.pop("MONGO2_URI", None); os.environ.pop("MONGO2_BACKUP_URI", None)
os.environ.pop("2NDMONGO_BACKUP_TURSO", None)
check("uri: unset -> empty", m2.mongo2_uri() == "")
os.environ["MONGO2_BACKUP_URI"] = "mongodb+srv://x"
check("uri: MONGO2_BACKUP_URI accepted", m2.mongo2_uri() == "mongodb+srv://x")
os.environ["MONGO2_URI"] = "mongodb+srv://primary"
check("uri: MONGO2_URI wins", m2.mongo2_uri() == "mongodb+srv://primary")

# ---- read/write gates ----
os.environ["MONGO2_READS"] = "1"; os.environ["MONGO2_WRITES"] = "1"
check("reads default on", m2.reads_enabled())
check("writes default on", m2.writes_enabled())
os.environ["MONGO2_READS"] = "0"
check("MONGO2_READS=0 -> Turso-only rollback", not m2.reads_enabled())
os.environ["MONGO2_READS"] = "1"

# ---- TTL index ensured once ----
m2._ready = False
ok = m2.put("gallery:1", '{"id":1}', expires_at=9999999999, ttl_sec=86400)
check("put: upsert ok", ok is True)
check("put: TTL index created (partial, expires_at>0)",
      any(f == "expires_at" and kw.get("expireAfterSeconds") == 0
          and kw.get("partialFilterExpression") == {"expires_at": {"$gt": 0}}
          for f, kw in FAKE.indexes))
check("put: doc stored with key fields",
      FAKE.docs["gallery:1"]["expires_at"] == 9999999999
      and FAKE.docs["gallery:1"]["_id"] == "gallery:1")

# ---- never-expire sentinel ----
m2.put("search:popular:page1", "[]", expires_at=0, ttl_sec=0)
check("put: expires_at=0 sentinel preserved (never-expire)",
      FAKE.docs["search:popular:page1"]["expires_at"] == 0)

# ---- reads ----
r = m2.get("gallery:1")
check("get: HIT returns payload", r and r["payload"] == '{"id":1}')
check("get: MISS returns None (Turso fallback signal)", m2.get("gallery:nope") is None)
os.environ["MONGO2_WRITES"] = "0"
check("writes=0 -> put skipped", m2.put("gallery:2", "{}", 0, 0) is False
      and "gallery:2" not in FAKE.docs)
os.environ["MONGO2_WRITES"] = "1"

# ---- similar_mongo scoring on the fake collection ----
import json as _json
for gid, payload in {
    "100": {"id": 100, "title": "A", "cover": "c", "pages": 10,
            "tag_groups": {"artist": ["x-artist"], "tag": ["sole female", "big breasts"]}},
    "101": {"id": 101, "title": "B", "cover": "c", "pages": 20,
            "tag_groups": {"artist": ["x-artist"], "tag": ["sole female"]}},
    "102": {"id": 102, "title": "C", "cover": "c", "pages": 30,
            "tag_groups": {"artist": ["other"], "tag": ["males only"]}},
}.items():
    FAKE.docs[f"gallery:{gid}"] = {"_id": f"gallery:{gid}", "payload": _json.dumps(payload)}

os.environ.pop("SIMILAR_ENABLED", None)
smpath = ROOT / "miniapp/backend/app/services/similar_mongo.py"
spec = importlib.util.spec_from_file_location("similar_mongo", smpath)
sm = importlib.util.module_from_spec(spec); spec.loader.exec_module(sm)
res = sm.similar_galleries("100", limit=6)
ids = [c["id"] for c in res]
check("similar: same-artist gallery ranks first", ids and ids[0] == 101)
check("similar: unrelated gallery excluded (score 0)", 102 not in ids)
check("similar: target itself excluded", 100 not in ids)
os.environ["SIMILAR_ENABLED"] = "0"
check("similar: SIMILAR_ENABLED=0 disables", sm.similar_galleries("100") == [])
os.environ.pop("SIMILAR_ENABLED", None)

# ---- route gate source checks ----
src = (ROOT / "miniapp/backend/app/routes/suggestions.py").read_text()
check("route: default ON", '"SIMILAR_ENABLED", "1"' in src)
check("route: mongo engine default + turso fallback",
      "SIMILAR_SOURCE" in src and "similar_mongo" in src and "gallery_suggestions" in src)

print("-" * 60)
if FAILED: print(f"FAILED {len(FAILED)}: {FAILED}"); sys.exit(1)
print("ALL v12.60 CHECKS PASSED")
