"""
discovery_digest.py — daily 10:00 IST admin broadcast of per-page discovery.

Every day at BOT1_DIGEST_TIME_IST (default "10:00", Asia/Kolkata), sends
one Telegram message to every BOT1_ADMIN_USER_IDS chat summarising where
the details_sweeper found NEW galleries over the last 24 hours:

    per sort/tag  →  per page  →  count of new items fetched there

Data source: dash_counters["per_page_new"] ring written by
details_sweeper._record_new_on_page(). Pure observability — this module
never touches nhentai, Turso writes, or the token bucket. If the bot
token or admin list is missing, it logs once per day and stays silent.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from .. import mongo_client
from ..config import settings

log = logging.getLogger("scraperbot.discovery_digest")

IST = timezone(timedelta(hours=5, minutes=30))
_K_SENT = "discovery_digest_last_sent"  # scraper1_state: last send epoch


def _parse_hhmm(raw: str) -> Tuple[int, int]:
    try:
        h, m = raw.split(":", 1)
        return int(h) % 24, int(m) % 60
    except Exception:  # noqa: BLE001
        return 10, 0


def _next_fire_epoch(now_ts: float) -> float:
    hh, mm = _parse_hhmm(getattr(settings, "digest_time_ist", "10:00"))
    now_ist = datetime.fromtimestamp(now_ts, tz=IST)
    target = now_ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target.timestamp() <= now_ts:
        target += timedelta(days=1)
    return target.timestamp()


def build_report(now_ts: float) -> Tuple[str, int]:
    """Aggregate the per_page_new ring into (message_text, total_new)."""
    cutoff = now_ts - 86400
    from . import channel_dashboard
    c = channel_dashboard._counters()
    ring = [e for e in (c.get("per_page_new") or []) if isinstance(e, list)]
    recent = [e for e in ring
              if len(e) >= 3 and isinstance(e[2], (int, float))
              and e[2] >= cutoff]

    # Prune expired entries so the ring doesn't grow stale forever.
    if len(recent) != len(ring):
        c["per_page_new"] = ring[-4000:] if ring else []
        try:
            channel_dashboard._save_counters(c)
        except Exception:  # noqa: BLE001
            pass

    if not recent:
        return ("📊 <b>Daily discovery digest</b> (last 24h)\n"
                "No new galleries were scraped today."), 0

    # by_sort: sort -> total; pages: (sort, page) -> count
    by_sort: Counter = Counter()
    pages: Counter = Counter()
    for sort, page, _ts in recent:
        s = str(sort)
        by_sort[s] += 1
        pages[(s, int(page or 0))] += 1

    lines: List[str] = ["📊 <b>Daily discovery digest</b> (last 24h)"]
    total = sum(by_sort.values())
    for sort, cnt in by_sort.most_common():
        esc = html.escape(sort)
        lines.append(f"\n<b>{esc}</b> — {cnt} new")
        for (s, page), n in sorted(
                ((k, v) for k, v in pages.items() if k[0] == sort),
                key=lambda kv: kv[0][1]):
            lines.append(f"  · page {page}: {n}")
    lines.append(f"\nTotal new galleries: <b>{total}</b>")
    text = "\n".join(lines)
    # Telegram hard cap is 4096 chars — trim per-page lines, keep totals.
    if len(text) > 3900:
        keep = lines[:1] + [ln for ln in lines[1:] if not ln.startswith("  ·")]
        keep.append(f"\n(per-page breakdown truncated; total lines: {len(lines)})")
        text = "\n".join(keep)
    return text, total


async def run_forever(stop_event: asyncio.Event) -> None:
    log.info("discovery_digest: starting (time_ist=%s)",
             getattr(settings, "digest_time_ist", "10:00"))
    while not stop_event.is_set():
        try:
            if not getattr(settings, "digest_enabled", True):
                log.info("digest disabled via BOT1_DIGEST_ENABLED=0 — idling")
            elif not settings.admin_user_ids:
                log.warning("digest: BOT1_ADMIN_USER_IDS empty — skipping send")
            elif not settings.bot_token:
                log.warning("digest: BOT1_TOKEN unset — skipping send")
            else:
                now = time.time()
                nxt = _next_fire_epoch(now)
                wait = max(5.0, nxt - now)
                log.info("digest: next send at %s IST (in %.0fs)",
                         datetime.fromtimestamp(nxt, tz=IST).strftime("%H:%M"),
                         wait)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait)
                    break  # shutdown requested
                except asyncio.TimeoutError:
                    pass
                # last-send guard (survives restarts via Mongo state)
                last = float(mongo_client.state_get(_K_SENT, 0) or 0)
                if time.time() - last < 20 * 3600:
                    continue
                from . import telegram_bot
                text, total = build_report(time.time())
                ok_all = True
                for uid in settings.admin_user_ids:
                    r = await telegram_bot.send_message(int(uid), text)
                    if not r.get("ok"):
                        ok_all = False
                        log.warning("digest send to %s failed: %s", uid,
                                    str(r)[:200])
                if ok_all:
                    mongo_client.state_set(_K_SENT, time.time())
                    log.info("digest sent to %d admin(s), %d new galleries",
                             len(settings.admin_user_ids), total)
        except Exception as e:  # noqa: BLE001
            log.exception("discovery_digest: unhandled: %s", e)
        # Safety nap — loop recomputes next fire time anyway.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
            break
        except asyncio.TimeoutError:
            pass
    log.info("discovery_digest: stopped")


def status() -> Dict[str, Any]:
    return {
        "enabled": getattr(settings, "digest_enabled", True),
        "time_ist": getattr(settings, "digest_time_ist", "10:00"),
        "last_sent": mongo_client.state_get(_K_SENT, 0),
        "admins": len(settings.admin_user_ids),
        "next_fire": _next_fire_epoch(time.time()),
    }
