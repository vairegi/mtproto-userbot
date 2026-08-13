"""
telegram_bot.py — thin Telegram Bot API wrapper for BOT 1.

Only used for the small in-chat admin surface (/status, /pause, /resume,
/trigger, /help). Webhook-driven; no long polling so it costs zero when
idle.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

import httpx

from ..auth import is_tg_admin
from ..config import settings
from . import list_sweeper, details_sweeper

log = logging.getLogger("scraperbot.telegram")

_TG = "https://api.telegram.org"


async def _api(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not settings.bot_token:
        return {"ok": False, "description": "BOT1_TOKEN not set"}
    url = f"{_TG}/bot{settings.bot_token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json=payload)
        try:
            return r.json() or {"ok": False, "description": f"HTTP {r.status_code}"}
        except Exception:  # noqa: BLE001
            return {"ok": False, "description": f"HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": f"http error: {e!s}"}


async def send_message(chat_id: int | str, text: str) -> Dict[str, Any]:
    return await _api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def _fmt_ago(ts: float) -> str:
    if not ts:
        return "never"
    delta = int(time.time() - float(ts))
    if delta < 60:  return f"{delta}s ago"
    if delta < 3600: return f"{delta // 60}m ago"
    if delta < 86400: return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _status_text() -> str:
    ls = list_sweeper.status()
    ds = details_sweeper.status()
    ls_stats = ls.get("stats") or {}
    ds_stats = ds.get("stats") or {}
    paused = ls.get("paused")
    enabled = ls.get("enabled")

    banner = "🟢 running" if (enabled and not paused) else (
        "⏸️ paused" if paused else "🔴 disabled")

    return (
        f"<b>ScraperBot (BOT 1)</b> — {banner}\n"
        f"\n"
        f"<b>List sweep</b>\n"
        f"• last run: {_fmt_ago(ls.get('last_run', 0))}\n"
        f"• sweeps: {ls_stats.get('sweeps', 0)}  "
        f"writes: {ls_stats.get('writes', 0)}  "
        f"skips: {ls_stats.get('skips', 0)}  "
        f"rate: {ls_stats.get('rate_limited', 0)}  "
        f"err: {ls_stats.get('errors', 0)}\n"
        f"• priority queue: {len(ls.get('priority') or [])}\n"
        f"\n"
        f"<b>Details sweep</b>\n"
        f"• last run: {_fmt_ago(ds.get('last_run', 0))}\n"
        f"• sweeps: {ds_stats.get('sweeps', 0)}  "
        f"writes: {ds_stats.get('writes', 0)}  "
        f"hits: {ds_stats.get('hits', 0)}  "
        f"skips: {ds_stats.get('skips', 0)}  "
        f"err: {ds_stats.get('errors', 0)}\n"
        f"• cursor: {ds.get('cursor', {})}\n"
    )


def _help_text() -> str:
    return (
        "<b>ScraperBot commands</b>\n"
        "/status    — sweep counters + last run\n"
        "/pause     — pause both sweepers (admin)\n"
        "/resume    — resume both sweepers (admin)\n"
        "/trigger   — kick a list sweep now (admin)\n"
        "/time &lt;n&gt; — set channel dashboard refresh (2–300 s, admin)\n"
        "/help      — this message"
    )


async def handle_update(update: Dict[str, Any]) -> None:
    """Dispatch a Telegram update to the matching handler. Non-commands
    are ignored (this bot has no user-facing surface besides admin ops)."""
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    if not text:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_user = (msg.get("from") or {}).get("id")

    # Strip "@BotUsername" suffix Telegram appends in groups.
    cmd = text.split()[0].split("@")[0].lower()

    if cmd == "/start" or cmd == "/help":
        await send_message(chat_id, _help_text())
        return

    if cmd == "/status":
        await send_message(chat_id, _status_text())
        return

    if cmd in ("/pause", "/resume", "/trigger", "/time"):
        if not is_tg_admin(from_user):
            await send_message(chat_id, "❌ admin only")
            return
        from .. import mongo_client
        if cmd == "/pause":
            mongo_client.set_paused(True)
            await send_message(chat_id, "⏸️ paused. /resume to continue.")
            return
        if cmd == "/resume":
            mongo_client.set_paused(False)
            await send_message(chat_id, "▶️ resumed.")
            return
        if cmd == "/trigger":
            # Fire-and-forget — return quickly so Telegram doesn't retry.
            import asyncio
            asyncio.create_task(list_sweeper.sweep_once())
            await send_message(chat_id, "🚀 list sweep kicked. /status for progress.")
            return
        if cmd == "/time":
            from . import channel_dashboard
            parts = text.split()
            if len(parts) < 2:
                cur = channel_dashboard.get_refresh_sec()
                await send_message(chat_id,
                    f"⏱ current channel refresh: <b>{cur}s</b>. "
                    f"Usage: <code>/time 10</code> (allowed 2–300).")
                return
            try:
                new_n = int(parts[1])
            except ValueError:
                await send_message(chat_id, "❌ usage: /time &lt;seconds&gt;")
                return
            applied = channel_dashboard.set_refresh_sec(new_n)
            await send_message(chat_id,
                f"⏱ channel refresh set to <b>{applied}s</b>.")
            return


async def set_webhook(base_url: str) -> Dict[str, Any]:
    """Register `<base_url>/telegram?s=<secret>` as the webhook."""
    if not settings.bot_token:
        return {"ok": False, "description": "BOT1_TOKEN not set"}
    q = f"?s={settings.webhook_secret}" if settings.webhook_secret else ""
    return await _api("setWebhook", {
        "url": f"{base_url.rstrip('/')}/telegram{q}",
        "allowed_updates": ["message", "edited_message"],
        "drop_pending_updates": True,
    })


async def delete_webhook() -> Dict[str, Any]:
    return await _api("deleteWebhook", {"drop_pending_updates": True})
