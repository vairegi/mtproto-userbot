"""
channel_dashboard.py — live 2-message dashboard in a Telegram channel.

Design
------
* Per FULL SWEEP PHASE (one complete list-sweep pass across all sorts x
  pages), post ONE numbered pair of messages:

        Message A (summary):
            [3]
            ➥ Total written galleries [06:47 UTC]: 412
            ➥ New today: 47
            ➥ New in "Popular Today": 12
            ➥ New in "Recent": 8
            ➥ New in "Popular Week": 15
            ➥ New in "Popular": 5
            ➥ New in "tag: incest": 7
            ➥ Search pages written: 84
            ➥ Errors: 2
            ➥ Bucket skips: 15

        Message B (current activity):
            [3]
            ➥ Sweeping: popular · page 6
            ➥ Last gallery: 672225
            ➥ Last tag: big-breasts

* WHILE the phase is running: edit both messages every `channel_refresh_sec`
  seconds (default 5). `/time <n>` from an admin flips this live.
* WHEN the phase ends: freeze both messages (no more edits), post a new
  numbered pair `[N+1]` at the start of the next phase.
* If a phase produced ZERO new galleries: the counter lines say
  `nothing new` instead of `0` so it's obvious the sweep ran but the
  cache was already warm.
* Tags: EVERY tag BOT 1 sweeps auto-shows up in the summary — no code
  change needed. `EXTRA_TAG_SORTS=incest,vanilla,...` env var.

Persistence
-----------
All counters + the phase number + the current pair of message IDs live
in Mongo `scraper1_state` so:
  * Render restarts don't reset the phase counter.
  * The refresh loop knows which messages to edit.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from .. import mongo_client
from ..config import settings

log = logging.getLogger("scraperbot.dashboard")

_TG = "https://api.telegram.org"

# Mongo state keys
_K_PHASE      = "dash_phase"          # int: current phase number
_K_MSG_A      = "dash_msg_a_id"       # int: message-id of summary msg
_K_MSG_B      = "dash_msg_b_id"       # int: message-id of activity msg
_K_COUNTERS   = "dash_counters"       # dict: this-phase per-sort counters
_K_TOTALS     = "dash_totals"         # dict: cumulative totals + last24h ring
_K_ACTIVITY   = "dash_activity"       # dict: cursor, last_gid, last_tag
_K_REFRESH    = "dash_refresh_sec"    # int: /time <n> override

# In-process cache of last rendered text so we don't send editMessageText
# API calls that would return 400 "message is not modified".
_last_a: str = ""
_last_b: str = ""


# ---------------------------------------------------------------------------
# Public API used by sweepers
# ---------------------------------------------------------------------------

def get_refresh_sec() -> int:
    v = mongo_client.state_get(_K_REFRESH, None)
    try:
        v = int(v) if v is not None else settings.channel_refresh_sec
    except (TypeError, ValueError):
        v = settings.channel_refresh_sec
    return max(2, min(300, v))


def set_refresh_sec(n: int) -> int:
    n = max(2, min(300, int(n)))
    mongo_client.state_set(_K_REFRESH, n)
    return n


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
# Counter mutations (called from sweepers)
# ---------------------------------------------------------------------------

def record_new_gallery(sort_or_tag: str) -> None:
    """One brand-new gallery:<id> got written this phase from `sort_or_tag`.
    Also bumps the cumulative total + the 24h ring."""
    c = _counters()
    per_sort = c.get("per_sort") or {}
    per_sort[sort_or_tag] = int(per_sort.get(sort_or_tag, 0)) + 1
    c["per_sort"] = per_sort
    c["new_galleries"] = int(c.get("new_galleries", 0)) + 1
    _save_counters(c)

    t = _totals()
    t["total_galleries"] = int(t.get("total_galleries", 0)) + 1
    # 24h ring: append (ts,) then drop entries older than 86400s
    ring = t.get("ring_24h") or []
    now = time.time()
    ring = [ts for ts in ring if isinstance(ts, (int, float)) and now - ts < 86400]
    ring.append(now)
    if len(ring) > 20000:
        ring = ring[-20000:]
    t["ring_24h"] = ring
    _save_totals(t)


def record_search_page_written() -> None:
    c = _counters()
    c["search_pages"] = int(c.get("search_pages", 0)) + 1
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
    """Update the 'current activity' message (Message B) fields.
    Empty strings are ignored so a caller can update just one field."""
    a = _activity()
    if sweeping:  a["sweeping"]  = sweeping
    if last_gid:  a["last_gid"]  = str(last_gid)
    if last_tag:  a["last_tag"]  = last_tag
    a["updated_at"] = time.time()
    _save_activity(a)


# ---------------------------------------------------------------------------
# Phase lifecycle
# ---------------------------------------------------------------------------

async def start_phase() -> None:
    """Called at the top of every list-sweeper sweep_once().

    - Bumps the phase counter [N -> N+1].
    - Resets per-phase counters.
    - Posts a new pair of messages to the channel.
    """
    phase = _phase_num() + 1
    mongo_client.state_set(_K_PHASE, phase)
    _save_counters({
        "new_galleries": 0,
        "per_sort":      {},
        "search_pages":  0,
        "errors":        0,
        "skips":         0,
        "started_at":    time.time(),
    })
    # Activity is a rolling display — don't reset it, just note phase start.
    a = _activity()
    a["phase_started_at"] = time.time()
    _save_activity(a)

    # Post the initial pair of messages.
    text_a = _render_summary(phase)
    text_b = _render_activity(phase)
    id_a = await _send_message(text_a)
    id_b = await _send_message(text_b)
    if id_a:
        mongo_client.state_set(_K_MSG_A, int(id_a))
    if id_b:
        mongo_client.state_set(_K_MSG_B, int(id_b))
    global _last_a, _last_b
    _last_a = text_a
    _last_b = text_b
    log.info("dashboard: phase %d started (msg_a=%s msg_b=%s)",
             phase, id_a, id_b)


async def end_phase() -> None:
    """Called at the bottom of every list-sweeper sweep_once().

    Final edit with the phase's terminal counters. Nothing else — the next
    phase will post a new pair via start_phase()."""
    phase = _phase_num()
    await _refresh_now(phase, final=True)


async def refresh_loop(stop_event: asyncio.Event) -> None:
    """Background task: edits both messages every N seconds while a phase
    is active. Idles quietly when no phase is running."""
    log.info("dashboard: refresh loop starting")
    while not stop_event.is_set():
        try:
            phase = _phase_num()
            if phase > 0:
                await _refresh_now(phase, final=False)
        except Exception as e:  # noqa: BLE001
            log.warning("dashboard refresh failed: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(),
                                   timeout=get_refresh_sec())
        except asyncio.TimeoutError:
            pass
    log.info("dashboard: refresh loop stopped")


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
    """popular-today -> 'Popular Today', tag:incest -> 'tag: incest'."""
    if sort_or_tag.startswith("tag:"):
        return f'tag: {sort_or_tag[4:].strip()}'
    return _SORT_LABEL.get(sort_or_tag, sort_or_tag)


def _fmt_hm_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M UTC")


def _render_summary(phase: int) -> str:
    c = _counters()
    t = _totals()
    per_sort = c.get("per_sort") or {}
    new_total = int(c.get("new_galleries", 0))
    now = time.time()

    # Total EVER written by BOT 1 (cumulative).
    total_written = int(t.get("total_galleries", 0))
    # New in last 24h (rolling).
    ring = t.get("ring_24h") or []
    new_24h = sum(1 for ts in ring if isinstance(ts, (int, float))
                  and now - ts < 86400)

    lines: List[str] = [f"[{phase}]"]
    lines.append(f"➥ Total written galleries [{_fmt_hm_utc(now)}]: {total_written}")

    # "New today" = last 24h rolling.
    if new_24h == 0:
        lines.append("➥ New today: nothing new")
    else:
        lines.append(f"➥ New today: {new_24h}")

    # Per-sort lines: always emit the 4 core sorts + every extra tag we've
    # ever seen this phase (so a fresh tag shows up automatically).
    core = ["popular-today", "date", "popular-week", "popular"]
    tags_seen = sorted(k for k in per_sort if k.startswith("tag:"))
    configured_tags = [f"tag:{t.strip()}" for t in settings.extra_tag_sorts
                       if t.strip()]
    tag_order = list(dict.fromkeys(configured_tags + tags_seen))

    for s in core + tag_order:
        n = int(per_sort.get(s, 0))
        label = _label_for(s)
        if n == 0:
            lines.append(f'➥ New in "{label}": nothing new')
        else:
            lines.append(f'➥ New in "{label}": {n}')

    pages = int(c.get("search_pages", 0))
    lines.append(f"➥ Search pages written: {pages if pages else 'nothing new'}")
    lines.append(f"➥ Errors: {int(c.get('errors', 0))}")
    lines.append(f"➥ Bucket skips: {int(c.get('skips', 0))}")

    return "\n".join(lines)


def _render_activity(phase: int) -> str:
    a = _activity()
    lines: List[str] = [f"[{phase}]"]
    sweeping  = str(a.get("sweeping")  or "-")
    last_gid  = str(a.get("last_gid")  or "-")
    last_tag  = str(a.get("last_tag")  or "-")
    lines.append(f"➥ Sweeping: {sweeping}")
    lines.append(f"➥ Last gallery: {last_gid}")
    lines.append(f"➥ Last tag: {last_tag}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram API calls
# ---------------------------------------------------------------------------

async def _tg_api(method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not settings.bot_token:
        return None
    url = f"{_TG}/bot{settings.bot_token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload)
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("dashboard tg api %s failed: %s", method, e)
        return None


async def _send_message(text: str) -> Optional[int]:
    if not settings.log_channel_id:
        return None
    r = await _tg_api("sendMessage", {
        "chat_id": settings.log_channel_id,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": True,
    })
    if not r or not r.get("ok"):
        log.warning("dashboard sendMessage failed: %s", r)
        return None
    return int(((r.get("result") or {}).get("message_id")) or 0) or None


async def _edit_message(msg_id: int, text: str) -> bool:
    if not settings.log_channel_id or not msg_id:
        return False
    r = await _tg_api("editMessageText", {
        "chat_id": settings.log_channel_id,
        "message_id": int(msg_id),
        "text": text,
        "disable_web_page_preview": True,
    })
    if not r or not r.get("ok"):
        desc = ((r or {}).get("description") or "").lower()
        # Silently ignore the "not modified" case — same text, no-op.
        if "not modified" in desc:
            return True
        log.warning("dashboard editMessage failed: %s", r)
        return False
    return True


async def _refresh_now(phase: int, *, final: bool) -> None:
    """Re-render both messages and edit them if text changed."""
    global _last_a, _last_b
    text_a = _render_summary(phase)
    text_b = _render_activity(phase)
    id_a = mongo_client.state_get(_K_MSG_A, 0) or 0
    id_b = mongo_client.state_get(_K_MSG_B, 0) or 0

    # If a message ID is missing (e.g. we lost it), post afresh.
    if not id_a:
        new_id = await _send_message(text_a)
        if new_id:
            mongo_client.state_set(_K_MSG_A, int(new_id))
    elif text_a != _last_a:
        await _edit_message(int(id_a), text_a)

    if not id_b:
        new_id = await _send_message(text_b)
        if new_id:
            mongo_client.state_set(_K_MSG_B, int(new_id))
    elif text_b != _last_b:
        await _edit_message(int(id_b), text_b)

    _last_a = text_a
    _last_b = text_b
