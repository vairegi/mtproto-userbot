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
import os
import shutil
import sys
from typing import Awaitable, Callable, Optional

from telegram import Update
from telegram.constants import ChatAction
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


def build_app() -> Application:
    # Ensure DB exists (admin bot may boot before worker on first ever run)
    db.init_db()

    app = ApplicationBuilder().token(settings.admin_bot_token).post_init(_on_startup).build()
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
    app.add_handler(MessageHandler(filters.ALL, swallow))
    return app


def main() -> int:
    app = build_app()
    log.info("admin bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
