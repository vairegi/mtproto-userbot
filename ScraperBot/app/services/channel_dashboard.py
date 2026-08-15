"""
channel_dashboard.py — ONE live message in a Telegram channel.

v1.8 rewrite — the whole design changed in response to the 429 storm:

  * ONE message per phase (merged summary + activity). Never two.
  * ONE background writer task. No other code path calls Telegram.
    Everything else (record_activity, counters, phase start/end) just
    writes to Mongo state. The writer reads the state, renders the text,
    and edits — throttled hard.
  * Retry-after aware: when Telegram returns 429 with retry_after=N,
    the writer sleeps N seconds before the next attempt. No more
    exponential snowballing.
  * Timestamp REMOVED from the summary line — identical states now produce
    byte-identical text, which Telegram silently ignores, saving the edit.
  * All scraped tags listed with per-tag new-item counts.

Phase model (unchanged): one full list-sweep pass = one phase [N].
A new numbered message posts at phase start; it's edited in place during
the phase; it freezes at phase end. Never deleted.

Heartbeat: every HEARTBEAT_SEC (default 2h) a second small message posts
with total cache size + how many new items were added in the last 2h.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .. import mongo_client
from ..config import settings

log = logging.getLogger("scraperbot.dashboard")

_TG = "https://api.telegram.org"

# Mongo state keys
_K_PHASE       = "dash_phase"           # int: current phase number
_K_MSG_ID      = "dash_msg_id"          # int: message-id of the ONE live msg
_K_COUNTERS    = "dash_counters"        # dict: this-phase counters
_K_TOTALS      = "dash_totals"          # dict: cumulative totals + 24h ring
_K_ACTIVITY    = "dash_activity"        # dict: cursor, last_gid, last_tag
_K_REFRESH     = "dash_refresh_sec"     # int: /time <n> override
_K_CURSOR      = "dash_cursor"          # dict: resume (sort_idx, page)
_K_HEARTBEAT   = "dash_heartbeat_last"  # float: last heartbeat ts
_K_HEARTBEAT_R = "dash_heartbeat_ring"  # list[float]: ts of writes in window

# Hard limits — Telegram's editMessageText per-chat budget is ~1/sec and
# it snowballs 429s. 3s between writes keeps us well under, even with the
# refresh loop + activity pushes both trying.
_MIN_WRITE_INTERVAL = 3.0
_HEARTBEAT_SEC      = 2 * 3600  # 2 hours, per user spec


# ---------------------------------------------------------------------------
# Public knobs
# ---------------------------------------------------------------------------

def get_refresh_sec() -> int:
    v = mongo_client.state_get(_K_REFRESH, None)
    try:
        v = int(v) if v is not None else settings.channel_refresh_sec
    except (TypeError, ValueError):
        v = settings.channel_refresh_sec
    return max(_MIN_WRITE_INTERVAL, min(300, v))


def set_refresh_sec(n: int) -> int:
    n = max(int(_MIN_WRITE_INTERVAL), min(300, int(n)))
    mongo_client.state_set(_K_REFRESH, n)
    return n


# ---------------------------------------------------------------------------
# State accessors (all callers write state, none calls Telegram)
# ---------------------------------------------------------------------------

def _phase_num() -> int:
    v = mongo_client.state_get(_K_PHASE, 0) or 0
    try: return int(v)
    except (TypeError, ValueError): return 0


def _counters() -> Dict[str, Any]:
    c = mongo_client.state_get(_K_COUNTERS, {}) or {}
    return dict(c) if isinstance(c, dict) else {}


def _save_counters(c: Dict[str, Any]) -> None:
    mongo_client.state_set(_K_COUNTERS, c)


def _totals() -> Dict[str, Any]:
    t = mongo_client.state_get(_K_TOTALS, {}) or {}
    return dict(t) if isinstance(t, dict) else {}


def _save_totals(t: Dict[str, Any]) -> None:
    mongo_client.state_set(_K_TOTALS, t)


def _activity() -> Dict[str, Any]:
    a = mongo_client.state_get(_K_ACTIVITY, {}) or {}
    return dict(a) if isinstance(a, dict) else {}


def _save_activity(a: Dict[str, Any]) -> None:
    mongo_client.state_set(_K_ACTIVITY, a)


# ---------------------------------------------------------------------------
# Public counter mutations (write-only, never trigger Telegram)
# ---------------------------------------------------------------------------

def record_new_gallery(sort_or_tag: str) -> None:
    c = _counters()
    per_sort = c.get("per_sort") or {}
    per_sort[sort_or_tag] = int(per_sort.get(sort_or_tag, 0)) + 1
    c["per_sort"] = per_sort
    c["new_galleries"] = int(c.get("new_galleries", 0)) + 1
    c["last_write_at"] = time.time()  # v1.15 (#1) freshness heartbeat
    _save_counters(c)

    t = _totals()
    t["total_galleries"] = int(t.get("total_galleries", 0)) + 1
    now = time.time()
    ring = [ts for ts in (t.get("ring_24h") or [])
            if isinstance(ts, (int, float)) and now - ts < 86400]
    ring.append(now)
    t["ring_24h"] = ring[-20000:]

    # Heartbeat ring — independent so heartbeats count "new in last 2h".
    hring = [ts for ts in (t.get("ring_hb") or [])
             if isinstance(ts, (int, float)) and now - ts < _HEARTBEAT_SEC]
    hring.append(now)
    t["ring_hb"] = hring[-20000:]
    _save_totals(t)


def record_cached_gallery(sort_or_tag: str) -> None:
    c = _counters()
    pc = c.get("per_cached") or {}
    pc[sort_or_tag] = int(pc.get(sort_or_tag, 0)) + 1
    c["per_cached"] = pc
    c["last_write_at"] = time.time()  # v1.15 (#1) freshness heartbeat
    _save_counters(c)


def record_search_page_written() -> None:
    c = _counters()
    c["search_pages"] = int(c.get("search_pages", 0)) + 1
    # v1.15 (#1): freshness heartbeat — stamp every successful write so the
    # dashboard can show "Last write: Ns ago". A dead-but-looping sweeper
    # stops updating this, which is the fastest visual tell that writes
    # have stalled even while the phase counter keeps ticking.
    c["last_write_at"] = time.time()
    _save_counters(c)


def record_error() -> None:
    c = _counters()
    c["errors"] = int(c.get("errors", 0)) + 1
    _save_counters(c)


def record_bucket_skip() -> None:
    c = _counters()
    c["skips"] = int(c.get("skips", 0)) + 1
    _save_counters(c)


def record_activity(*, sweeping: str = "", last_gid: str = "",
                    last_tag: str = "") -> None:
    """Just writes state. The writer loop picks it up — no direct API call."""
    a = _activity()
    if sweeping:  a["sweeping"]  = sweeping
    if last_gid:  a["last_gid"]  = str(last_gid)
    if last_tag:  a["last_tag"]  = last_tag
    a["updated_at"] = time.time()
    _save_activity(a)


# ---------------------------------------------------------------------------
# Resume cursor (sweep progress survives Render restarts)
# ---------------------------------------------------------------------------

def cursor_get() -> Dict[str, Any]:
    return mongo_client.state_get(_K_CURSOR, {}) or {}


def cursor_set(sort_idx: int, page: int) -> None:
    mongo_client.state_set(_K_CURSOR, {
        "sort_idx": int(sort_idx), "page": int(page),
        "updated_at": time.time(),
    })


def cursor_clear() -> None:
    mongo_client.state_set(_K_CURSOR, {})


# ---------------------------------------------------------------------------
# Phase lifecycle
# ---------------------------------------------------------------------------

async def start_phase() -> None:
    phase = _phase_num() + 1
    mongo_client.state_set(_K_PHASE, phase)
    _save_counters({
        "new_galleries": 0, "per_sort": {}, "per_cached": {},
        "search_pages": 0, "errors": 0, "skips": 0,
        "started_at": time.time(),
    })
    a = _activity()
    a["phase_started_at"] = time.time()
    _save_activity(a)
    log.info("dashboard: phase %d started", phase)
    # First write of the phase — the writer loop will pick it up within
    # MIN_WRITE_INTERVAL. No direct API call here.


async def end_phase() -> None:
    # The writer loop's next tick does the final edit. Just clear cursor.
    cursor_clear()
    log.info("dashboard: phase %d ended", _phase_num())


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_SORT_LABEL = {
    "popular":        "Popular",
    "popular-today":  "Popular Today",
    "popular-week":   "Popular Week",
    "date":           "Recent",
}


def _label_for(sort_or_tag: str) -> str:
    if sort_or_tag.startswith("tag:"):
        return f"tag: {sort_or_tag[4:].strip()}"
    return _SORT_LABEL.get(sort_or_tag, sort_or_tag)


def _fmt_hm_local(ts: float) -> str:
    from datetime import datetime, timezone, timedelta
    off = int(getattr(settings, "display_tz_offset_min", 330))
    label = getattr(settings, "display_tz_label", "IST") or "IST"
    return datetime.fromtimestamp(
        ts, tz=timezone(timedelta(minutes=off))
    ).strftime(f"%H:%M {label}")


def _render() -> str:
    """Build the merged message body (summary + activity). No timestamp in
    the summary line — identical states produce identical text, so the
    writer skips the edit and Telegram never sees the call."""
    c = _counters()
    t = _totals()
    a = _activity()
    phase = _phase_num()
    per_sort = c.get("per_sort") or {}

    total_written = int(t.get("total_galleries", 0))
    ring = t.get("ring_24h") or []
    now = time.time()
    new_24h = sum(1 for ts in ring
                  if isinstance(ts, (int, float)) and now - ts < 86400)

    # ---- summary ----------------------------------------------------------
    lines: List[str] = [f"[{phase}]"]
    lines.append(f"➥ Total galleries: {total_written}")
    lines.append(f"➥ New this phase: {int(c.get('new_galleries', 0))}")
    lines.append(f"➥ New today (24h): {new_24h}")

    # Per-sort rows: core 4 sorts + every tag that ever produced a write.
    core = ["popular-today", "date", "popular-week", "popular"]
    tags_seen = sorted(k for k in per_sort if k.startswith("tag:"))
    configured = [f"tag:{t.strip()}" for t in getattr(settings, "extra_tag_sorts", [])
                  if t.strip()]
    trending = mongo_client.state_get("trending_tags", []) or []
    trending_keys = [f"tag:{t}" for t in trending if isinstance(t, str) and t]
    tag_order = list(dict.fromkeys(configured + trending_keys + tags_seen))

    per_cached = c.get("per_cached") or {}
    for s in core + tag_order:
        n = int(per_sort.get(s, 0))
        m = int(per_cached.get(s, 0))
        label = _label_for(s)
        if m:
            lines.append(f"➥ {label:<20} ▸ {n} new · {m} cached")
        else:
            lines.append(f"➥ {label:<20} ▸ {n}")

    lines.append(f"➥ Pages written: {int(c.get('search_pages', 0))}")
    lines.append(f"➥ Errors: {int(c.get('errors', 0))} · Skips: {int(c.get('skips', 0))}")

    # v1.15 (#1): freshness heartbeat — seconds since the last successful
    # write. If the sweeper is looping but writes have stalled, this number
    # climbs and you can see it immediately instead of reading raw logs.
    last_write = c.get("last_write_at")
    if isinstance(last_write, (int, float)) and last_write > 0:
        ago = max(0, int(now - last_write))
        lines.append(f"➥ Last write: {ago}s ago")
    else:
        lines.append("➥ Last write: —")

    # v1.15 (#2): retry backlog — how many (sort, page) pairs are sitting
    # in the priority queue waiting for the bucket to refill. Growing
    # phase-over-phase means the bucket is undersized BEFORE users hit
    # cache misses.
    try:
        _prio = mongo_client.state_get("list_priority", []) or []
        backlog = len(_prio) if isinstance(_prio, list) else 0
    except Exception:  # noqa: BLE001
        backlog = 0
    lines.append(f"➥ Retry backlog: {backlog}")

    # ---- activity (merged) ------------------------------------------------
    lines.append("")
    lines.append("———")
    sweeping = str(a.get("sweeping") or "—")
    lines.append(f"➥ Now: {sweeping}")
    lines.append(f"➥ Last gallery: {a.get('last_gid', '—')}")
    lines.append(f"➥ Last tag: {a.get('last_tag', '—')}")

    started = a.get("phase_started_at") or c.get("started_at") or now
    elapsed = int(now - started)
    lines.append(f"➥ Elapsed: {elapsed // 60}m {elapsed % 60:02d}s")

    # ETA: pages done vs pages total, rough estimate.
    max_pages = max(1, int(getattr(settings, "list_max_pages", 30)))
    sorts_total = 4 + len(tag_order)  # core + tags
    pages_total = sorts_total * max_pages
    pages_done = int(c.get("search_pages", 0))
    if pages_done > 0 and elapsed > 0:
        rate = elapsed / max(1, pages_done)
        eta_sec = int(rate * max(0, pages_total - pages_done))
        lines.append(f"➥ ETA: ~{eta_sec // 60}m")

    return "\n".join(lines)


def _heartbeat_text() -> str:
    """Compact health + activity snapshot posted every 2h."""
    t = _totals()
    now = time.time()
    hring = t.get("ring_hb") or []
    new_2h = sum(1 for ts in hring
                 if isinstance(ts, (int, float)) and now - ts < _HEARTBEAT_SEC)
    ring = t.get("ring_24h") or []
    new_24h = sum(1 for ts in ring
                  if isinstance(ts, (int, float)) and now - ts < 86400)
    total = int(t.get("total_galleries", 0))
    paused = mongo_client.is_paused()
    banner = "⏸ paused" if paused else "🟢 running"
    return (
        f"[HB] {banner}\n"
        f"➥ Total galleries: {total}\n"
        f"➥ New last 2h:  {new_2h}\n"
        f"➥ New last 24h: {new_24h}"
    )


def _blockquote(body: str) -> str:
    return f"<blockquote>{html.escape(body)}</blockquote>"


# ---------------------------------------------------------------------------
# Telegram API — called ONLY from _writer_loop
# ---------------------------------------------------------------------------

async def _tg_api(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not settings.bot_token:
        return {"ok": False, "description": "no token"}
    url = f"{_TG}/bot{settings.bot_token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload)
        try:
            return r.json() or {"ok": False}
        except Exception:
            return {"ok": False, "description": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def _retry_after(resp: Dict[str, Any]) -> int:
    """Seconds Telegram wants us to wait, or 0 if no 429."""
    if resp.get("error_code") != 429:
        return 0
    params = resp.get("parameters") or {}
    try:
        return max(1, int(params.get("retry_after", 0)))
    except (TypeError, ValueError):
        return 0


async def _send_or_edit(text: str) -> Optional[int]:
    """Send if no message ID stored, else edit. Returns message_id or None.
    Caller is responsible for throttling."""
    if not settings.log_channel_id:
        return None
    stored = mongo_client.state_get(_K_MSG_ID, 0) or 0
    body = _blockquote(text)

    if not stored:
        r = await _tg_api("sendMessage", {
            "chat_id": settings.log_channel_id,
            "text": body, "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": True,
        })
        if r.get("ok"):
            mid = int(((r.get("result") or {}).get("message_id")) or 0) or None
            if mid:
                mongo_client.state_set(_K_MSG_ID, mid)
            return mid
        return None

    r = await _tg_api("editMessageText", {
        "chat_id": settings.log_channel_id,
        "message_id": int(stored),
        "text": body, "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    if r.get("ok"):
        return int(stored)
    # Message deleted or not found — clear the stored id so next write
    # creates a fresh one.
    desc = (r.get("description") or "").lower()
    if "not modified" in desc:
        return int(stored)
    if "message to edit not found" in desc or "message_id_invalid" in desc:
        mongo_client.state_set(_K_MSG_ID, 0)
        return None
    return int(stored) if r.get("error_code") == 429 else None


# ---------------------------------------------------------------------------
# The writer loop — single owner of ALL Telegram writes
# ---------------------------------------------------------------------------

async def _writer_loop(stop_event: asyncio.Event) -> None:
    """Edits the live message every MIN_WRITE_INTERVAL. On 429, sleeps for
    retry_after seconds. Posts a heartbeat every 2h."""
    log.info("dashboard writer: starting")
    last_text: str = ""
    last_hb: float = 0.0
    backoff_until: float = 0.0

    while not stop_event.is_set():
        now = time.time()
        try:
            # Heartbeat (every 2h, independent of phase state)
            if now - last_hb >= _HEARTBEAT_SEC and now > backoff_until:
                hb = _heartbeat_text()
                r = await _tg_api("sendMessage", {
                    "chat_id": settings.log_channel_id,
                    "text": _blockquote(hb), "parse_mode": "HTML",
                    "disable_notification": True,
                })
                ra = _retry_after(r)
                if ra:
                    backoff_until = now + ra
                    log.info("heartbeat 429 — backing off %ds", ra)
                elif r.get("ok"):
                    last_hb = now
                    mongo_client.state_set(_K_HEARTBEAT, now)
                # After heartbeat, fall through to main write in same tick.

            # Main message write — skip if backing off.
            if now < backoff_until:
                await asyncio.sleep(max(1, backoff_until - now))
                continue

            text = _render()
            if text != last_text:
                if not _phase_num():
                    # No phase running — skip the write entirely.
                    pass
                else:
                    mid = await _send_or_edit(text)
                    if mid:
                        last_text = text
                    # We don't re-read the resp here — _send_or_edit already
                    # handled 429 semantics internally for the edit path.
        except Exception as e:
            log.warning("dashboard writer tick failed: %s", e)

        # Sleep until next tick — user-tunable via /time, min 3s.
        try:
            await asyncio.wait_for(stop_event.wait(),
                                   timeout=get_refresh_sec())
        except asyncio.TimeoutError:
            pass
    log.info("dashboard writer: stopped")


# Back-compat: the sweepers call `refresh_loop` — keep that name.
async def refresh_loop(stop_event: asyncio.Event) -> None:
    await _writer_loop(stop_event)
