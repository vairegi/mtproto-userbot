"""v1.24 English-only enforcement — unit tests (pure Python, no network).

Run from repo root:  python3 ScraperBot/tests/test_english_only.py
"""
from __future__ import annotations
import asyncio, pathlib, sys, types

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent  # repo root
SB = ROOT / "ScraperBot"

# --- Stub the two modules under test's only external deps ---------------
settings = types.SimpleNamespace(
    user_agent="test-ua",
    nhentai_api_key="",
    english_only=True,
    trending_tags_enabled=True,
    trending_tags_top_n=10,
    trending_tags_refresh_sec=86400,
)

app_pkg = types.ModuleType("app")
app_pkg.__path__ = [str(SB / "app")]
sys.modules["app"] = app_pkg

cfg = types.ModuleType("app.config")
cfg.settings = settings
sys.modules["app.config"] = cfg

mc = types.ModuleType("app.mongo_client")
mc.state_get = lambda k, d=None: d
mc.state_set = lambda k, v: None
sys.modules["app.mongo_client"] = mc

sys.path.insert(0, str(SB))

import importlib  # noqa: E402
hf = importlib.import_module("app.hf_scraper_lite")
tt = importlib.import_module("app.services.trending_tags")


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload or {}
        self.headers = {}
        self.text = ""
    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload if payload is not None else {"result": []}
    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return _FakeResp(200, self.payload)


def test_search_query_guard():
    settings.english_only = True
    c = _FakeClient()
    asyncio.run(hf.fetch_search_page(c, query="tag:incest", sort="popular", page=1))
    assert c.calls[-1][1]["query"] == "tag:incest language:english", c.calls[-1]
    asyncio.run(hf.fetch_search_page(c, query="sole female", sort="date", page=2))
    assert c.calls[-1][1]["query"] == "sole female language:english"
    # explicit language filter respected as-is (no double-append)
    asyncio.run(hf.fetch_search_page(c, query="tag:x language:japanese"))
    assert c.calls[-1][1]["query"] == "tag:x language:japanese"
    # empty query -> chip path, still english
    asyncio.run(hf.fetch_search_page(c, query=""))
    assert c.calls[-1][1]["query"] == "language:english"
    print("guard ON: tag/typed/empty/explicit OK")

    settings.english_only = False
    c2 = _FakeClient()
    asyncio.run(hf.fetch_search_page(c2, query="tag:incest"))
    assert c2.calls[-1][1]["query"] == "tag:incest", c2.calls[-1]
    print("guard OFF: verbatim OK")
    settings.english_only = True


def test_trending_language_filter():
    settings.english_only = True
    en = [{"type": "language", "name": "english"},
          {"type": "tag", "name": "vanilla"}]
    jp = [{"type": "language", "name": "japanese"},
          {"type": "tag", "name": "vanilla"}]
    none_ = [{"type": "tag", "name": "vanilla"}]
    assert tt._gallery_is_english(en) is True
    assert tt._gallery_is_english(jp) is False
    assert tt._gallery_is_english(none_) is True
    assert tt._gallery_is_english([]) is True
    assert tt._gallery_is_english(None) is True
    assert tt._gallery_is_english("junk") is True
    print("trending language filter OK")

    # end-to-end: _fetch_gallery_names counts only English galleries
    from collections import Counter
    counts = Counter()
    asyncio.run(tt._fetch_gallery_names(_FakeClient({"tags": jp}), 1, {}, counts))
    assert counts.get("vanilla", 0) == 0, counts
    asyncio.run(tt._fetch_gallery_names(_FakeClient({"tags": en}), 2, {}, counts))
    assert counts.get("vanilla", 0) == 1, counts

    # gate off -> japanese galleries counted again
    settings.english_only = False
    counts2 = Counter()
    asyncio.run(tt._fetch_gallery_names(_FakeClient({"tags": jp}), 3, {}, counts2))
    assert counts2.get("vanilla", 0) == 1, counts2
    settings.english_only = True
    print("trending end-to-end (skip non-EN, count EN, gate-off) OK")


if __name__ == "__main__":
    test_search_query_guard()
    test_trending_language_filter()
    print("\nALL v1.24 english-only tests PASS")
