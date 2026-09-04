"""v1.27 discovery counting + digest window — pure Python, no network.
Run from repo root:  python3 ScraperBot/tests/test_v1_27_discovery.py
"""
from __future__ import annotations
import asyncio, importlib, pathlib, sys, time, types

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SB = ROOT / "ScraperBot"
sys.path.insert(0, str(SB))

FAILED = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: FAILED.append(name)

class FakeState:
    def __init__(self): self.store = {}
fake = FakeState()

settings = types.SimpleNamespace(
    admin_user_ids=[111], bot_token="tok", digest_enabled=True,
    digest_time_ist="10:00", details_per_tick=5, details_rest_sec=0,
    list_bucket_skip_wait_sec=0, list_429_sleep_cap_sec=120.0,
)
app_pkg = types.ModuleType("app"); app_pkg.__path__ = [str(SB / "app")]
sys.modules["app"] = app_pkg
cfg = types.ModuleType("app.config"); cfg.settings = settings
sys.modules["app.config"] = cfg
mc = types.ModuleType("app.mongo_client")
mc.state_get = lambda k, d=None: fake.store.get(k, d)
mc.state_set = lambda k, v: fake.store.__setitem__(k, v)
sys.modules["app.mongo_client"] = mc

class _DummyClient:
    def __init__(self, *a, **k): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
_stub_httpx = types.ModuleType("httpx")
_stub_httpx.AsyncClient = _DummyClient
_stub_httpx.HTTPError = Exception
sys.modules["httpx"] = _stub_httpx

dash = importlib.import_module("app.services.channel_dashboard")
dig = importlib.import_module("app.services.discovery_digest")

# ---- 1) batch recorder feeds all counters in ONE call --------------------
fake.store.clear()
dash.record_new_galleries_on_page("popular-today", 2, ["101", "102", "103"])
c = fake.store.get("dash_counters", {})
ring = c.get("per_page_new") or []
check("batch recorder: 3 ring entries", len(ring) == 3)
check("batch recorder: correct sort/page",
      all(e[0] == "popular-today" and e[1] == 2 for e in ring))
check("batch recorder: per_sort=3", c.get("per_sort", {}).get("popular-today") == 3)
check("batch recorder: new_galleries=3", c.get("new_galleries") == 3)
t = fake.store.get("dash_totals", {})
check("batch recorder: ring_24h=3", len(t.get("ring_24h") or []) == 3)
check("batch recorder: ring24h_by_key fed",
      len((t.get("ring24h_by_key") or {}).get("popular-today") or []) == 3)
check("batch recorder: gids deduped in per_sort_gids",
      sorted((c.get("per_sort_gids") or {}).get("popular-today") or []) == ["101", "102", "103"])

# ---- 2) digest window anchors to previous send ----------------------------
now = time.time()
dash._save_counters({"per_page_new": [
    ["date", 1, now - 3600],          # within window
    ["date", 1, now - 90000],         # 25h ago — OUTSIDE a 24h fallback
    ["tag:incest", 4, now - 100],     # fresh
]})
# case A: no prior send -> 24h fallback
fake.store.pop("discovery_digest_last_sent", None)
text, total = dig.build_report(now)
check("digest fallback window (24h): total=2", total == 2)
check("digest fallback header", "(last 24h)" in text or "since" in text)
# case B: prior send 26h ago -> 26h window includes the 25h-old entry
fake.store["discovery_digest_last_sent"] = now - 26 * 3600
text, total = dig.build_report(now)
check("digest anchored window: total=3 (25h-old included)", total == 3)
check("digest anchored header shows 'since'", "since" in text)
# case C: explicit since_ts
text, total = dig.build_report(now, since_ts=now - 200)
check("digest explicit since_ts: total=1", total == 1)

# ---- 3) list_sweeper discovery hook present in source --------------------
src = (SB / "app/services/list_sweeper.py").read_text(encoding="utf-8")
check("list_sweeper: discovery hook wired",
      "record_new_galleries_on_page" in src)
check("list_sweeper: PK IN-query for existing gallery rows",
      'WHERE "key" IN' in src and "gallery:" in src)

# ---- 4) Bot2 dashboard relabel present ------------------------------------
dsrc = (ROOT / "Bot2Fetcher/app/dashboard.py").read_text(encoding="utf-8")
check("bot2 dashboard: lifetime line", "Lifetime cached (Turso)" in dsrc)
check("bot2 dashboard: queue line", "Cache queue (not yet done/failed)" in dsrc)
check("bot2 dashboard: remaining = queue size", "remaining = total_cache" in dsrc)
fsrc = (ROOT / "Bot2Fetcher/app/fetcher.py").read_text(encoding="utf-8")
check("bot2 fetcher: cache_lifetime plumbed", "cache_lifetime" in fsrc and "_last_cache_lifetime" in fsrc)

print("-" * 60)
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}"); sys.exit(1)
print("ALL v1.27 CHECKS PASSED")
