"""
nhentai_cache.py — v12.2 Mongo-backed cache + shared token bucket.

Two problems this module solves, both from the v12.1 → v12.2 postmortem:

1. "1 user 429s everyone" — nhentai rate-limits by IP, and your backend has
   one outbound IP. Once a user's search chews through the quota, all other
   users' next requests get 429'd too. Fix: cache aggressively so identical
   upstream requests happen at most ONCE across all users for the whole TTL.

2. "Paginator hit 20 upstream pages per search". Fix: a shared Mongo token
   bucket, sized to the anon limits documented in the real openapi.json at
   nhentai.net/api/v2/openapi.json. Every upstream call consumes a token;
   when the bucket runs dry the caller MUST serve from cache (even stale)
   or fail gracefully — no more silent 429 storms.

TTL policy — deliberately long, per the v12.2 conversation:
  * gallery detail   : 30 days   (immutable after upload)
  * search results   :  3 days   (nhentai's popular/date order barely shifts)
  * suggestions      :  3 days   (same reasoning)
  * trending/homepage: 30 minutes (this one actually rotates)

Everything is best-effort: a Mongo outage MUST NOT break the mini-app.
Every public function catches PyMongoError and returns None / False so the
caller can fall through to the live upstream path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("miniapp.nhentai_cache")

# v12.4: Turso-first cache layer. Import lazily so a missing package can
# never crash the mini-app; turso_client.turso_available() gates all use.
try:
    from . import turso_client as _turso
except Exception:  # noqa: BLE001
    _turso = None

# ---------------------------------------------------------------------------
# TTL config (seconds). Env-overridable so ops can tune without a redeploy.
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


TTL_GALLERY_SEC    = _env_int("NHCACHE_TTL_GALLERY_SEC",    30 * 24 * 3600)   # 30 days
TTL_SEARCH_SEC     = _env_int("NHCACHE_TTL_SEARCH_SEC",      3 * 24 * 3600)   #  3 days
TTL_SUGGEST_SEC    = _env_int("NHCACHE_TTL_SUGGEST_SEC",     3 * 24 * 3600)   #  3 days
TTL_TRENDING_SEC   = _env_int("NHCACHE_TTL_TRENDING_SEC",              1800)   # 30 min
TTL_STALE_GRACE_SEC = _env_int("NHCACHE_TTL_STALE_GRACE_SEC", 7 * 24 * 3600)   # keep stale docs around this long for "serve-stale-if-error"

# v12.20: NEVER-EXPIRE sentinel. Chip-sort and tag-sort search pages are
# fully owned by BOT 1's continuous sweep — every phase INSERT-OR-REPLACEs
# the whole page payload, so new items appear and removed items disappear
# automatically. TTL freshness makes no sense for a row a background worker
# is authoritatively rewriting; instead we stamp expires_at=0 and treat
# that as "always fresh, never call nhentai". Non-chip/tag keys still use
# TTL. Toggle via NHCACHE_CHIP_TAG_NEVER_EXPIRE=0 to revert without a code
# change.
NHCACHE_CHIP_TAG_NEVER_EXPIRE = os.environ.get(
    "NHCACHE_CHIP_TAG_NEVER_EXPIRE", "1").strip() not in ("0", "false", "False", "")


def _is_chip_or_tag_key(key: str) -> bool:
    """True for keys whose payload is owned by BOT 1's sweep and therefore
    should never expire. Two formats qualify:
        search:<sort>:page<N>              chip sorts
        search:q=<q>|sort=<s>|page=<N>     tag / typed-search sorts
    Legacy `search:<hash>|<sort>|<page>` (Mongo-only, from earlier BOT 1
    versions) is NOT included — those rows are read-only fallbacks.
    """
    if not isinstance(key, str) or not key.startswith("search:"):
        return False
    tail = key[len("search:"):]
    # Chip: <sort>:pageN, no pipe, one colon separating sort from pageN.
    if "|" not in tail and tail.count(":") == 1 and ":page" in tail:
        return True
    # Tag / typed: q=...|sort=...|page=N (all three parts required).
    if tail.startswith("q=") and "|sort=" in tail and "|page=" in tail:
        return True
    return False


# ---------------------------------------------------------------------------
# Token-bucket capacities (per minute) — sourced from openapi.json ANON tier.
# See nhentai.net/api/v2/openapi.json — these are documented, not guessed.
# ---------------------------------------------------------------------------
BUCKETS = {
    # bucket_id       : (capacity_per_min, human_label)
    "search"          : (10, "GET /api/v2/search"),
    "galleries"       : (20, "GET /api/v2/galleries/{id}"),
    "galleries_list"  : (15, "GET /api/v2/galleries"),
    "popular"         : ( 8, "GET /api/v2/galleries/popular"),
    "suggestions"     : (60, "GET /api/v2/galleries/{id}/suggestions"),
}


# ---------------------------------------------------------------------------
# Lazy db-handle acquisition. Import-time db.connect() would break under
# pytest and any tool that doesn't have MONGO_URI set. Every public function
# calls _handle() and gracefully returns on failure.
# ---------------------------------------------------------------------------
_conn = None  # cached MongoHandle


def _handle():
    global _conn
    if _conn is not None:
        return _conn
    try:
        import db as _db  # local import — sys.path is set by scraper_bridge
        _conn = _db.connect()
    except Exception as e:  # noqa: BLE001
        log.warning("nhentai_cache: mongo unavailable (%s) — cache disabled", e)
        _conn = None
    return _conn


# ---------------------------------------------------------------------------
# Cache-key helpers. Deterministic, short, human-readable when possible.
# ---------------------------------------------------------------------------
def gallery_key(gid: str | int) -> str:
    return f"gallery:{gid}"


def search_key(query: str, sort: str, page: int) -> str:
    q = (query or "").strip().lower()
    s = (sort or "popular").strip().lower()
    p = int(page or 1)
    # Long queries get hashed to keep the _id compact; short queries stay legible.
    if len(q) <= 40 and all(c.isalnum() or c in " -_" for c in q):
        return f"search:{q}|{s}|{p}"
    h = hashlib.sha1(q.encode("utf-8")).hexdigest()[:16]
    return f"search:{h}|{s}|{p}"


def suggestions_key(gid: str | int) -> str:
    return f"suggest:{gid}"


def trending_key(kind: str = "popular") -> str:
    return f"trending:{kind}"


def bucket_for_key(key: str) -> str:
    """Map a cache key to its token-bucket id — used to pick the right
    quota bucket before firing the upstream call."""
    if key.startswith("gallery:"):     return "galleries"
    if key.startswith("search:"):      return "search"
    if key.startswith("suggest:"):     return "suggestions"
    if key.startswith("trending:"):    return "popular"
    return "galleries_list"


def ttl_for_key(key: str) -> int:
    if key.startswith("gallery:"):  return TTL_GALLERY_SEC
    if key.startswith("search:"):   return TTL_SEARCH_SEC
    if key.startswith("suggest:"):  return TTL_SUGGEST_SEC
    if key.startswith("trending:"): return TTL_TRENDING_SEC
    return TTL_SEARCH_SEC


# ---------------------------------------------------------------------------
# Cache API
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# v12.4: Turso-first read/write path. Same public signatures as before.
#
# Read order:  Turso → (miss/error) → Mongo → (miss/error) → None
# Write order: Turso (best-effort) → Mongo (best-effort)
#
# Turso holds fresh reads; Mongo remains as the last-known-good fallback
# so a Turso outage cannot break the mini-app. Both stores use unix-epoch
# seconds for expires_at to keep comparisons trivial across backends.
# ---------------------------------------------------------------------------
def _now_epoch() -> int:
    import time as _t
    return int(_t.time())


def _turso_get(key: str, allow_stale: bool) -> Optional[dict]:
    if _turso is None or not _turso.turso_available():
        return None
    try:
        rs = _turso.execute(
            "SELECT payload, expires_at FROM nhentai_cache WHERE key = ?",
            [key],
        )
    except Exception:  # noqa: BLE001
        return None
    if not rs or not getattr(rs, "rows", None):
        return None
    row = rs.rows[0]
    payload_json = row[0]
    try:
        expires_at = int(row[1])
    except (TypeError, ValueError):
        return None
    now = _now_epoch()
    # v12.20: expires_at == 0 is the never-expire sentinel for chip/tag rows
    # that BOT 1 authoritatively rewrites every sweep. Always fresh.
    if expires_at == 0:
        pass  # never expires
    elif expires_at > now:
        pass  # fresh
    elif allow_stale and (now - expires_at) < TTL_STALE_GRACE_SEC:
        pass  # stale-but-servable
    else:
        return None
    try:
        return json.loads(payload_json)
    except (TypeError, ValueError):
        return None


def _mongo_get(key: str, allow_stale: bool) -> Optional[dict]:
    conn = _handle()
    if conn is None:
        return None
    try:
        doc = conn.nhentai_cache.find_one({"_id": key})
    except Exception:  # noqa: BLE001
        return None
    if not doc:
        return None
    now = _now_dt()
    exp = doc.get("expires_at")
    # v12.20: BOT 1 writes epoch seconds; BOT 0 historically wrote datetimes.
    # Both are handled. The never-expire sentinel is exp==0 OR exp is a
    # tz-aware datetime at epoch 0 — either way, treat as always fresh.
    if isinstance(exp, (int, float)):
        if exp == 0:
            return doc.get("payload")
        # Compare epoch-seconds to now (as epoch).
        if exp > _now_epoch():
            return doc.get("payload")
        if allow_stale and (_now_epoch() - exp) < TTL_STALE_GRACE_SEC:
            return doc.get("payload")
        return None
    if exp and exp > now:
        return doc.get("payload")
    if allow_stale and exp:
        stale_cutoff = now - timedelta(seconds=TTL_STALE_GRACE_SEC)
        if exp > stale_cutoff:
            return doc.get("payload")
    return None


# ---------------------------------------------------------------------------
# v12.22 (#3): per-bucket HIT/MISS histogram.
#
# In-process counters only (per Render service restart) — deliberately NOT
# written to Mongo/Turso so the read hot path pays zero extra I/O. Surfaces
# via GET /api/admin/cache/hitmiss so the admin can verify the cache works
# without reading raw logs. Thread-safe via a module-level Lock; bounded
# by the fixed bucket set below so it can never grow unbounded.
# ---------------------------------------------------------------------------
import threading as _hm_threading

_HM_LOCK = _hm_threading.Lock()
# bucket -> {"hit": n, "miss": n}. Buckets mirror bucket_for_key()'s ids.
_HITMISS: dict = {}


def _hm_bucket_of(key: str) -> str:
    """Coarse bucket for the histogram. Chip/tag search pages are split out
    from detail/gallery reads because that's the axis the admin cares about
    ('are my Discover chips warm?')."""
    if not isinstance(key, str):
        return "other"
    if key.startswith("search:"):
        return "search"
    if key.startswith("gallery:"):
        return "gallery"
    if key.startswith("suggest:"):
        return "suggest"
    if key.startswith("trending:"):
        return "trending"
    return "other"


def _hm_record(key: str, hit: bool) -> None:
    b = _hm_bucket_of(key)
    with _HM_LOCK:
        slot = _HITMISS.setdefault(b, {"hit": 0, "miss": 0})
        slot["hit" if hit else "miss"] += 1


def hitmiss_snapshot() -> dict:
    """Return a copy of the counters + derived hit-rate per bucket."""
    with _HM_LOCK:
        snap = {b: dict(v) for b, v in _HITMISS.items()}
    out = {}
    for b, v in snap.items():
        h = int(v.get("hit", 0))
        m = int(v.get("miss", 0))
        total = h + m
        out[b] = {
            "hit": h,
            "miss": m,
            "total": total,
            "hit_rate": round(h / total, 4) if total else None,
        }
    return out


def hitmiss_reset() -> None:
    with _HM_LOCK:
        _HITMISS.clear()


def get(key: str, allow_stale: bool = False) -> Optional[dict]:
    """Return the cached payload for `key`, or None.

    `allow_stale=True` lets the caller pull a doc that's past its expires_at
    but still within TTL_STALE_GRACE_SEC — powers 'upstream 429 → serve
    stale-if-error'. v12.4: reads Turso first, Mongo as fallback.
    v12.22: records a hit/miss into the in-process histogram (item #3).
    """
    payload = _turso_get(key, allow_stale)
    if payload is not None:
        _hm_record(key, True)
        return payload
    payload = _mongo_get(key, allow_stale)
    _hm_record(key, payload is not None)
    return payload


def _turso_put(key: str, payload_json: str, ttl: int) -> bool:
    if _turso is None or not _turso.turso_available():
        return False
    now = _now_epoch()
    # v12.20: chip/tag rows get expires_at=0 sentinel so they never expire.
    # ttl_sec is still stored (as 0) so anyone querying the row sees the
    # intent clearly.
    if NHCACHE_CHIP_TAG_NEVER_EXPIRE and _is_chip_or_tag_key(key):
        expires_at = 0
        stored_ttl = 0
    else:
        expires_at = now + ttl
        stored_ttl = ttl
    try:
        rs = _turso.execute(
            "INSERT INTO nhentai_cache (key, payload, cached_at, expires_at, ttl_sec) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "payload=excluded.payload, cached_at=excluded.cached_at, "
            "expires_at=excluded.expires_at, ttl_sec=excluded.ttl_sec",
            [key, payload_json, now, expires_at, stored_ttl],
        )
        return rs is not None
    except Exception as e:  # noqa: BLE001
        log.debug("turso put(%s) failed: %s", key, e)
        return False


def _mongo_put(key: str, payload: Any, ttl: int) -> bool:
    conn = _handle()
    if conn is None:
        return False
    # v12.20: same never-expire treatment as _turso_put. Note that BOT 1
    # writes expires_at as an epoch NUMBER; BOT 0 historically writes a
    # datetime. _mongo_get handles both shapes, so either sentinel form
    # (0 as int/float) works; we pick the numeric form for consistency
    # with BOT 1.
    if NHCACHE_CHIP_TAG_NEVER_EXPIRE and _is_chip_or_tag_key(key):
        exp_val: Any = 0
        stored_ttl = 0
    else:
        exp_val = _now_dt() + timedelta(seconds=ttl)
        stored_ttl = ttl
    doc = {
        "_id":        key,
        "payload":    payload,
        "expires_at": exp_val,
        "cached_at":  _now_dt(),
        "ttl_sec":    stored_ttl,
    }
    try:
        conn.nhentai_cache.replace_one({"_id": key}, doc, upsert=True)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("mongo put(%s) failed: %s", key, e)
        return False


def put(key: str, payload: Any, ttl_sec: Optional[int] = None):
    """Write a cache entry. Best-effort to BOTH backends so a Turso outage
    still leaves a Mongo copy for the fallback read path (and vice versa).

    v12.9: DEDUP GUARD — before writing, read the existing FRESH row from
    Turso. If the stored payload is byte-identical to the new one, skip
    the rewrite entirely and return the string "unchanged" (callers that
    do `bool(put(...))` still see a truthy value, so semantics are
    preserved; only the Turso row's expires_at is NOT extended, which is
    correct — unchanged data doesn't need a freshness bump, and the
    prefetch cron will rewrite it when content actually changes).

    This stops the "user reopened popular page 5 minutes later and I saw
    another write in the log" flood: same upstream payload = no write.

    Returns True if AT LEAST ONE write succeeded, "unchanged" if the
    existing payload matched byte-for-byte, False on hard failure."""
    ttl = int(ttl_sec if ttl_sec is not None else ttl_for_key(key))
    # Guard against garbage payloads that would poison the cache.
    try:
        payload_json = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        log.warning("nhentai_cache.put(%s): payload not JSON-serialisable — skipped", key)
        return False

    # v12.9 dedup: compare against the existing fresh Turso row (if any).
    # v12.20: expires_at == 0 is the never-expire sentinel — treat it as
    # "always fresh" here too, otherwise every chip/tag rewrite would bypass
    # dedup and flood Turso with identical writes.
    if _turso is not None and _turso.turso_available():
        try:
            rs = _turso.execute(
                "SELECT payload, expires_at FROM nhentai_cache WHERE key = ?",
                [key],
            )
            if rs is not None and getattr(rs, "rows", None):
                existing_json, existing_exp = rs.rows[0]
                try:
                    _exp = int(existing_exp or 0)
                except (TypeError, ValueError):
                    _exp = 0
                _is_fresh = (_exp == 0) or (_exp > _now_epoch())
                if existing_json == payload_json and _is_fresh:
                    return "unchanged"
        except Exception:  # noqa: BLE001
            pass  # dedup is best-effort; never block the write path

    ok_turso = _turso_put(key, payload_json, ttl)
    ok_mongo = _mongo_put(key, payload, ttl)
    return ok_turso or ok_mongo


def invalidate(key: str) -> None:
    """Force-delete a cache entry from BOTH backends (admin /force-rescrape)."""
    if _turso is not None and _turso.turso_available():
        try:
            _turso.execute("DELETE FROM nhentai_cache WHERE key = ?", [key])
        except Exception:  # noqa: BLE001
            pass
    conn = _handle()
    if conn is None:
        return
    try:
        conn.nhentai_cache.delete_one({"_id": key})
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# v12.21: promote existing chip/tag rows to the never-expire sentinel.
#
# Rows written before v12.20 / v1.12 still carry a real expires_at timestamp.
# This helper flips every chip/tag row to expires_at=0 / ttl_sec=0 so it
# becomes permanently fresh under v12.20's read path. Idempotent — rows
# already at the sentinel are left alone.
#
# Two SQL/Mongo statements, each with `WHERE expires_at != 0` so re-running
# is cheap. Returns per-store counts + a total.
# ---------------------------------------------------------------------------
def promote_chip_tag_sentinel() -> dict:
    """Convert every chip-sort + tag-sort row to the never-expire sentinel.

    Matches the two key formats defined in `_is_chip_or_tag_key`:
        search:<sort>:page<N>              (chip sorts)
        search:q=<q>|sort=<s>|page=<N>     (tag / typed sorts)

    Returns:
        {"turso": <rows_updated>, "mongo": <rows_updated>,
         "turso_ok": bool, "mongo_ok": bool}
    """
    result: dict = {"turso": 0, "mongo": 0, "turso_ok": False, "mongo_ok": False}

    # ---- Turso: two UPDATEs, one per key format ----
    if _turso is not None and _turso.turso_available():
        try:
            # Chip format: search:<sort>:page<N>. GLOB is SQLite-native and
            # cheaper than LIKE; the `NOT LIKE '%|%'` guard excludes the tag
            # format that also starts with `search:`.
            rs1 = _turso.execute(
                "UPDATE nhentai_cache "
                "   SET expires_at = 0, ttl_sec = 0 "
                " WHERE key GLOB 'search:*:page*' "
                "   AND key NOT LIKE '%|%' "
                "   AND expires_at != 0",
                [],
            )
            # Tag format: search:q=<q>|sort=<s>|page=<N>
            rs2 = _turso.execute(
                "UPDATE nhentai_cache "
                "   SET expires_at = 0, ttl_sec = 0 "
                " WHERE key LIKE 'search:q=%|sort=%|page=%' "
                "   AND expires_at != 0",
                [],
            )
            # libsql returns affected-row count in rows_affected; guard both.
            def _rows(rs):
                if rs is None:
                    return 0
                for attr in ("rows_affected", "affected_row_count", "rowcount"):
                    v = getattr(rs, attr, None)
                    if isinstance(v, int):
                        return v
                return 0
            result["turso"] = _rows(rs1) + _rows(rs2)
            result["turso_ok"] = True
        except Exception as e:  # noqa: BLE001
            log.warning("promote_chip_tag_sentinel: turso failed: %s", e)

    # ---- Mongo: same two predicates, updateMany ----
    conn = _handle()
    if conn is not None:
        try:
            import re as _re
            # Chip: search:<sort>:page<N>, no pipes, exactly one colon in the tail.
            chip_re = _re.compile(r"^search:[^|:]+:page\d+$")
            # Tag/typed: search:q=...|sort=...|page=N
            tag_re = _re.compile(r"^search:q=.*\|sort=.*\|page=\d+$")
            r1 = conn.nhentai_cache.update_many(
                {"_id": chip_re, "expires_at": {"$ne": 0}},
                {"$set": {"expires_at": 0, "ttl_sec": 0}},
            )
            r2 = conn.nhentai_cache.update_many(
                {"_id": tag_re, "expires_at": {"$ne": 0}},
                {"$set": {"expires_at": 0, "ttl_sec": 0}},
            )
            result["mongo"] = int(getattr(r1, "modified_count", 0)) + \
                              int(getattr(r2, "modified_count", 0))
            result["mongo_ok"] = True
        except Exception as e:  # noqa: BLE001
            log.warning("promote_chip_tag_sentinel: mongo failed: %s", e)

    log.info("promote_chip_tag_sentinel: turso=%d mongo=%d",
             result["turso"], result["mongo"])
    return result


# ---------------------------------------------------------------------------
# v12.13 (#C): Turso dedup helpers.
#
# The nhentai_cache table declares `key TEXT PRIMARY KEY`, so by construction
# it cannot hold two rows sharing the same key — SQLite/libSQL rejects the
# second INSERT and our `ON CONFLICT(key) DO UPDATE` collapses concurrent
# writers into a single row. That means dedup_cron has no legitimate row
# duplicates to remove on the Turso side.
#
# We still expose list_gallery_keys() and delete_row() because they are the
# clean, safe utilities dedup_cron probes for (see getattr(_nc, ...) in
# dedup_cron._dedup_turso). Their presence flips the cron from the noisy
# "turso dedup unsupported" branch to the normal, quiet branch that simply
# reports scanned=0/removed=0 when the table is already unique-by-key.
#
# As a bonus, list_gallery_keys() also surfaces expired rows so a future
# sweep or admin utility can prune them without touching valid data. This
# module never *automatically* deletes expired rows here — that is Turso's
# job via the idx_nhcache_expires index and can be extended later.
# ---------------------------------------------------------------------------
def list_gallery_keys() -> list[dict]:
    """List every gallery:<id> row currently stored in Turso nhentai_cache.

    Returns a list of dicts with keys: {"key", "expires_at", "cached_at"}.
    Because `key` is the PRIMARY KEY, each returned row is already unique.
    Returns an empty list when Turso is unavailable or the query fails.

    The dedup cron uses this to build its by-key buckets; with a unique
    primary key every bucket has size 1, so removed=0 is the correct and
    healthy outcome — no scary warning needed.
    """
    if _turso is None or not _turso.turso_available():
        return []
    try:
        rs = _turso.execute(
            "SELECT key, expires_at, cached_at FROM nhentai_cache "
            "WHERE key LIKE 'gallery:%'"
        )
    except Exception as e:  # noqa: BLE001
        log.debug("list_gallery_keys: turso execute failed: %s", e)
        return []
    if rs is None or not getattr(rs, "rows", None):
        return []
    out: list[dict] = []
    for row in rs.rows:
        try:
            out.append({
                "key":        str(row[0]),
                "expires_at": int(row[1]) if row[1] is not None else 0,
                "cached_at":  int(row[2]) if row[2] is not None else 0,
            })
        except (TypeError, ValueError, IndexError):
            continue
    return out


def delete_row(key: str, rowid: Any = None) -> bool:
    """Delete a single row from Turso nhentai_cache by key.

    The `rowid` argument is accepted for API compatibility with dedup_cron
    but is not needed — the primary key alone uniquely identifies the row.
    Returns True when the DELETE executed, False on any failure. Never
    raises. Used defensively; because the table is unique-by-key, the cron
    normally never has anything to delete.
    """
    del rowid  # accepted for signature-compat, intentionally unused
    if not key:
        return False
    if _turso is None or not _turso.turso_available():
        return False
    try:
        _turso.execute("DELETE FROM nhentai_cache WHERE key = ?", [key])
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("delete_row(%s) failed: %s", key, e)
        return False


# ---------------------------------------------------------------------------
# Token bucket — SHARED across users. Prevents any single search from
# blowing past the openapi.json quota and 429ing everyone.
#
# v12.4: Turso-first. The refill+spend happens in ONE atomic SQL statement
# ('UPDATE ... SET tokens = min(cap, tokens + elapsed*rate) - cost WHERE
# tokens_after_refill >= cost'), which closes the read-modify-write race
# that plagued the Mongo path when two concurrent searches consumed the
# same bucket. Mongo remains as fallback for a Turso outage.
# ---------------------------------------------------------------------------
def _turso_try_consume(bucket_id: str, cap: int, cost: float) -> Optional[bool]:
    """Turso-backed atomic consume. Returns True/False on success, None if
    Turso is unavailable or errored (caller falls through to Mongo)."""
    if _turso is None or not _turso.turso_available():
        return None
    rate_per_sec = cap / 60.0
    now = time.time()
    # Ensure row exists. INSERT OR IGNORE is atomic; no-op if already there.
    try:
        _turso.execute(
            "INSERT OR IGNORE INTO nhentai_ratelimit "
            "(bucket_id, tokens, capacity, rate_per_sec, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [bucket_id, float(cap), cap, rate_per_sec, now],
        )
    except Exception:  # noqa: BLE001
        return None
    # Atomic refill-and-spend. The SQL:
    #   1. computes refilled tokens: min(capacity, tokens + max(0, now-updated_at) * rate_per_sec)
    #   2. guards WHERE that value >= cost
    #   3. subtracts cost and stamps updated_at = now
    # If the WHERE clause fails, zero rows are affected → we treat that as
    # "bucket exhausted" and return False WITHOUT bumping updated_at, so
    # the next caller still gets the full refill they earned.
    try:
        rs = _turso.execute(
            "UPDATE nhentai_ratelimit SET "
            "  tokens = MIN(CAST(capacity AS REAL), tokens + MAX(0, ? - updated_at) * rate_per_sec) - ?, "
            "  updated_at = ? "
            "WHERE bucket_id = ? "
            "  AND MIN(CAST(capacity AS REAL), tokens + MAX(0, ? - updated_at) * rate_per_sec) >= ?",
            [now, cost, now, bucket_id, now, cost],
        )
    except Exception as e:  # noqa: BLE001
        log.debug("turso try_consume(%s) failed: %s", bucket_id, e)
        return None
    if rs is None:
        return None
    # libsql exposes affected rows via .rows_affected on newer clients.
    affected = getattr(rs, "rows_affected", None)
    if affected is None:
        # Older clients: no reliable affected-rows count. Read back tokens
        # to determine success — if tokens < 0 we accidentally over-spent
        # (should be impossible with the WHERE guard, but handle it).
        try:
            probe = _turso.execute(
                "SELECT tokens FROM nhentai_ratelimit WHERE bucket_id = ?",
                [bucket_id],
            )
            if probe and probe.rows and float(probe.rows[0][0]) >= 0:
                return True
        except Exception:  # noqa: BLE001
            pass
        return None
    return bool(affected)


def _mongo_try_consume(bucket_id: str, cap: int, cost: float) -> bool:
    """Mongo-backed consume. Not atomic across processes but the ONLY
    consumer of nhentai_ratelimit is the mini-app backend, and each
    process serialises on its own event loop."""
    conn = _handle()
    if conn is None:
        return True  # dev/test without Mongo: fail open
    rate_per_sec = cap / 60.0
    now = time.time()
    try:
        doc = conn.nhentai_ratelimit.find_one({"_id": bucket_id})
        if doc is None:
            doc = {
                "_id": bucket_id, "tokens": float(cap),
                "capacity": cap, "rate_per_sec": rate_per_sec,
                "updated_at": now,
            }
        elapsed = max(0.0, now - float(doc.get("updated_at") or now))
        tokens = min(float(cap), float(doc.get("tokens") or cap) + elapsed * rate_per_sec)
        if tokens < cost:
            conn.nhentai_ratelimit.update_one(
                {"_id": bucket_id},
                {"$set": {"tokens": tokens, "updated_at": now,
                          "capacity": cap, "rate_per_sec": rate_per_sec}},
                upsert=True,
            )
            return False
        tokens -= cost
        conn.nhentai_ratelimit.update_one(
            {"_id": bucket_id},
            {"$set": {"tokens": tokens, "updated_at": now,
                      "capacity": cap, "rate_per_sec": rate_per_sec}},
            upsert=True,
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("mongo token bucket %s failed (%s) — failing open", bucket_id, e)
        return True


def try_consume(bucket_id: str, cost: float = 1.0) -> bool:
    """Consume `cost` tokens from `bucket_id`. Return True on success.

    v12.4: Turso first (atomic), Mongo fallback (per-process), fail-open
    when neither is available.
    """
    cap, _label = BUCKETS.get(bucket_id, (10, bucket_id))
    turso_result = _turso_try_consume(bucket_id, cap, cost)
    if turso_result is not None:
        return turso_result
    return _mongo_try_consume(bucket_id, cap, cost)


def _turso_bucket_state(bucket_id: str, cap: int, label: str) -> Optional[dict]:
    if _turso is None or not _turso.turso_available():
        return None
    try:
        rs = _turso.execute(
            "SELECT tokens, updated_at FROM nhentai_ratelimit WHERE bucket_id = ?",
            [bucket_id],
        )
    except Exception:  # noqa: BLE001
        return None
    if rs is None:
        return None
    now = time.time()
    if not rs.rows:
        return {"bucket": bucket_id, "label": label, "capacity": cap,
                "tokens": float(cap), "available": True, "backend": "turso"}
    tokens = float(rs.rows[0][0])
    updated_at = float(rs.rows[0][1])
    elapsed = max(0.0, now - updated_at)
    tokens = min(float(cap), tokens + elapsed * (cap / 60.0))
    return {"bucket": bucket_id, "label": label, "capacity": cap,
            "tokens": tokens, "available": tokens >= 1.0, "backend": "turso"}


def bucket_state(bucket_id: str) -> dict:
    """Diagnostic: current tokens / capacity for a bucket. Cheap read.
    v12.4: Turso first, Mongo fallback."""
    cap, label = BUCKETS.get(bucket_id, (0, bucket_id))
    ts = _turso_bucket_state(bucket_id, cap, label)
    if ts is not None:
        return ts
    conn = _handle()
    if conn is None:
        return {"bucket": bucket_id, "label": label, "capacity": cap,
                "tokens": cap, "available": True, "backend": "no-store"}
    try:
        doc = conn.nhentai_ratelimit.find_one({"_id": bucket_id})
        tokens = float(doc.get("tokens") if doc else cap)
        elapsed = max(0.0, time.time() - float(doc.get("updated_at") if doc else time.time()))
        tokens = min(float(cap), tokens + elapsed * (cap / 60.0))
        return {"bucket": bucket_id, "label": label, "capacity": cap,
                "tokens": tokens, "available": tokens >= 1.0, "backend": "mongo"}
    except Exception as e:  # noqa: BLE001
        return {"bucket": bucket_id, "label": label, "error": str(e)}


def all_buckets_state() -> list[dict]:
    """Diagnostic: state of every configured bucket."""
    return [bucket_state(bid) for bid in BUCKETS]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _now_dt() -> datetime:
    # UTC datetime because Mongo TTL indexes require a BSON Date, not epoch.
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# v12.23: re-normalize legacy wrong-shape rows (one-time admin pass).
# BOT 1 (pre-v1.16) wrote RAW nhentai JSON; BOT 0 only reads normalized
# shapes. Walk every search:* + gallery:* row and rewrite in correct shape.
# Idempotent: already-correct rows are skipped.
# ---------------------------------------------------------------------------
def _rn_search(payload):
    """list -> already good (skip); dict with 'result' -> normalize."""
    if isinstance(payload, list):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("result"), list):
        from . import scraper_bridge as _sb
        out = []
        for item in payload["result"]:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            if 12227 not in (item.get("tag_ids") or []):
                continue
            out.append({
                "id": str(item.get("id")),
                "title": _sb._title_from_item(item),
                "title_en_clean": _sb._title_en_clean_from_item(item),
                "cover": _sb._thumb_url_from_item(item),
                "pages": item.get("num_pages"),
                "tags": [],
            })
        return out or None
    return None


def _rn_gallery(payload):
    """dict with tag_groups -> already good (skip); raw v2 -> normalize."""
    if isinstance(payload, dict) and isinstance(payload.get("tag_groups"), dict):
        return None
    if isinstance(payload, dict) and payload.get("id") is not None and isinstance(payload.get("title"), dict):
        from . import scraper_bridge as _sb
        item = payload
        t = item.get("title") or {}
        groups = _sb._group_tags(item)
        flat = [{"name": n, "type": ty} for ty, names in groups.items() for n in names]
        page1 = _sb._direct_nhentai_page1(item)
        cover_path = (item.get("cover") or {}).get("path") or ""
        cover = page1 or (_sb._T_CDN + "/" + cover_path.lstrip("/") if cover_path else _sb._thumb_url_from_item(item))
        return {
            "id": item.get("id"),
            "title": _sb.clean_title(t.get("pretty") or "") if t.get("pretty") else _sb._title_from_item(item),
            "title_english": t.get("english") or "",
            "title_japanese": t.get("japanese") or "",
            "cover": cover,
            "page1_url": page1,
            "pages": item.get("num_pages"),
            "favorites": item.get("num_favorites"),
            "upload_date": _sb._iso_date(item.get("upload_date")),
            "scanlator": item.get("scanlator") or "",
            "tags": flat,
            "tag_groups": groups,
        }
    return None


def renormalize_existing_rows() -> dict:
    out = {"turso_search": 0, "turso_gallery": 0,
           "mongo_search": 0, "mongo_gallery": 0, "skipped": 0}
    def _rows_turso(prefix):
        rs = _turso.execute("SELECT key, payload FROM nhentai_cache WHERE key LIKE ?", [prefix])
        rows = getattr(rs, "rows", None)
        if rows is None and isinstance(rs, dict):
            rows = rs.get("rows")
        return rows or []
    if _turso is not None and _turso.turso_available():
        try:
            for prefix, fn, ctr in (("search:%", _rn_search, "turso_search"),
                                    ("gallery:%", _rn_gallery, "turso_gallery")):
                for row in _rows_turso(prefix):
                    key = row[0]
                    try:
                        payload = json.loads(row[1])
                    except Exception:
                        continue
                    new = fn(payload)
                    if new is None:
                        out["skipped"] += 1
                        continue
                    put(key, new)
                    out[ctr] += 1
        except Exception as e:
            log.warning("renormalize turso failed: %s", e)
    conn = _handle()
    if conn is not None:
        try:
            for prefix, fn, ctr in (("search:", _rn_search, "mongo_search"),
                                    ("gallery:", _rn_gallery, "mongo_gallery")):
                for doc in conn.nhentai_cache.find({"_id": {"$regex": "^" + prefix}}):
                    new = fn(doc.get("payload"))
                    if new is None:
                        out["skipped"] += 1
                        continue
                    put(doc["_id"], new)
                    out[ctr] += 1
        except Exception as e:
            log.warning("renormalize mongo failed: %s", e)
    log.info("renormalize done: %s", out)
    return out
