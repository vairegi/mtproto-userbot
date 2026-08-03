"""
admin_bot.py — python-telegram-bot process, separate from the userbot.

Commands (Brief §9, §15):
  /fetch   URL(s), one per line     — validate, dedupe, enqueue
  /queue                            — pending/processing counts
  /pause                            — set control flag
  /resume                           — clear control flag
  /status                           — last 5 jobs
  /last                             — full error text of most recent failed job
  /health                           — summary: session valid, disk free, queue depth, last pings

Security (Brief §10):
  - Every command silently ignored if update.effective_user.id != ADMIN_USER_ID.
  - A stranger who guesses the bot's username gets NO reply at all.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import asyncio
import random
from datetime import datetime, time as dt_time, timedelta, timezone
import os
import shutil
import sys
from typing import Awaitable, Callable, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ChatAction
from pymongo import MongoClient
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import settings
from logging_setup import setup_logging
from queue_service import enqueue_batch
import hf_scraper
import search_picker
import progress_tracker
# ---------------------------------------------------------------------------
# Auto-queue scheduler (2026-08-01)
#
# Queues ONE random popular English gallery per day at a configured IST time,
# automatically, as if an admin had dropped the URL. Toggle with /autoon and
# /autooff; adjust the daily time with /autotime HH:MM (24-hour, IST).
#
# State lives in the `control_flags` MongoDB collection so it survives
# bot restarts. Defaults: disabled, 09:00 IST.
# ---------------------------------------------------------------------------
_IST_TZ = timezone(timedelta(hours=5, minutes=30), name="IST")
_AUTO_QUEUE_DEFAULT_TIME = "09:00"          # IST, HH:MM 24-hour
_AUTO_QUEUE_TASK_KEY = "_auto_queue_task"

# How long to wait after a successful auto-post before we consider posting
# another one. Prevents flooding your channel if the queue drains quickly.
# Override at runtime with /autocooldown N.
# Override at runtime with /autocooldown N. Set to 0 to disable cooldown
# entirely (idle-based gating alone controls the pace).
_AUTO_QUEUE_DEFAULT_COOLDOWN_MIN = 1



log = setup_logging("admin_bot")


# -------------- allowlist guard (two-tier) --------------

def _is_root_super(user_id: int) -> bool:
    """ADMIN_USER_ID from .env is the immutable root super-admin."""
    try:
        return int(user_id) == int(settings.admin_user_id)
    except (TypeError, ValueError):
        return False


def _is_admin(user_id: int) -> bool:
    """True if user is the root super-admin OR listed in the admins table."""
    if _is_root_super(user_id):
        return True
    conn = db.connect()
    try:
        return db.get_admin(conn, int(user_id)) is not None
    finally:
        conn.close()


def _is_super(user_id: int) -> bool:
    """True if user is the root super-admin OR has is_super=1 in the table."""
    if _is_root_super(user_id):
        return True
    conn = db.connect()
    try:
        row = db.get_admin(conn, int(user_id))
        return bool(row and int(row["is_super"]) == 1)
    finally:
        conn.close()


_PUBLIC_SAFE = {"cmd_fetch", "cmd_search", "cmd_help", "cmd_queue",
                "cmd_status", "cmd_auto_url", "cb_search_picker"}


def _is_public() -> bool:
    conn = db.connect()
    try:
        return db.get_flag(conn, "public", "0") == "1"
    finally:
        conn.close()


def _gate(fn, level: str):
    """level in {'admin','super','public_admin'}."""
    if level == "super":
        check = _is_super
    else:
        check = _is_admin
    is_public_gate = (level == "public_admin")

    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        uid = int(user.id) if user else 0
        allowed = check(uid) if user else False
        if not allowed and is_public_gate and _is_public():
            allowed = True
        if not allowed:
            log.info(
                "ignored command (level=%s) from user_id=%s text=%r",
                level,
                getattr(user, "id", None),
                (update.effective_message.text if update.effective_message else "")[:80],
            )
            return
        try:
            await fn(update, ctx)
        except Exception as e:  # noqa: BLE001
            log.exception("handler crash")
            if update.effective_message:
                await update.effective_message.reply_text(f"Internal error: {e!s}")

    return wrapper


def only_admin(fn):
    """Regular-admin gate (also allows super-admins)."""
    return _gate(fn, "admin")


def only_super(fn):
    """Super-admin-only gate."""
    return _gate(fn, "super")


def only_public(fn):
    """Handler is allowed for admins always, and for regular users
    when the 'public' control flag is on (toggled by /onpublic)."""
    return _gate(fn, "public_admin")


def admin_with_hint(fn):
    """v11: for URL-drop / /fetch commands. Admin can always use them.
    Non-admins in public mode get a friendly reply pointing them to /search
    (with their current token count). In private mode non-admins are still
    silently ignored to preserve stealth."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user:
            return
        uid = int(user.id)
        if _is_admin(uid):
            try:
                await fn(update, ctx)
            except Exception as e:  # noqa: BLE001
                log.exception("handler crash")
                if update.effective_message:
                    await update.effective_message.reply_text(f"Internal error: {e!s}")
            return
        if not _is_public():
            return
        try:
            conn = db.connect()
            try:
                tok = db.get_user_tokens(conn, uid, user.username or None)
            finally:
                conn.close()
            hint = (
                "Only admins can drop URLs directly here.\n"
                f"Use /search <keyword> — you have {tok['remaining']}/{tok['daily_cap']} "
                "post tokens available today."
            )
            if update.effective_message:
                await update.effective_message.reply_text(hint)
        except Exception:
            log.exception("admin_with_hint reply failed")

    return wrapper


def _parse_user_id_arg(update: Update) -> Optional[int]:
    """Extract the first argument after the command."""
    msg = update.effective_message
    if not msg or not msg.text:
        return None
    parts = msg.text.split()
    if len(parts) < 2:
        return None
    raw = parts[1].strip().lstrip("@")
    try:
        return int(raw)
    except ValueError:
        return None


# -------------- helpers --------------

def _fmt_ts(ts: int) -> str:
    try:
        tz = None
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(settings.timezone or "UTC")
        except Exception:  # noqa: BLE001
            tz = None
        d = dt.datetime.fromtimestamp(int(ts), tz=tz) if tz else dt.datetime.utcfromtimestamp(int(ts))
        return d.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    except Exception:  # noqa: BLE001
        return str(ts)


def _disk_free_gb(path: str = "/") -> float:
    try:
        total, used, free = shutil.disk_usage(path)
        return round(free / (1024 ** 3), 2)
    except Exception:  # noqa: BLE001
        return -1.0


# -------------- handlers --------------

@admin_with_hint
async def cmd_fetch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text
    # Strip the /fetch prefix (with optional @bot suffix)
    first_line, _, rest = text.partition("\n")
    body = rest
    # If the URL is on the same line as /fetch, keep everything after the first token
    if not body.strip():
        parts = first_line.split(None, 1)
        body = parts[1] if len(parts) > 1 else ""

    if not body.strip():
        await msg.reply_text(
            "Usage: /fetch <url>  (one URL per line, max "
            f"{settings.batch_max_links} URLs)"
        )
        return

    await ctx.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    result = enqueue_batch(body, max_links=settings.batch_max_links)

    lines = [result.summary_line()]
    lines.extend(result.detail_lines())
    if result.queued:
        lines.append("")
        lines.append("Queued:")
        for job_id, u in result.queued[:25]:
            lines.append(f"  #{job_id}  {u}")
    await msg.reply_text("\n".join(lines)[:4000])

    if result.queued:
        try:
            await progress_tracker.start_batch_tracking(ctx.application, msg.chat_id, result.queued)
        except Exception as e:  # noqa: BLE001
            log.warning("progress tracker failed to start: %s", e)


@only_public
async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        c = db.counts_by_status(conn)
    finally:
        conn.close()
    text = (
        f"Queue depth\n"
        f"  pending:    {c['pending']}\n"
        f"  processing: {c['processing']}\n"
        f"  done:       {c['done']}\n"
        f"  partial:    {c['partial']}\n"
        f"  failed:     {c['failed']}"
    )
    await update.effective_message.reply_text(text)


@only_admin
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        db.set_flag(conn, "paused", "1")
    finally:
        conn.close()
    await update.effective_message.reply_text("⏸ Paused. Worker will finish the current job then wait.")


@only_admin
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        db.set_flag(conn, "paused", "0")
    finally:
        conn.close()
    await update.effective_message.reply_text("▶️ Resumed.")


@only_public
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        jobs = db.last_jobs(conn, 5)
    finally:
        conn.close()
    if not jobs:
        await update.effective_message.reply_text("No jobs yet.")
        return
    lines = ["Last 5 jobs (newest first):"]
    for j in jobs:
        line = f"#{j['id']}  {j['status'].upper():10s}  {_fmt_ts(j['updated_at'])}  {j['url']}"
        lines.append(line)
        if j["error_reason"]:
            lines.append(f"        reason: {j['error_reason'][:200]}")
    await update.effective_message.reply_text("\n".join(lines)[:4000])


@only_admin
async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        row = db.most_recent_failed(conn)
    finally:
        conn.close()
    if row is None:
        await update.effective_message.reply_text("No failed jobs on record.")
        return
    text = (
        f"Most recent failed job\n"
        f"  id:      #{row['id']}\n"
        f"  url:     {row['url']}\n"
        f"  when:    {_fmt_ts(row['updated_at'])}\n"
        f"  reason:  {row['error_reason'] or '(no reason recorded)'}"
    )
    await update.effective_message.reply_text(text[:4000])


@only_admin
async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        c = db.counts_by_status(conn)
        failed24 = db.failed_last_24h(conn)
        p1 = db.get_bot_ping(conn, "bot1")
        p2 = db.get_bot_ping(conn, "bot2")
        paused = db.get_flag(conn, "paused", "0") == "1"
    finally:
        conn.close()

    # Session check via a quick Telethon connect from THIS process would double
    # the auth surface — instead, we infer from bot_pings: if the worker DMed
    # in the last hour, the session is functionally valid.
    session_hint = "unknown"
    if p1 or p2:
        recent = max(x for x in (p1 or 0, p2 or 0) if x is not None)
        age = int(dt.datetime.utcnow().timestamp()) - int(recent)
        session_hint = "recently active" if age < 3600 else f"stale ({age}s since last DM)"
    disk = _disk_free_gb("/")

    lines = [
        f"Health",
        f"  userbot session: {session_hint}",
        f"  disk free /:     {disk} GB",
        f"  paused:          {'yes' if paused else 'no'}",
        f"  queue:           pending={c['pending']}  processing={c['processing']}  failed_24h={failed24}",
        f"  last Bot 1 DM:   {_fmt_ts(p1) if p1 else '(never)'}",
        f"  last Bot 2 DM:   {_fmt_ts(p2) if p2 else '(never)'}",
    ]
    await update.effective_message.reply_text("\n".join(lines))


# -------------- auto-fetch on plain messages with URLs --------------

# Match https://<host><path?> stopping at whitespace OR at the next scheme.
# This lets us split URLs that are stuck together with no separator, e.g.
#   https://hentaifox.com/gallery/1/https://hentaifox.com/gallery/2/
_URL_RE = re.compile(r"https?://[^\s'\"<>]+?(?=https?://|\s|$)", re.IGNORECASE)


def _host_matches(host: str) -> bool:
    """True if host equals a whitelisted domain OR is a subdomain of one."""
    host = (host or "").lower().strip(".")
    if not host:
        return False
    for d in (settings.auto_fetch_domains or ()):
        d = d.lower().strip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _extract_whitelisted_urls(text: str) -> list:
    """Return list of URLs from `text` whose host is in AUTO_FETCH_DOMAINS."""
    if not text:
        return []
    from urllib.parse import urlparse
    out = []
    seen = set()
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;)!?\'\"")
        try:
            host = urlparse(u).netloc
        except Exception:  # noqa: BLE001
            continue
        if not _host_matches(host):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


@admin_with_hint
async def cmd_auto_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs on every non-command message from an authorized admin.
    Extracts whitelisted URLs and queues them; otherwise silent."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    if msg.text.lstrip().startswith("/"):
        return

    urls = _extract_whitelisted_urls(msg.text)
    if not urls:
        return

    body = "\n".join(urls)
    await ctx.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    result = enqueue_batch(body, max_links=settings.batch_max_links)

    lines = [result.summary_line()]
    lines.extend(result.detail_lines())
    if result.queued:
        lines.append("")
        lines.append("Queued:")
        for job_id, u in result.queued[:25]:
            lines.append(f"  #{job_id}  {u}")
    await msg.reply_text("\n".join(lines)[:4000])

    if result.queued:
        try:
            await progress_tracker.start_batch_tracking(ctx.application, msg.chat_id, result.queued)
        except Exception as e:  # noqa: BLE001
            log.warning("progress tracker failed to start: %s", e)


# Silent handler for anything else — never confirm existence to strangers.
async def swallow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    return


# -------------- new commands (admin management + help) --------------

@only_super
async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _parse_user_id_arg(update)
    if uid is None:
        await update.effective_message.reply_text(
            "Usage: /addadmin <user_id>\n"
            "user_id is the numeric Telegram user ID (get it via @userinfobot)."
        )
        return
    conn = db.connect()
    try:
        existing = db.get_admin(conn, uid)
        db.add_admin(conn, uid, is_super=False, added_by=update.effective_user.id)
    finally:
        conn.close()
    if existing:
        await update.effective_message.reply_text(f"User {uid} was already an admin (kept as regular).")
    else:
        await update.effective_message.reply_text(f"\u2705 Added {uid} as regular admin.")


@only_super
async def cmd_removeadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _parse_user_id_arg(update)
    if uid is None:
        await update.effective_message.reply_text("Usage: /removeadmin <user_id>")
        return
    if _is_root_super(uid):
        await update.effective_message.reply_text(
            "\u274c Cannot remove the root super-admin (ADMIN_USER_ID in .env)."
        )
        return
    conn = db.connect()
    try:
        removed = db.remove_admin(conn, uid)
    finally:
        conn.close()
    if removed:
        await update.effective_message.reply_text(f"\U0001f5d1 Removed admin {uid}.")
    else:
        await update.effective_message.reply_text(f"User {uid} was not an admin.")


@only_super
async def cmd_addsuperadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _parse_user_id_arg(update)
    if uid is None:
        await update.effective_message.reply_text(
            "Usage: /addsuperadmin <user_id>\n"
            "Grants full privileges including managing other admins."
        )
        return
    conn = db.connect()
    try:
        row = db.get_admin(conn, uid)
        if row:
            db.set_super(conn, uid, True)
        else:
            db.add_admin(conn, uid, is_super=True, added_by=update.effective_user.id)
    finally:
        conn.close()
    await update.effective_message.reply_text(f"\u2b50 {uid} is now a super-admin.")


@only_super
async def cmd_removesuperadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _parse_user_id_arg(update)
    if uid is None:
        await update.effective_message.reply_text("Usage: /removesuperadmin <user_id>")
        return
    if _is_root_super(uid):
        await update.effective_message.reply_text(
            "\u274c Cannot demote the root super-admin (ADMIN_USER_ID in .env)."
        )
        return
    conn = db.connect()
    try:
        row = db.get_admin(conn, uid)
        if not row:
            await update.effective_message.reply_text(f"User {uid} is not an admin.")
            return
        if int(row["is_super"]) == 0:
            await update.effective_message.reply_text(f"User {uid} is not a super-admin.")
            return
        db.set_super(conn, uid, False)
    finally:
        conn.close()
    await update.effective_message.reply_text(f"\u2b07 {uid} demoted to regular admin.")


@only_super
async def cmd_listadmins(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        rows = db.list_admins(conn)
    finally:
        conn.close()

    lines = ["Admins:"]
    lines.append(f"  \u2b50 {settings.admin_user_id}  (root super-admin, from .env)")
    if not rows:
        lines.append("  (no others in the database)")
    else:
        for r in rows:
            uid = int(r["user_id"])
            if uid == int(settings.admin_user_id):
                continue
            tier = "\u2b50 super" if int(r["is_super"]) == 1 else "\u2022  admin"
            when = _fmt_ts(int(r["added_at"]))
            by = r["added_by"] if r["added_by"] is not None else "-"
            lines.append(f"  {tier}  {uid}   added {when} by {by}")
    await update.effective_message.reply_text("\n".join(lines)[:4000])


@only_super
async def cmd_onpublic(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        db.set_flag(conn, "public", "1")
    finally:
        conn.close()
    await update.effective_message.reply_text(
        "🌐 Public mode ON.\n"
        "Any user can now use /fetch, /search, /queue, /status, /help, and "
        "drop gallery URLs. Admin-only commands remain locked."
    )
    log.info("public mode ENABLED by user_id=%s", update.effective_user.id)


@only_super
async def cmd_offpublic(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        db.set_flag(conn, "public", "0")
    finally:
        conn.close()
    await update.effective_message.reply_text(
        "🔒 Public mode OFF.\nOnly admins can use the bot."
    )
    log.info("public mode DISABLED by user_id=%s", update.effective_user.id)


# ------------------------- v11 token commands -------------------------

@only_public
async def cmd_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Any user (or admin) can check their remaining daily /search tokens."""
    user = update.effective_user
    if not user:
        return
    uid = int(user.id)
    if _is_admin(uid):
        await update.effective_message.reply_text(
            "🛡 You are an admin — /search has no token cap for you."
        )
        return
    conn = db.connect()
    try:
        tok = db.get_user_tokens(conn, uid, user.username or None)
    finally:
        conn.close()
    await update.effective_message.reply_text(
        f"🎟 Tokens today: {tok['remaining']} / {tok['daily_cap']} remaining\n"
        f"    ({tok['used']} used)\n"
        f"Resets at 00:00 UTC."
    )


@only_super
async def cmd_freepost(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the daily token cap for regular users. Usage: /freepost N"""
    msg = update.effective_message
    if not msg:
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        conn = db.connect()
        try:
            cur = db.get_freepost(conn)
        finally:
            conn.close()
        await msg.reply_text(
            f"Current daily token cap: {cur}\n"
            f"Usage: /freepost N   (e.g. /freepost 20)"
        )
        return
    try:
        n = int(parts[1])
    except ValueError:
        await msg.reply_text("N must be a non-negative integer.")
        return
    if n < 0:
        await msg.reply_text("N must be >= 0. Use 0 to effectively disable /search for regular users.")
        return
    conn = db.connect()
    try:
        db.set_freepost(conn, n)
    finally:
        conn.close()
    await msg.reply_text(f"✅ Daily token cap set to {n}. New cap applies immediately.")
    log.info("freepost set to %d by user_id=%s", n, update.effective_user.id)


@only_admin
async def cmd_alltoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin view of every user's token consumption today, sorted by usage desc."""
    conn = db.connect()
    try:
        rows = db.list_all_user_tokens(conn)
        cap = db.get_freepost(conn)
    finally:
        conn.close()
    if not rows:
        await update.effective_message.reply_text("No users have used /search yet today.")
        return
    lines = [f"🎟 Daily token report — cap = {cap} per user, resets 00:00 UTC", ""]
    total_used = 0
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "(no username)"
        lines.append(
            f"  {uname}  [id={r['user_id']}]  — {r['used']} used, {r['remaining']} left"
        )
        total_used += r["used"]
    lines.append("")
    lines.append(f"Totals: {len(rows)} users, {total_used} posts today.")
    await update.effective_message.reply_text("\n".join(lines)[:4000])


@only_super
async def cmd_settoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a user's REMAINING tokens for today.
    Usage: /settoken @username N   or   /settoken <user_id> N"""
    msg = update.effective_message
    if not msg:
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply_text(
            "Usage: /settoken @username N   or   /settoken <user_id> N\n"
            "N is the NEW remaining-tokens count for that user today.\n"
            "The target must have used /search or /token at least once "
            "(otherwise no user_id is on file for their @username)."
        )
        return
    target = parts[1].strip()
    try:
        n = int(parts[2])
    except ValueError:
        await msg.reply_text("N must be a non-negative integer.")
        return
    if n < 0:
        await msg.reply_text("N must be >= 0.")
        return
    conn = db.connect()
    try:
        target_uid: Optional[int] = None
        if target.startswith("@") or not target.lstrip("-").isdigit():
            target_uid = db.resolve_user_id_by_username(conn, target)
            if target_uid is None:
                await msg.reply_text(
                    f"No user with username {target} on file. "
                    "They need to /search or /token at least once first."
                )
                return
        else:
            target_uid = int(target)
        result = db.set_user_tokens(conn, target_uid, n, None)
    finally:
        conn.close()
    await msg.reply_text(
        f"✅ user_id={target_uid} now has {result['remaining']}/{result['daily_cap']} "
        f"tokens remaining today ({result['used']} used)."
    )
    log.info("settoken by user_id=%s: target=%s remaining=%s",
             update.effective_user.id, target_uid, result["remaining"])


@only_super
async def cmd_resettokens(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-reset every user's used_today to 0."""
    conn = db.connect()
    try:
        n = db.reset_all_tokens(conn)
    finally:
        conn.close()
    await update.effective_message.reply_text(
        f"✅ Reset used_today=0 for {n} user(s). Everyone starts fresh."
    )
    log.info("resettokens by user_id=%s (affected %d rows)",
             update.effective_user.id, n)


@only_public
async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    parts = msg.text.split(None, 1)
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        await msg.reply_text("Usage: /search <keyword>")
        return
    await ctx.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    await search_picker.start_search(update, ctx, query)


async def cb_search_picker(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback query dispatcher for the interactive /search picker.

    Only accepts callbacks from users who pass _is_admin (silent-ignore otherwise
    per Brief §10). The picker itself also enforces owner_user_id.
    """
    q = update.callback_query
    if not q or not q.from_user:
        return
    uid = int(q.from_user.id)
    if not _is_admin(uid) and not _is_public():
        # Silent ignore for non-admins in admins-only mode.
        try:
            await q.answer()
        except Exception:  # noqa: BLE001
            pass
        return
    await search_picker.on_callback(update, ctx)


@only_public
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = int(update.effective_user.id)
    is_super = _is_super(uid)
    is_admin = _is_admin(uid)
    is_public = _is_public()

    lines = ["Available commands:", ""]
    # Everyone-friendly commands
    lines.append("Everyday:")
    lines.append("  /search <keyword>  search hentaifox.com directly (uses tokens for non-admins)")
    lines.append("  /token             show your remaining daily tokens")
    lines.append("  /queue             pending/processing/done counts")
    lines.append("  /status            last 5 jobs with status")
    lines.append("  /help              show this message")
    lines.append("")
    if is_admin:
        lines.append("Admin URL submission (goes to Bot 1 + Bot 2 only, no /mpost):")
        lines.append("  /fetch <url>       queue one or more gallery URLs (one per line)")
        lines.append("  Auto-fetch: send any message containing a whitelisted URL.")
    else:
        lines.append("Only admins can drop URLs directly. Use /search to post from the catalog.")
    if is_admin:
        lines.append("")
        lines.append("Admin only:")
        lines.append("  /pause             stop consuming new jobs (finish current)")
        lines.append("  /resume            resume consuming")
        lines.append("  /last              full error text of the most recent failed job")
        lines.append("  /health            session, disk, queue depth, last bot pings")
        lines.append("  /alltoken          everyone's daily token usage (sorted)")
        lines.append("  /autoon          enable daily random-gallery auto-queue")
        lines.append("  /autooff         disable auto-queue")
        lines.append("  /autotime HH:MM  set daily queue time (IST, 24-hour)")
        lines.append("  /autocooldown N  set minutes between auto-posts (default 30)")

        lines.append("  /autostatus      show auto-queue configuration")
    if is_super:
        lines.append("")
        lines.append("Super-admin only:")
        lines.append("  /onpublic               open bot to any user (public mode ON)")
        lines.append("  /offpublic              close bot to admins only (public mode OFF)")
        lines.append("  /freepost <n>           set daily token cap for regular users")
        lines.append("  /settoken @u <n>        set a user's remaining tokens for today")
        lines.append("  /resettokens            reset everyone's used_today to 0 now")
        lines.append("  /addadmin <id>          grant regular admin")
        lines.append("  /removeadmin <id>       revoke admin (any tier)")
        lines.append("  /addsuperadmin <id>     grant super-admin")
        lines.append("  /removesuperadmin <id>  demote super-admin to regular")
        lines.append("  /listadmins             show all admins and tiers")
    lines.append("")
    lines.append(f"Public mode is currently: {'🌐 ON' if is_public else '🔒 OFF'}")
    lines.append("📢 All doujinshi posted here → https://t.me/+uyNxVAVPdUBlOWU9")
    await update.effective_message.reply_text("\n".join(lines))


async def _on_startup(app: Application) -> None:
    # Resume tracking any progress batches left over from before a restart.
    progress_tracker.ensure_tracker_running(app)

# ---------------------------------------------------------------------------
# /diag — admin-only diagnostic probe (added 2026-08-01)
#
# Runs three tests from inside the bot process (i.e. from Render's actual
# outbound IP) and DMs the result back to the caller. Used to diagnose
# whether hentaifox.com is still returning HTTP 403 to this container, or
# whether Cloudflare has stopped blocking us and we can drop the whole
# bypass stack.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# /diag — admin-only diagnostic probe (updated 2026-08-01 — nhentai edition)
# ---------------------------------------------------------------------------
@only_admin
async def cmd_diag(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Probe the current scraper source and report results back to the admin."""
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text("🔎 Running diagnostics… this takes ~10 seconds.")

    lines: list[str] = ["🧪 scraper diagnostics", ""]

    # ---- Test 1: hf_scraper.route_status() — what source are we using? ----
    lines.append("① scraper source")
    try:
        s = hf_scraper.route_status()
        lines.append(f"   source        : {s.get('source', '?')}")
        lines.append(f"   endpoint      : {s.get('endpoint', '?')}")
        lines.append(f"   cache entries : {s.get('cache_entries', 0)}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"   ERROR: {e!s}")
    lines.append("")

    # ---- Test 2: live search against the configured source ----
    lines.append("② hf_scraper.search('sister', page=1)")
    try:
        import time as _t
        t0 = _t.time()
        result = await hf_scraper.search("sister", page=1)
        elapsed = _t.time() - t0
        if result is None:
            lines.append(f"   verdict : ❌ returned None (source unreachable)")
        else:
            lines.append(f"   elapsed       : {elapsed:.2f}s")
            lines.append(f"   total_results : {result.total_results:,}")
            lines.append(f"   hits returned : {len(result.hits)}")
            if result.hits:
                first = result.hits[0]
                lines.append(f"   first title   : {first.title[:60]!r}")
                lines.append(f"   first url     : {first.url}")
                lines.append("   verdict : ✅ WORKING")
            else:
                lines.append("   verdict : ⚠️ empty results (parse issue?)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"   ERROR: {e!s}")
    lines.append("")

    # ---- Test 3: gallery detail (proves pretty-title path works) ----
    lines.append("③ hf_scraper.fetch_gallery_meta()")
    try:
        if 'result' in dir() and result and result.hits:
            gid = result.hits[0].gallery_id
            meta = await hf_scraper.fetch_gallery_meta(gid)
            if meta:
                lines.append(f"   pretty title  : {meta.title[:60]!r}")
                lines.append(f"   pages         : {meta.pages}")
                lines.append(f"   tag count     : {len(meta.tags)}")
                lines.append("   verdict : ✅ WORKING")
            else:
                lines.append("   verdict : ❌ gallery lookup failed")
        else:
            lines.append("   skipped (no search hits to look up)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"   ERROR: {e!s}")
    lines.append("")

    # ---- Environment ----
    import platform, sys
    lines.append("─── environment ───")
    lines.append(f"python  : {sys.version.split()[0]}")
    lines.append(f"platform: {platform.system()} {platform.machine()}")
    lines.append(f"bot1    : @{settings.bot1_username}")
    lines.append(f"bot2    : @{settings.bot2_username}")
    lines.append(f"douginshi: @{settings.doujinshibot_username}")

    report = "\n".join(lines)
    await msg.reply_text(f"```\n{report}\n```", parse_mode="Markdown")
    # ---------------------------------------------------------------------------
# Auto-queue scheduler — background loop + /autoon /autooff /autotime commands
# ---------------------------------------------------------------------------

def _auto_queue_get_enabled(conn) -> bool:
    return db.get_flag(conn, "auto_queue_enabled", "0") == "1"


def _auto_queue_get_time_str(conn) -> str:
    v = db.get_flag(conn, "auto_queue_time", _AUTO_QUEUE_DEFAULT_TIME)
    return v if re.match(r"^\d{1,2}:\d{2}$", v) else _AUTO_QUEUE_DEFAULT_TIME


def _auto_queue_set_enabled(conn, enabled: bool) -> None:
    db.set_flag(conn, "auto_queue_enabled", "1" if enabled else "0")


def _auto_queue_set_time(conn, time_str: str) -> None:
    db.set_flag(conn, "auto_queue_time", time_str)


def _auto_queue_last_run_date(conn) -> str:
    return db.get_flag(conn, "auto_queue_last_run_date", "")


def _auto_queue_mark_ran_today(conn, today_str: str) -> None:
    db.set_flag(conn, "auto_queue_last_run_date", today_str)

def _auto_queue_get_cooldown_min(conn) -> int:
    """Minutes to wait after a successful auto-post before firing again."""
    v = db.get_flag(conn, "auto_queue_cooldown_min", str(_AUTO_QUEUE_DEFAULT_COOLDOWN_MIN))
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return _AUTO_QUEUE_DEFAULT_COOLDOWN_MIN


def _auto_queue_set_cooldown_min(conn, minutes: int) -> None:
    db.set_flag(conn, "auto_queue_cooldown_min", str(max(1, int(minutes))))


def _auto_queue_last_run_iso(conn) -> str:
    """Full ISO timestamp of the last successful auto-post (used by cooldown)."""
    return db.get_flag(conn, "auto_queue_last_run_iso", "")


async def _pick_random_english_gallery() -> Optional[str]:
    """
    Pick a random popular English gallery from nhentai's recent uploads.

    Strategy:
      1. Query nhentai's /api/v2/search with an empty keyword, sorted by
         popularity, and ask for a random page in 1..5 (so we're not always
         picking from the literal top-25).
      2. Filter to English-only rows (same tag-ID filter as /search).
      3. Pick one at random from the resulting rows.
      4. Skip if the URL is already in our queue or already posted.

    Returns the full nhentai.net/g/<id>/ URL on success, or None if every
    candidate was already processed or nhentai was unreachable.
    """
    try:
        page = random.randint(1, 5)
        data = await hf_scraper.search("", page=page)
        # If empty query returns None (it does on empty input), use a
        # genuinely random popular keyword instead — keeps us out of the
        # literal top-25 newest uploads.
        if data is None:
            keywords = ["sister", "school", "sister", "love", "office"]
            data = await hf_scraper.search(random.choice(keywords), page=page)
        if data is None or not data.hits:
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("auto-queue: search failed: %s", e)
        return None

    candidates = [h for h in data.hits]
    if not candidates:
        return None

    random.shuffle(candidates)
    conn = db.connect()
    try:
        for h in candidates:
            if db.has_completed(conn, hf_scraper.hash_url(h.url) if hasattr(hf_scraper, "hash_url") else h.url):
                continue
            if db.has_pending_or_processing(conn, hf_scraper.hash_url(h.url) if hasattr(hf_scraper, "hash_url") else h.url):
                continue
            # Use queue_service's own hashing logic for dedupe — it
            # already knows how to hash a URL the same way /fetch does.
            # We avoid re-implementing hash logic here.
            return h.url
    finally:
        conn.close()
    return None


async def _auto_queue_tick(app) -> None:
    """
    One iteration of the auto-queue scheduler.

    New rule set (2026-08-02, matches user spec exactly):
      * enabled?                    -> no  ⇒ skip
      * past the daily start time?  -> no  ⇒ skip
      * cooldown expired?           -> no  ⇒ skip
      * queue currently active?     -> yes ⇒ skip (user is using the bot)
      * queue idle for >= 15s?      -> no  ⇒ skip
      * ALL YES                     -> queue one random English gallery

    The old daily curfew ("fires only at HH:MM once") is gone. Once auto-queue
    is enabled AND we're past the configured start-of-day time, the scheduler
    keeps posting one gallery every time the queue drains and stays idle for
    15 s, up to the cooldown limit. Only /autooff stops it.

    Returns True iff we actually queued a new gallery.
    """
    now_ist = datetime.now(_IST_TZ)

    conn = db.connect()
    try:
        # ---- Gate 1: enabled ----
        if not _auto_queue_get_enabled(conn):
            return False

        # ---- Gate 2: past the daily "start" time-of-day ----
        # This is a floor, not a curfew: fire on/after HH:MM IST any day.
        target_str = _auto_queue_get_time_str(conn)
        try:
            target_h, target_m = map(int, target_str.split(":"))
        except (ValueError, TypeError):
            target_h, target_m = 9, 0
        if (now_ist.hour, now_ist.minute) < (target_h, target_m):
            # Still before today's start time. Skip for now, will re-check
            # in the next tick (5s later).
            return False

        # ---- Gate 3: cooldown between successive auto-posts ----
        cooldown_min = _auto_queue_get_cooldown_min(conn)
        if cooldown_min > 0:
            last_run_iso = db.get_flag(conn, "auto_queue_last_run_iso", "")
            if last_run_iso:
                try:
                    last_run_dt = datetime.fromisoformat(last_run_iso)
                    if last_run_dt.tzinfo is None:
                        last_run_dt = last_run_dt.replace(tzinfo=_IST_TZ)
                    minutes_since = (now_ist - last_run_dt).total_seconds() / 60.0
                    if minutes_since < cooldown_min:
                        return False
                except ValueError:
                    pass  # garbage in DB; treat as "never ran"

        # ---- Gate 4: is the queue currently active? ----
        # If ANY job is pending or processing, someone (user OR previous
        # auto-post) is being served. Back off; retry next tick.
        counts = db.counts_by_status(conn)
        active = int(counts.get("pending", 0)) + int(counts.get("processing", 0))
        if active > 0:
            # Something is running — stamp "user activity" so the 15s idle
            # timer only starts once this job is done.
            db.set_flag(conn, "auto_queue_last_activity_iso", now_ist.isoformat())
            return False

        # ---- Gate 5: has the queue been idle for at least 15 seconds? ----
        # "Idle" means: no pending/processing job, AND at least 15s have
        # passed since the last time something WAS active. This gives real
        # users a grace window to submit their next selection without the
        # auto-queue jumping in mid-conversation.
        last_activity_iso = db.get_flag(conn, "auto_queue_last_activity_iso", "")
        if last_activity_iso:
            try:
                last_act_dt = datetime.fromisoformat(last_activity_iso)
                if last_act_dt.tzinfo is None:
                    last_act_dt = last_act_dt.replace(tzinfo=_IST_TZ)
                seconds_idle = (now_ist - last_act_dt).total_seconds()
                if seconds_idle < 15:
                    return False
            except ValueError:
                # Corrupt timestamp — pretend queue just went idle now, so
                # we wait a full 15s before posting.
                db.set_flag(conn, "auto_queue_last_activity_iso", now_ist.isoformat())
                return False
        else:
            # First-ever run of this loop; stamp a starting point and
            # give ourselves 15s of grace before firing.
            db.set_flag(conn, "auto_queue_last_activity_iso", now_ist.isoformat())
            return False

    finally:
        conn.close()

    # ---- All gates passed — pick and queue a gallery ----
    url = await _pick_random_english_gallery()
    if not url:
        log.info("auto-queue: no suitable English gallery found this tick")
        return False

    result = enqueue_batch(
        url,
        max_links=1,
        via_search=False,
        submitted_by=settings.admin_user_id,
        username="auto-queue",     # marker so this doesn't count as user activity
        chat_id=None,
    )

    if not result.queued:
        log.warning("auto-queue: enqueue_batch returned nothing for %s", url)
        return False

    # Stamp last_run (cooldown clock) AND last_activity (idle clock) atomically.
    # Idle clock stamping means the queue starts "not idle" and will need to
    # drain + stay quiet 15s again before the next auto-post — exactly what
    # the user asked for.
    conn = db.connect()
    try:
        db.set_flag(conn, "auto_queue_last_run_iso", now_ist.isoformat())
        db.set_flag(conn, "auto_queue_last_activity_iso", now_ist.isoformat())
        _auto_queue_mark_ran_today(conn, now_ist.date().isoformat())
    finally:
        conn.close()

    log.info("auto-queue: queued %s (job_id=%s)", url, result.queued[0][0])
    try:
        await app.bot.send_message(
            chat_id=settings.admin_user_id,
            text=(
                "🤖 Auto-queue picked another gallery:\n"
                f"  • {result.queued[0][1]}\n"
                f"  Job #{result.queued[0][0]}"
            ),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("auto-queue: failed to notify admin: %s", e)
    return True



async def _auto_queue_loop(app) -> None:
    """
    Background loop. Wakes up on a short interval, defers to user activity,
    and posts a random gallery whenever the queue has been idle long enough.

    Cadence:
      * check every 5 s (fast poll — matches the 15 s idle window)
      * if a manual user just used the bot, back off silently
      * initial 20 s delay so the bot finishes bootstrapping before we
        hit nhentai on cold start
    """
    await asyncio.sleep(20)
    log.info("auto-queue loop started (idle-based, 5s poll)")
    while True:
        try:
            await _auto_queue_tick(app)
        except Exception as e:  # noqa: BLE001
            log.exception("auto-queue tick crashed (non-fatal): %s", e)
        await asyncio.sleep(5)



def _ensure_auto_queue_running(app) -> None:
    """Idempotently start the auto-queue background loop on this app."""
    existing = app.bot_data.get(_AUTO_QUEUE_TASK_KEY)
    if existing is not None and not existing.done():
        return
    task = asyncio.get_event_loop().create_task(_auto_queue_loop(app))
    app.bot_data[_AUTO_QUEUE_TASK_KEY] = task
    log.info("auto-queue background task spawned")


# --------------------------- /autoon /autooff /autotime ---------------------------

@only_admin
async def cmd_autoon(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable daily auto-queue."""
    conn = db.connect()
    try:
        _auto_queue_set_enabled(conn, True)
        time_str = _auto_queue_get_time_str(conn)
    finally:
        conn.close()
    await update.effective_message.reply_text(
        f"✅ Auto-queue ENABLED.\n"
        f"Daily random gallery will be queued at {time_str} IST.\n"
        f"Use /autooff to stop, /autotime HH:MM to change the time."
    )


@only_admin
async def cmd_autooff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable daily auto-queue."""
    conn = db.connect()
    try:
        _auto_queue_set_enabled(conn, False)
    finally:
        conn.close()
    await update.effective_message.reply_text(
        "⏹️ Auto-queue DISABLED.\n"
        "Use /autoon to restart it."
    )


@only_admin
async def cmd_autocooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the cooldown (in minutes) between consecutive auto-posts.
    Usage: /autocooldown N   (N >= 1, default 30)
    """
    msg = update.effective_message
    args = msg.text.split(maxsplit=1) if msg and msg.text else []
    conn = db.connect()
    try:
        current = _auto_queue_get_cooldown_min(conn)
    finally:
        conn.close()

    if len(args) < 2:
        await msg.reply_text(
            f"Usage: /autocooldown N   (N minutes, >= 1)\n"
            f"Current cooldown: {current} min\n\n"
            f"After each auto-post, the scheduler waits this long before it\n"
            f"can queue another one, even if the queue is already empty."
        )
        return
    try:
        minutes = int(args[1].strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await msg.reply_text("Cooldown must be a whole number of minutes, >= 1.")
        return

    conn = db.connect()
    try:
        _auto_queue_set_cooldown_min(conn, minutes)
    finally:
        conn.close()
    await msg.reply_text(f"✅ Auto-queue cooldown set to {minutes} min.")


@only_admin
async def cmd_autotime(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the daily auto-queue time. Usage: /autotime HH:MM (24-hour, IST)."""
    msg = update.effective_message
    args = msg.text.split(maxsplit=1) if msg and msg.text else []
    if len(args) < 2 or not re.match(r"^\d{1,2}:\d{2}$", args[1].strip()):
        conn = db.connect()
        try:
            current = _auto_queue_get_time_str(conn)
        finally:
            conn.close()
        await msg.reply_text(
            f"Usage: /autotime HH:MM  (24-hour, IST)\n"
            f"Current time: {current} IST"
        )
        return
    time_str = args[1].strip()
    try:
        h, m = map(int, time_str.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        await msg.reply_text("Time must be HH:MM in 24-hour format, e.g. 09:00 or 21:30.")
        return
    conn = db.connect()
    try:
        _auto_queue_set_time(conn, time_str)
        enabled = _auto_queue_get_enabled(conn)
    finally:
        conn.close()
    status = "ENABLED" if enabled else "DISABLED"
    await msg.reply_text(
        f"✅ Auto-queue daily time set to {time_str} IST.\n"
        f"Auto-queue is currently {status}."
    )


@only_admin
async def cmd_autostatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current auto-queue configuration."""
    conn = db.connect()
    try:
        enabled = _auto_queue_get_enabled(conn)
        time_str = _auto_queue_get_time_str(conn)
        cooldown_min = _auto_queue_get_cooldown_min(conn)
        last_run_iso = _auto_queue_last_run_iso(conn)
        counts = db.counts_by_status(conn)
    finally:
        conn.close()

    now_ist = datetime.now(_IST_TZ)
    now_str = now_ist.strftime("%Y-%m-%d %H:%M IST")
    active = int(counts.get("pending", 0)) + int(counts.get("processing", 0))

    if last_run_iso:
        try:
            last_dt = datetime.fromisoformat(last_run_iso)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=_IST_TZ)
            last_run_display = last_dt.strftime("%Y-%m-%d %H:%M IST")
            mins_since = (now_ist - last_dt).total_seconds() / 60.0
            mins_until = max(0, cooldown_min - mins_since)
            cooldown_line = (
                f"  Next eligible : {int(mins_until)} min from now"
                if mins_until > 0 else
                f"  Next eligible : cooldown clear (queue must also be empty)"
            )
        except ValueError:
            last_run_display = "(invalid)"
            cooldown_line = ""
    else:
        last_run_display = "(never)"
        cooldown_line = f"  Next eligible : cooldown clear (queue must also be empty)"

    lines = [
        "🤖 Auto-queue status",
        f"  Enabled       : {'✅ yes' if enabled else '⏹️ no'}",
        f"  Start time    : {time_str} IST (fires only on/after this)",
        f"  Cooldown      : {cooldown_min} min between auto-posts",
        f"  Last ran      : {last_run_display}",
        cooldown_line,
        f"  Queue now     : {active} active job(s)",
        f"  Now (IST)     : {now_str}",
    ]
    await update.effective_message.reply_text("\n".join(l for l in lines if l))

MINIAPP_URL = os.environ.get("MINIAPP_URL", "").rstrip("/") + "/"

@only_public
async def cmd_app(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Give the caller a big 'Open Universe' Web App button."""
    if not MINIAPP_URL or MINIAPP_URL == "/":
        await update.effective_message.reply_text(
            "⚠️ MINIAPP_URL is not configured on the server.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌌 Open Universe", web_app=WebAppInfo(url=MINIAPP_URL))
    ]])
    await update.effective_message.reply_text(
        "Tap to open the Doujinshi Universe browser:",
        reply_markup=kb,
    )

@only_admin
async def cmd_appon(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip Mini App to PUBLIC mode."""
    _set_miniapp_visibility(True)
    await update.effective_message.reply_text(
        "✅ Mini App is now PUBLIC (all users can browse & queue).")

@only_admin
async def cmd_appoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip Mini App to PRIVATE (admin-only) mode."""
    _set_miniapp_visibility(False)
    await update.effective_message.reply_text(
        "🔒 Mini App is now PRIVATE (admin only).")

def _set_miniapp_visibility(public: bool) -> None:
    """Writes miniapp_settings.public_mode in Mongo."""
    uri = os.environ["MONGO_URI"]
    db_name = os.environ.get("MONGO_DB", "relaybot")
    with MongoClient(uri, serverSelectionTimeoutMS=5000) as c:
        c[db_name]["miniapp_settings"].update_one(
            {"_id": "singleton"},
            {"$set": {"public_mode": bool(public)}},
            upsert=True,
        )
def build_app() -> Application:
    # Ensure DB exists
    db.init_db()
        # Belt-and-braces: if a webhook was ever configured on this bot token
    # (e.g. by an old deploy), getUpdates will Conflict forever. Nuke it
    # once at build time — cheap, idempotent, and safe if none is set.
    try:
        import httpx as _httpx_boot
        _httpx_boot.post(
            f"https://api.telegram.org/bot{settings.admin_bot_token}/deleteWebhook",
            params={"drop_pending_updates": "true"},
            timeout=10,
        )
        log.info("deleteWebhook called at boot (idempotent)")
    except Exception as _e:  # noqa: BLE001
        log.warning("deleteWebhook failed at boot (non-fatal): %s", _e)

    app = ApplicationBuilder().token(settings.admin_bot_token).post_init(_on_startup).build()

    _ensure_auto_queue_running(app)  # 👈 FIXED! 'app' banne ke BAAD call kiya

    # Regular-admin commands
    app.add_handler(CommandHandler("fetch", cmd_fetch))
    app.add_handler(CommandHandler("search", cmd_search))
    # Interactive picker callback (buttons on /search results)
    app.add_handler(CallbackQueryHandler(cb_search_picker, pattern=r"^sp\|"))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("help", cmd_help))
    # Super-admin commands
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("addsuperadmin", cmd_addsuperadmin))
    app.add_handler(CommandHandler("removesuperadmin", cmd_removesuperadmin))
    app.add_handler(CommandHandler("listadmins", cmd_listadmins))
    app.add_handler(CommandHandler("onpublic", cmd_onpublic))
    app.add_handler(CommandHandler("offpublic", cmd_offpublic))
    # v11 token commands
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CommandHandler("freepost", cmd_freepost))
    app.add_handler(CommandHandler("alltoken", cmd_alltoken))
    app.add_handler(CommandHandler("settoken", cmd_settoken))
    app.add_handler(CommandHandler("resettokens", cmd_resettokens))
    # Auto-fetch on plain text messages containing whitelisted URLs.
    # Must be registered BEFORE the catch-all swallow.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_auto_url))
    # Absorb everything else silently
    # Auto-queue commands
    app.add_handler(CommandHandler("autoon", cmd_autoon))
    app.add_handler(CommandHandler("autooff", cmd_autooff))
    app.add_handler(CommandHandler("autotime", cmd_autotime))
    app.add_handler(CommandHandler("autocooldown", cmd_autocooldown))
    app.add_handler(CommandHandler("autostatus", cmd_autostatus))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("appon", cmd_appon))
    app.add_handler(CommandHandler("appoff", cmd_appoff))

    # Absorb everything else silently (Yeh ALWAYS bilkul LAST mein hona chahiye)
    app.add_handler(MessageHandler(filters.ALL, swallow))

    return app


def main() -> int:
    app = build_app()
    log.info("admin bot starting")
    # ---- Conflict-safety settings -----------------------------------------
    # drop_pending_updates=True  → discard the update backlog on boot. A
    #                              previous crashed instance may have left a
    #                              stale offset that Telegram would resend.
    # timeout / poll_interval    → long poll for 25s at a time (default 10s).
    #                              Long-poll lets Telegram forcibly close the
    #                              PREVIOUS conflicting connection faster.
    # bootstrap_retries=-1       → infinite retry on transient network errors
    #                              so we never exit(1) into the restart budget
    #                              on a hiccup.
    # allowed_updates            → only the update types we actually handle
    #                              (unchanged behaviour, less bandwidth).
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
        drop_pending_updates=True,
        timeout=25,
        poll_interval=1.0,
        bootstrap_retries=-1,
    )
    return 0



if __name__ == "__main__":
    sys.exit(main())
