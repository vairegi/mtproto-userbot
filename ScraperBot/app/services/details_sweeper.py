"""
details_sweeper.py — warms gallery:<id> cache keys.

Round-robin walks the (sort, page) grid, reads the already-cached search
page from Turso/Mongo (zero search-bucket cost — the list_sweeper wrote
it), extracts gallery IDs, and fetches per-gallery detail via
/api/v2/galleries/<id> for any that aren't cached yet or whose entry is
expired.

Priority queue: (sort, page) tuples that list_sweeper JUST wrote get
hydrated on the very next tick.

State (Mongo `scraper1_state`):
  * details_cursor        — {sort_idx, page}  round-robin position
  * details_last_run      — unix ts
  * details_stats         — {sweeps, writes, skips, errors, hits}

v12.48 (sync-audit) — F3:
  _gallery_is_fresh() honours the shared canonical never-expire sentinel
  (expires_at == 0). Bot 0's mini-app reader already treats 0 as fresh,
  the contract in common/turso_cache/normalize.py §7 documents 0 as the
  never-expire sentinel, and Bot 2's meta.py doesn't inspect expires_at
  at all — BOT 1 was the ONE outlier that read 0 as expired. Live pre-fix:
  4,413 of 11,325 gallery:* rows carry expires_at=0; the sweeper used to
  re-fetch and rewrite ALL of them once per round-robin sweep, burning
  the shared 10/min nhentai bucket for zero user benefit.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .. import cache, mongo_client, turso_client, hf_scraper_lite, normalize
from ..config import settings
from . import list_sweeper

log = logging.getLogger("scraperbot.details_sweeper")

_CURSOR_KEY = "details_cursor"
_STATS_KEY  = "details_stats"
_LAST_KEY   = "details_last_run"


def _record_new_on_page(sort: str, page: "Optional[int]") -> None:
    """v1.25: append (sort, page, ts) to the 24h discovery ring in
    dash_counters["per_page_new"] — the raw data the daily admin digest
    (services/discovery_digest.py) aggregates. Capped at 4000 entries so
    the Mongo doc stays small. Failures are swallowed: observability must
    never break a sweep."""
    try:
        from . import channel_dashboard
        c = channel_dashboard._counters()
        ring = [e for e in (c.get("per_page_new") or [])
                if isinstance(e, list)]
        ring.append([str(sort), int(page or 0), time.time()])
        c["per_page_new"] = ring[-4000:]
        channel_dashboard._save_counters(c)
    except Exception as e:  # noqa: BLE001
        log.warning("per-page record failed (non-fatal): %s", e)


def _stats_read() -> Dict[str, Any]:
    return mongo_client.state_get(_STATS_KEY, {}) or {}


def _stats_bump(**delta: int) -> None:
    s = _stats_read()
    for k, v in delta.items():
        s[k] = int(s.get(k, 0)) + int(v)
    mongo_client.state_set(_STATS_KEY, s)


def _cursor_read() -> Tuple[int, int]:
    c = mongo_client.state_get(_CURSOR_KEY, {}) or {}
    return int(c.get("sort_idx", 0) or 0), int(c.get("page", 1) or 1)


def _cursor_write(sort_idx: int, page: int) -> None:
    mongo_client.state_set(_CURSOR_KEY, {
        "sort_idx": int(sort_idx),
        "page": int(page),
        "updated_at": time.time(),
    })


def _cursor_advance() -> Tuple[str, int]:
    """Advance round-robin. Returns the (sort, page) tuple to work on NEXT
    (before the advance) so callers get a stable current pointer."""
    sorts = settings.list_sorts or ["popular"]
    sort_idx, page = _cursor_read()
    sort_idx = sort_idx % len(sorts)
    current_sort = sorts[sort_idx]
    current_page = page

    # Advance for next call: page++, wrap into next sort at cap.
    next_page = current_page + 1
    next_sort_idx = sort_idx
    if next_page > int(settings.details_page_cap or 20):
        next_page = 1
        next_sort_idx = (sort_idx + 1) % len(sorts)
    _cursor_write(next_sort_idx, next_page)
    return current_sort, current_page


async def _read_search_cache(sort: str, page: int) -> Optional[Any]:
    """Read a search-page blob straight from the shared cache. Never fetches.

    v1.10: try BOTH key formats — the BOT 0 chip format (new canonical)
    and the legacy BOT 1 format (what old rows were written with)."""
    keys = [cache.bot0_chip_key(sort, page)]
    if sort.startswith("tag:"):
        tag = sort[4:].strip()
        keys.append(cache.bot0_search_key(tag, "popular", page))
    keys.append(cache.search_key("", sort, page))  # legacy fallback

    for key in keys:
        hit = await turso_client.get(key)
        if hit and hit.get("payload"):
            return hit["payload"]
        m = mongo_client.cache_get_mongo(key)
        if m and m.get("payload"):
            return m["payload"]
    return None


def _extract_ids_any(payload: Any) -> List[str]:
    """v1.17: extract IDs from EITHER shape — v1.16 normalized list of
    card dicts, or legacy raw nhentai dict. Fixes the crash
    'list' object has no attribute 'get'."""
    if isinstance(payload, list):
        ids: List[str] = []
        seen: set = set()
        for item in payload:
            if not isinstance(item, dict):
                continue
            gid = item.get("id") or item.get("gallery_id") or item.get("media_id")
            if gid is None:
                continue
            sv = str(gid).strip()
            if sv and sv not in seen:
                seen.add(sv)
                ids.append(sv)
        return ids
    if isinstance(payload, dict):
        return hf_scraper_lite.extract_ids_from_search(payload)
    return []


def _coerce_epoch(v: Any) -> float:
    """BOT 0's older cache writes stored `expires_at` as a BSON datetime;
    newer writes use epoch floats. Accept either — plus int strings — and
    always return a float epoch. Never raises."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    # datetime.datetime — pymongo returns naive UTC by default.
    try:
        import datetime as _dt
        if isinstance(v, _dt.datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=_dt.timezone.utc)
            return v.timestamp()
    except Exception:  # noqa: BLE001
        pass
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _cache_row_is_fresh(row: Optional[Dict[str, Any]], now: float) -> bool:
    """v12.48 (F3): canonical-contract-aware freshness check.

    A cache row is fresh if the payload is present AND either:
      * expires_at is exactly 0 (never-expire sentinel, per contract §7), OR
      * expires_at is a future epoch.

    A row with expires_at absent / non-numeric / negative behaves like the
    pre-fix expired path (no false-fresh reads of malformed legacy rows).
    """
    if not row:
        return False
    if not row.get("payload"):
        return False
    exp = row.get("expires_at")
    # Exactly-zero sentinel: accept ONLY when the column was explicitly
    # stamped 0 (int or float). Missing / None / strings do not count as
    # the sentinel — they behave like the old "expired" path.
    if isinstance(exp, (int, float)) and float(exp) == 0.0:
        return True
    return _coerce_epoch(exp) > now


async def _gallery_is_fresh(gid: str) -> bool:
    """True if gallery:<id> is present AND not expired in either backend.

    v12.48 (F3): sentinel-aware via _cache_row_is_fresh().
    """
    key = cache.gallery_key(gid)
    now = time.time()
    hit = await turso_client.get(key)
    if _cache_row_is_fresh(hit, now):
        return True
    m = mongo_client.cache_get_mongo(key)
    if _cache_row_is_fresh(m, now):
        return True
    return False


async def _fetch_one_gallery(
    client: httpx.AsyncClient, gid: str, *, source_sort: str = "",
    page: "Optional[int]" = None,
) -> str:
    """One /galleries/<id> call → cache. Returns ok / hit / skip / rate / error.
    `source_sort` is the list-sort or tag pseudo-sort the gid came from
    (e.g. 'popular-today' or 'tag:incest') so the dashboard can attribute
    the new gallery to the right counter line."""
    from . import channel_dashboard
    key = cache.gallery_key(gid)

    if await _gallery_is_fresh(gid):
        _stats_bump(hits=1)
        channel_dashboard.record_cached_gallery(source_sort or "popular")
        # v1.22: attribute the gid to its sort/tag for the /health
        # "cached: N" per-row total (deduped, rolling).
        channel_dashboard.record_gid_seen(source_sort or "popular", gid)
        return "hit"   # already in cache — NOT counted as new

    if not await cache.try_consume(key):
        log.info("⏭  galleries bucket exhausted gid=%s", gid)
        _stats_bump(skips=1)
        channel_dashboard.record_bucket_skip()
        return "skip"

    try:
        raw = await hf_scraper_lite.fetch_gallery(client, gid)
        # v1.16: store the NORMALIZED detail dict (title=string, tag_groups,
        # page1_url, ...) — the raw v2 JSON makes the frontend render
        # `[object Object]`.
        payload = normalize.normalize_gallery(raw)
        if payload is None:
            _stats_bump(errors=1)
            channel_dashboard.record_error()
            return "error"
    except hf_scraper_lite.RateLimited as e:
        log.warning("🚫 429 gid=%s retry_after=%s", gid, e.retry_after)
        _stats_bump(rate_limited=1)
        return "rate"
    except hf_scraper_lite.UpstreamError as e:
        log.warning("upstream error gid=%s status=%s", gid, e.status)
        _stats_bump(errors=1)
        channel_dashboard.record_error()
        return "error"

    write_res = await cache.put(key, payload)
    if not (write_res.get("turso") or write_res.get("mongo")):
        _stats_bump(errors=1)
        channel_dashboard.record_error()
        return "error"

    log.info("📝 detail WRITE gid=%s turso=%s mongo=%s", gid,
             write_res.get("turso"), write_res.get("mongo"))
    _stats_bump(writes=1)
    # Attribute NEW gallery to its source sort/tag so the summary line
    # "New in <label>: N" ticks up on the right row.
    channel_dashboard.record_new_gallery(source_sort or "popular")
    # v1.22: also add to the deduped per-key set backing /health cached: N.
    channel_dashboard.record_gid_seen(source_sort or "popular", gid)
    channel_dashboard.record_activity(last_gid=str(gid))
    # v1.25: per-page discovery record (feeds the daily admin digest) +
    # live log line so Render logs show exactly which page found new items.
    if page is not None:
        log.info("\U0001f195 NEW gid=%s found on %s page %d", gid,
                 source_sort or "popular", page)
    _record_new_on_page(source_sort or "popular", page)
    return "ok"


async def _work_page(
    client: httpx.AsyncClient, sort: str, page: int
) -> Dict[str, int]:
    """Hydrate every gallery ID visible on (sort, page). Reads the cached
    list page — no /search call is made."""
    tally = {"ok": 0, "hit": 0, "skip": 0, "rate": 0, "error": 0, "no_ids": 0}
    payload = await _read_search_cache(sort, page)
    if not payload:
        # list_sweeper hasn't written this page yet — skip cheaply.
        tally["no_ids"] = 1
        return tally
    ids = _extract_ids_any(payload)
    if not ids:
        tally["no_ids"] = 1
        return tally

    per_tick = max(1, int(settings.details_per_tick or 5))
    worked = 0
    for gid in ids:
        if worked >= per_tick:
            break
        if mongo_client.is_paused():
            break
        res = await _fetch_one_gallery(client, gid, source_sort=sort,
                                        page=page)
        tally[res if res in tally else "error"] += 1
        # Only sleep if we actually made an upstream call.
        if res in ("ok", "rate", "error"):
            worked += 1
            await asyncio.sleep(settings.details_rest_sec)
    # v1.25: one summary line per page whenever new items were found.
    if tally["ok"]:
        log.info("\U0001f195 %s page %d \u2192 %d new galleries fetched",
                 sort, page, tally["ok"])
    return tally


async def _work_external_hints(
    client: httpx.AsyncClient, gids: List[str]
) -> Dict[str, int]:
    """v12.34b: hydrate galleries the user just opened from the Mini App.

    Each gid is one gallery the user actually clicked, so warming it
    converts the next user's cold MISS into a HIT without any extra
    round-robin traffic. source_sort="user-hint" makes the new-galleries
    counter roll up under a dedicated row in the channel dashboard so we
    can see it (and only it) trend up.
    """
    tally = {"ok": 0, "hit": 0, "skip": 0, "rate": 0, "error": 0, "no_ids": 0}
    for gid in gids:
        if mongo_client.is_paused():
            break
        res = await _fetch_one_gallery(client, gid, source_sort="user-hint")
        tally[res if res in tally else "error"] += 1
        if res in ("ok", "rate", "error"):
            await asyncio.sleep(settings.details_rest_sec)
    return tally


async def sweep_once() -> Dict[str, Any]:
    """One tick:
        0) v12.34b: BOT 0 user-hint queue (highest priority — the user
                      JUST clicked, latency is what they actually feel).
        1) Priority: freshly-written list pages from list_sweeper.
        2) Round-robin: one (sort, page) advance per tick.
    """
    if mongo_client.is_paused():
        return {"skipped": "paused"}

    started = time.time()
    combined = {"ok": 0, "hit": 0, "skip": 0, "rate": 0, "error": 0, "no_ids": 0}

    client = await hf_scraper_lite.make_client()
    try:
        # 0) v12.34b: BOT 0 cross-bot user-hint queue.
        per_tick = max(1, int(settings.details_per_tick or 5))
        external = mongo_client.hint_pop_gids(min(per_tick, 4))
        if external:
            log.info("v12.34b user-hints: popping %d gid(s): %s",
                     len(external), ",".join(external))
            t = await _work_external_hints(client, external)
            for k, v in t.items():
                combined[k] = combined.get(k, 0) + v
            combined["external_hints"] = len(external)

        # 1) Priority: freshly-written list pages from list_sweeper.
        hints = await list_sweeper.drain_details_hints()
        for (sort, page) in hints[:4]:  # cap per tick
            if mongo_client.is_paused():
                break
            t = await _work_page(client, sort, page)
            for k, v in t.items():
                combined[k] = combined.get(k, 0) + v

        # 2) Round-robin: one (sort, page) advance per tick.
        if not mongo_client.is_paused():
            sort, page = _cursor_advance()
            t = await _work_page(client, sort, page)
            for k, v in t.items():
                combined[k] = combined.get(k, 0) + v
            combined["cursor"] = f"{sort}#{page}"
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass

    duration = time.time() - started
    mongo_client.state_set(_LAST_KEY, time.time())
    _stats_bump(sweeps=1)
    log.info("details sweep done %s dur=%.1fs", combined, duration)
    # v1.18: explain the commonest "did nothing" pattern so the Render log
    # (and the person reading it) isn't misled into thinking the sweeper is
    # broken. ok=0 + hit>0 + no_ids=0 means every gallery ID on the page was
    # already cached — i.e. the details cache is WARM, not failing.
    if combined.get("ok", 0) == 0 and combined.get("hit", 0) > 0 \
            and combined.get("error", 0) == 0:
        log.info("details sweep: all %d galleries already warm (cache hit, "
                 "nothing to fetch) — this is the healthy steady state",
                 combined.get("hit", 0))
    combined["duration_sec"] = round(duration, 2)
    return combined


async def run_forever(stop_event: asyncio.Event) -> None:
    log.info("details_sweeper: starting (tick=%ds per_tick=%d page_cap=%d)",
             settings.details_tick_sec, settings.details_per_tick,
             settings.details_page_cap)
    # Gentle start-up: give list_sweeper a head start so cache exists.
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=15)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        if not settings.scraper_enabled:
            log.info("scraper disabled via SCRAPER_ENABLED=0 — idling")
        else:
            try:
                await sweep_once()
            except Exception as e:  # noqa: BLE001
                log.exception("details_sweeper: unhandled: %s", e)
                _stats_bump(errors=1)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.details_tick_sec)
        except asyncio.TimeoutError:
            pass
    log.info("details_sweeper: stopped")


def status() -> Dict[str, Any]:
    sort_idx, page = _cursor_read()
    sorts = settings.list_sorts or ["popular"]
    return {
        "last_run": mongo_client.state_get(_LAST_KEY, 0),
        "stats":    _stats_read(),
        "cursor":   {"sort": sorts[sort_idx % len(sorts)], "page": page},
        "paused":   mongo_client.is_paused(),
        "enabled":  settings.scraper_enabled,
        "tick_sec": settings.details_tick_sec,
        "per_tick": settings.details_per_tick,
        "page_cap": settings.details_page_cap,
    }
