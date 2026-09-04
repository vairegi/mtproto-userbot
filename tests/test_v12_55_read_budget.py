"""v12.55 Turso read-budget — unit tests (pure Python, no network).

Run from repo root:  python3 tests/test_v12_55_read_budget.py

Covers:
  1. _turso_key_for maps typed-search keys to the canonical row
     (no more search:search:* probes or writes)
  2. search() accepts a canonical LIST payload (WARN fix) and only
     probes the sort=date variant (single Turso read per page)
  3. Bot2 list_gallery_ids memo: second call within the TTL window
     performs ZERO additional Turso executes
  4. ensure_schema creates the cached_at index
"""
from __future__ import annotations

import asyncio, importlib, os, pathlib, sys, types

ROOT = pathlib.Path(__file__).resolve().parent.parent
B2 = ROOT / "Bot2Fetcher"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(B2))

FAILED = []

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)

# --- stubs for hf_scraper deps -------------------------------------------
_stub_log = types.ModuleType("logging_setup")
_stub_log.setup_logging = lambda name: __import__("logging").getLogger(name)
sys.modules["logging_setup"] = _stub_log

hf = importlib.import_module("hf_scraper")

# ---- 1) _turso_key_for ----
k = hf._turso_key_for("search:Sister  Pregnant:p3")
check("turso_key_for: typed search -> canonical",
      k == "search:q=sister pregnant|sort=date|page=3")
check("turso_key_for: gallery passthrough",
      hf._turso_key_for("gallery|12345") == "gallery:12345")
check("turso_key_for: non-typed search unchanged",
      hf._turso_key_for("search|q=x|sort=date|page=1") == "search:search|q=x|sort=date|page=1")

# ---- 2) search() with canonical list payload -----------------------------
# Fake the Turso cache module so the warm read returns a canonical list.
CANON_LIST = [{"id": 111, "title_en_clean": "Warm Title",
               "cover": "https://t.nhentai.net/galleries/1/cover.jpg",
               "pages": 20, "tags": ["sole female"]},
              {"id": 222, "title": "Second Title",
               "cover": "", "pages": 10, "tags": []}]

class _FakeNHC:
    reads = []
    def get(self, key, allow_stale=False):
        _FakeNHC.reads.append(key)
        return CANON_LIST
    def ttl_for_key(self, k): return 999
    def put(self, k, v): return True

hf._turso_cache_module = lambda: _FakeNHC()
os.environ.pop("NHENTAI_API_KEY", None)

page = asyncio.run(hf.search("Sister Pregnant", page=3))
check("search(): list payload normalised (no WARN path)", page is not None)
check("search(): 2 hits from canonical list", page and len(page.hits) == 2)
check("search(): title from title_en_clean", page and page.hits[0].title == "Warm Title")
check("search(): only sort=date probed (single read)",
      _FakeNHC.reads == ["search:q=sister pregnant|sort=date|page=3"])
check("search(): no legacy search:search key touched",
      all(not r.startswith("search:search:") for r in _FakeNHC.reads))

# dict payload still works (raw upstream envelope)
class _FakeNHC2(_FakeNHC):
    def get(self, key, allow_stale=False): return None
hf._turso_cache_module = lambda: _FakeNHC2()
hf._cache = {}  # clear L1
hf._inflight = {}

async def _fake_http(path, params):
    return {"result": [{"id": 555, "english_title": "Fresh",
                        "thumbnail": "https://x/t.jpg", "tag_ids": [12227],
                        "num_pages": 5}],
            "num_pages": 5, "per_page": 25, "total": 125}
hf._http_get_json = _fake_http
page2 = asyncio.run(hf.search("newquery", page=1))
check("search(): dict envelope still works", page2 is not None and len(page2.hits) == 1)
check("search(): english filter still applies to raw rows",
      page2 and page2.hits[0].gallery_id == "555")

# ---- 3) Bot2 list_gallery_ids memo ---------------------------------------
ts = importlib.import_module("app.turso_store")

class _SpyTurso(ts.Turso):
    def __init__(self):
        super().__init__("https://x.turso.io", "tok")
        self.calls = 0
    async def execute(self, sql, args=None):
        self.calls += 1
        return {"rows": [{"key": "gallery:1", "cached_at": 100}]}

t = _SpyTurso()
t._list_gallery_watermark = 0
r1 = asyncio.run(t.list_gallery_ids())
calls_after_first = t.calls
r2 = asyncio.run(t.list_gallery_ids())
check("Bot2 memo: first scan hit Turso", calls_after_first >= 1)
check("Bot2 memo: second call within TTL = ZERO extra Turso executes",
      t.calls == calls_after_first)
check("Bot2 memo: cached rows returned", [g["gid"] for g in r2] == ["1"])

# ---- 4) ensure_schema creates cached_at index ----------------------------
schema_sql = []
class _SpyTurso2(ts.Turso):
    async def execute(self, sql, args=None):
        schema_sql.append(sql)
        return {"rows": []}
t2 = _SpyTurso2("https://x.turso.io", "tok")
asyncio.run(t2.ensure_schema())
check("ensure_schema: cached_at index created",
      any("idx_nhentai_cache_cached_at" in s for s in schema_sql))

print("-" * 60)
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}"); sys.exit(1)
print("ALL v12.55 CHECKS PASSED")
