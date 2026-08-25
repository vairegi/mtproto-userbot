"""
channel_dashboard.py — ONE live message in a Telegram channel.

v1.21 (2026-08-25) — fix the silent 400 storm.

Symptom seen in Render:
    POST https://api.telegram.org/bot.../sendMessage "HTTP/1.1 400 Bad Request"
    ... (every 3s, no dashboard message ever appears in the log channel)

Root cause
----------
`_tg_api` returned `{"ok": False, "description": ...}` on any HTTP != 2xx
BUT the writer never LOGGED the `description` field. So every 400 vanished
into a debug-invisible void. The three most common 400 reasons were all
possible:

  1. `chat_id` sent as a string  → Telegram wants int for `-100...` channel
     ids. httpx serialises the value verbatim.
  2. HTML parse error — a `<blockquote>` body that contained an unescaped
     `<` → 400 "Bad Request: can't parse entities". html.escape() covered
     the body itself but a stray Mongo value could still slip through.
  3. Text length > 4096 chars — long extra_tag_sorts + big activity block
     could push a single edit past the limit.

Fix
---
1. `_tg_api` now logs EVERY non-ok response at WARNING with the full
   Telegram description so the next 400 is diagnosable from the log
   alone.
2. `chat_id` is coerced to int when the value looks numeric ("-100...").
3. Switched from HTML `<blockquote>` to Markdown v1 fenced code block
   (```\ntext\n```) — no more entity-parse errors possible.
   Bonus: monospace is what the dashboard visually wants anyway.
4. Body is hard-capped at 3900 chars (safe under Telegram's 4096) with a
   `\n…` tail if truncated.
5. On persistent-error (5 consecutive failed sends), the writer pauses
   for 5 minutes so the log stops spamming and the process stays alive.
6. Broader top-level `except` in `_writer_loop` so an unexpected shape
   from Telegram never crashes the task (which is what caused occasional
   full-process restarts).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .. import mongo_client
from ..config import settings

log = logging.getLogger("scraperbot.dashboard")

_TG = "https://api.telegram.org"

# Mongo state keys
_K_PHASE       = "dash_phase"
_K_MSG_ID      = "dash_msg_id"
_K_COUNTERS    = "dash_counters"
_K_TOTALS      = "dash_totals"
_K_ACTIVITY    = "dash_activity"
_K_REFRESH     = "dash_refresh_sec"
_K_CURSOR      = "dash_cursor"
_K_HEARTBEAT   = "dash_heartbeat_last"
_K_HEARTBEAT_R = "dash_heartbeat_ring"

_MIN_WRITE_INTERVAL = 3.0
_HEARTBEAT_SEC      = 2 * 3600
_TEXT_HARD_LIMIT    = 3900          # safe under Telegram's 4096
_MAX_CONSEC_FAIL    = 5             # then pause for _PAUSE_ON_FAIL_SEC
_PAUSE_ON_FAIL_SEC  = 300


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
# State accessors
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
# Public counter mutations
# ---------------------------------------------------------------------------

def record_new_gallery(sort_or_tag: str) -> None:
    c = _counters()
    per_sort = c.get("per_sort") or {}
    per_sort[sort_or_tag] = int(per_sort.get(sort_or_tag, 0)) + 1
    c["per_sort"] = per_sort
    c["new_galleries"] = int(c.get("new_galleries", 0)) + 1
    c["last_write_at"] = time.time()
    _save_counters(c)

    t = _totals()
    t["total_galleries"] = int(t.get("total_galleries", 0)) + 1
    now = time.time()
    ring = [ts for ts in (t.get("ring_24h") or [])
            if isinstance(ts, (int, float)) and now - ts < 86400]
    ring.append(now)
    t["ring_24h"] = ring[-20000:]

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
    c["last_write_at"] = time.time()
    _save_counters(c)


def record_search_page_written() -> None:
    c = _counters()
    c["search_pages"] = int(c.get("search_pages", 0)) + 1
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
    a = _activity()
    if sweeping:  a["sweeping"]  = sweeping
    if last_gid:  a["last_gid"]  = str(last_gid)
    if last_tag:  a["last_tag"]  = last_tag
    a["updated_at"] = time.time()
    _save_activity(a)


# ---------------------------------------------------------------------------
# Resume cursor
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
    mongo_client.state_set(_K_MSG_ID, 0)
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


async def end_phase() -> None:
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


def _render() -> str:
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

    lines: List[str] = [f"[{phase}]"]
    lines.append(f"➥ Total galleries: {total_written}")
    lines.append(f"➥ New this phase: {int(c.get('new_galleries', 0))}")
    lines.append(f"➥ New today (24h): {new_24h}")

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

    last_write = c.get("last_write_at")
    if isinstance(last_write, (int, float)) and last_write > 0:
        ago = max(0, int(now - last_write))
        lines.append(f"➥ Last write: {ago}s ago")
    else:
        lines.append("➥ Last write: —")

    try:
        _prio = mongo_client.state_get("list_priority", []) or []
        backlog = len(_prio) if isinstance(_prio, list) else 0
    except Exception:
        backlog = 0
    lines.append(f"➥ Retry backlog: {backlog}")

    lines.append("")
    lines.append("———")
    sweeping = str(a.get("sweeping") or "—")
    lines.append(f"➥ Now: {sweeping}")
    lines.append(f"➥ Last gallery: {a.get('last_gid', '—')}")
    lines.append(f"➥ Last tag: {a.get('last_tag', '—')}")

    started = a.get("phase_started_at") or c.get("started_at") or now
    elapsed = int(now - started)
    lines.append(f"➥ Elapsed: {elapsed // 60}m {elapsed % 60:02d}s")

    max_pages = max(1, int(getattr(settings, "list_max_pages", 30)))
    sorts_total = 4 + len(tag_order)
    pages_total = sorts_total * max_pages
    pages_done = int(c.get("search_pages", 0))
    if pages_done > 0 and elapsed > 0:
        rate = elapsed / max(1, pages_done)
        eta_sec = int(rate * max(0, pages_total - pages_done))
        lines.append(f"➥ ETA: ~{eta_sec // 60}m")

    return "\n".join(lines)


def _heartbeat_text() -> str:
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


# ---------------------------------------------------------------------------
# Payload helpers — Markdown v1 code-fence wrap. No HTML entity risk.
# ---------------------------------------------------------------------------

_MD_ESC = str.maketrans({"`": "'", "\\": "/"})   # only chars that break a fence


def _fence(body: str) -> str:
    """Wrap `body` in a Markdown v1 fenced code block. Truncate to fit
    Telegram's 4096-char limit."""
    clean = (body or "").translate(_MD_ESC)
    if len(clean) > _TEXT_HARD_LIMIT:
        clean = clean[:_TEXT_HARD_LIMIT - 3] + "\n…"
    return "```\n" + clean + "\n```"


def _coerce_chat_id(v: Any) -> Any:
    """Send numeric chat_id as int — Telegram sometimes rejects the
    string form of a channel id ('-100...') as 'chat not found'."""
    if isinstance(v, int):
        return v
    s = str(v or "").strip()
    if s.lstrip("-").isdigit():
        try:
            return int(s)
        except ValueError:
            return s
    return s


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

async def _tg_api(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST to Bot API. ALWAYS returns a dict, ALWAYS logs the description
    on non-ok so a 400 storm can be diagnosed from Render logs.
    """
    if not settings.bot_token:
        return {"ok": False, "description": "no token"}
    url = f"{_TG}/bot{settings.bot_token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload)
        try:
            data = r.json() or {"ok": False}
        except Exception:
            data = {"ok": False,
                    "description": f"HTTP {r.status_code} body={r.text[:200]!r}"}
    except Exception as e:
        return {"ok": False, "description": f"transport error: {e}"}
    if not data.get("ok"):
        # v1.21: LOUD-LOG the real reason. "not modified" is not an error.
        desc = str(data.get("description", ""))[:400]
        code = data.get("error_code")
        if "not modified" not in desc.lower():
            log.warning("📊 tg %s failed [%s]: %s", method, code, desc)
    return data


def _retry_after(resp: Dict[str, Any]) -> int:
    if resp.get("error_code") != 429:
        return 0
    params = resp.get("parameters") or {}
    try:
        return max(1, int(params.get("retry_after", 0)))
    except (TypeError, ValueError):
        return 0


async def _send_or_edit(text: str) -> Optional[int]:
    """Send if no message ID stored, else edit. Returns message_id or None."""
    if not settings.log_channel_id:
        return None
    stored = mongo_client.state_get(_K_MSG_ID, 0) or 0
    body = _fence(text)
    chat_id = _coerce_chat_id(settings.log_channel_id)

    if not stored:
        r = await _tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": body,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "disable_notification": True,
        })
        if r.get("ok"):
            mid = int(((r.get("result") or {}).get("message_id")) or 0) or None
            if mid:
                mongo_client.state_set(_K_MSG_ID, mid)
                log.info("📊 dashboard message posted (id=%s)", mid)
            return mid
        return None

    r = await _tg_api("editMessageText", {
        "chat_id": chat_id,
        "message_id": int(stored),
        "text": body,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    })
    if r.get("ok"):
        return int(stored)
    desc = (r.get("description") or "").lower()
    if "not modified" in desc:
        return int(stored)
    if "message to edit not found" in desc or "message_id_invalid" in desc:
        log.info("📊 stored dashboard msg_id=%s is gone — will repost", stored)
        mongo_client.state_set(_K_MSG_ID, 0)
        return None
    return int(stored) if r.get("error_code") == 429 else None


# ---------------------------------------------------------------------------
# Writer loop
# ---------------------------------------------------------------------------

async def _writer_loop(stop_event: asyncio.Event) -> None:
    log.info("dashboard writer: starting")
    last_text: str = ""
    last_hb: float = 0.0
    backoff_until: float = 0.0
    consec_fail: int = 0

    while not stop_event.is_set():
        now = time.time()
        try:
            if now - last_hb >= _HEARTBEAT_SEC and now > backoff_until:
                hb = _heartbeat_text()
                r = await _tg_api("sendMessage", {
                    "chat_id": _coerce_chat_id(settings.log_channel_id),
                    "text": _fence(hb),
                    "parse_mode": "Markdown",
                    "disable_notification": True,
                })
                ra = _retry_after(r)
                if ra:
                    backoff_until = now + ra
                    log.info("heartbeat 429 — backing off %ds", ra)
                elif r.get("ok"):
                    last_hb = now
                    mongo_client.state_set(_K_HEARTBEAT, now)

            if now < backoff_until:
                await asyncio.sleep(max(1, backoff_until - now))
                continue

            text = _render()
            if text != last_text:
                if not _phase_num():
                    pass
                else:
                    mid = await _send_or_edit(text)
                    if mid:
                        last_text = text
                        consec_fail = 0
                    else:
                        # v1.21: bail-out on persistent failed sends so we
                        # don't hammer the log with a 400 line every 3s.
                        consec_fail += 1
                        if consec_fail >= _MAX_CONSEC_FAIL:
                            log.error(
                                "📊 dashboard: %d consecutive failed sends — "
                                "pausing writer for %ds. Check the WARNING "
                                "lines above for Telegram's exact reason "
                                "(e.g. 'chat not found' → bot must be admin "
                                "of the channel; 'can't parse entities' → "
                                "fixed in v1.21 via Markdown fence).",
                                consec_fail, _PAUSE_ON_FAIL_SEC,
                            )
                            backoff_until = now + _PAUSE_ON_FAIL_SEC
                            consec_fail = 0
        except Exception as e:
            log.warning("dashboard writer tick failed: %s", e)

        try:
            await asyncio.wait_for(stop_event.wait(),
                                   timeout=get_refresh_sec())
        except asyncio.TimeoutError:
            pass
    log.info("dashboard writer: stopped")


# Back-compat: the sweepers call `refresh_loop` — keep that name.
async def refresh_loop(stop_event: asyncio.Event) -> None:
    await _writer_loop(stop_event)
