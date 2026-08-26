"""v12.47: parity + behavior tests for the shared turso_cache layer.
Run from repo root:  python3 tests/test_turso_cache.py
"""
import pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent

def load(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

# 1) byte-parity of the three shipped copies
a = (ROOT / "common/turso_cache/normalize.py").read_bytes()
b = (ROOT / "ScraperBot/app/turso_cache/normalize.py").read_bytes()
c = (ROOT / "Bot2Fetcher/app/turso_cache/normalize.py").read_bytes()
assert a == b == c, "normalize.py copies DRIFTED — resync them"
print("parity: normalize.py x3 byte-identical OK")

w = (ROOT / "common/turso_cache/writer.py").read_bytes()
for x in ("ScraperBot/app/turso_cache/writer.py",
          "Bot2Fetcher/app/turso_cache/writer.py"):
    assert w == (ROOT / x).read_bytes(), f"writer.py drifted in {x}"
print("parity: writer.py x3 byte-identical OK")

# 2) behavior suite against the canonical copy
norm = load("common/turso_cache/normalize.py", "tc_norm")
writer = load("common/turso_cache/writer.py", "tc_writer")

# gallery: raw v2 dict payload -> canonical
raw = {"id": 12345, "media_id": "999",
       "title": {"english": "Some Title", "pretty": "Some Title"},
       "images": {"pages": [{"t": "j"}] * 22, "cover": {"t": "p"}},
       "tags": [{"name": "sole female", "type": "tag"}],
       "num_pages": 22, "num_favorites": 5, "upload_date": 1767000000}
ok, out = norm.normalize_for_write("gallery:12345", raw, source="test")
assert ok and out["id"] == "12345" and isinstance(out["pages"], int) \
    and out["pages"] == 22 and isinstance(out["title"], str) \
    and out["title"] == "Some Title" \
    and out["cover"] == "https://t.nhentai.net/galleries/999/cover.png" \
    and out["tags"][0]["name"] == "sole female" \
    and out["tag_groups"]["tag"] == ["sole female"]
print("gallery raw-v2 -> canonical OK")

# gallery: pages as list / dict (the two crash shapes)
for weird in ([22], {"count": 22}, "22", 22.0):
    ok, out = norm.normalize_for_write(
        "gallery:1", {"id": 1, "media_id": "9", "cover": {"t": "j"},
                      "pages": weird, "title": "X"}, source="test")
    assert ok and out["pages"] == 22, (weird, out)
print("gallery pages list/dict/str/float -> int OK")

# gallery: refuse on missing cover+media_id, missing id
ok, out = norm.normalize_for_write(
    "gallery:1", {"id": 1, "title": "X"}, source="test")
assert not ok and out is None
ok, out = norm.normalize_for_write(
    "gallery:1", {"title": "X", "cover": "https://x/y.jpg"}, source="x")
# id falls back to gid hint from the key -> accepted
assert ok and out["id"] == "1"
ok, out = norm.normalize_for_write(
    "gallery:", {"title": "X", "cover": "https://x/y.jpg"}, source="x")
assert not ok  # no id anywhere -> refuse
print("gallery refuse paths OK")

# gallery: already-canonical passthrough stays canonical
canon = {"id": "7", "title": "T", "tag_groups": {"tag": ["x"]},
         "pages": "9", "cover": "https://t.nhentai.net/galleries/9/cover.jpg"}
ok, out = norm.normalize_for_write("gallery:7", canon, source="test")
assert ok and out["pages"] == 9 and out["id"] == "7"
print("gallery canonical passthrough OK")

# search: raw v2 page -> cards
page = {"result": [{"id": 11, "media_id": "5", "num_pages": 3,
                    "title": {"english": "T1"},
                    "thumbnail": "galleries/5/thumb.jpg"},
                   {"id": "12", "media_id": "6", "num_pages": [4],
                    "title": {"english": "T2"}, "cover": {"t": "w"}}]}
ok, out = norm.normalize_for_write("search:popular:page1", page, source="test")
assert ok and len(out) == 2 and out[0]["id"] == "11" \
    and out[0]["cover"] == "https://t.nhentai.net/galleries/5/thumb.jpg" \
    and out[1]["pages"] == 4 and out[1]["cover"].endswith("cover.webp")
print("search raw-v2 -> cards OK")

# search: normalized list coerced in place; junk entries dropped
lst = [{"id": 21, "title": "A", "cover": "https://t/x.jpg", "pages": "5"},
       "garbage", {"no_id": True}, None]
ok, out = norm.normalize_for_write("search:date:page2", lst, source="test")
assert ok and len(out) == 1 and out[0]["id"] == "21" and out[0]["pages"] == 5
print("search list coercion OK")

# search: refuse None payload (literal null write)
ok, out = norm.normalize_for_write("search:x:page1", None, source="test")
assert not ok
print("search None refused OK")

# search: unrecognised dict passthrough (never block real writes)
ok, out = norm.normalize_for_write(
    "search:q=x|sort=popular|page=1", {"custom": 1}, source="test")
assert ok and out == {"custom": 1}
print("search dict passthrough OK")

# misc keys untouched
blob = {"whatever": [1, {"a": "b"}]}
ok, out = norm.normalize_for_write("trending:popular", blob, source="test")
assert ok and out is blob
print("misc passthrough OK")

# writer builders
sql, args = writer.build_upsert_sql("gallery:1", '{"a":1}', 60, now=1000)
assert args == ["gallery:1", '{"a":1}', 1000, 1060, 60] and "ON CONFLICT" in sql
sql, args = writer.build_upsert_sql("search:x:page1", "[]", 0, now=1000)
assert args[3] == 0 and args[4] == 0
sql2, args2 = writer.build_upsert_sql("k", "p", 10, now=1, preserve_cached_at=True)
assert "cached_at=excluded.cached_at" not in sql2
sql3, args3 = writer.build_update_payload_sql("gallery:1", '{"b":2}')
assert sql3.startswith("UPDATE") and args3 == ['{"b":2}', "gallery:1"]
print("writer builders OK")

print("ALL turso_cache TESTS PASS")
