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
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import time as _time_mod

import db
import feature_flags
import gallery_state as _gs
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


@only_admin
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """v11.4 — /broadcast. Reply to ANY message (text, photo, video, document,
    styled caption with spoilers, etc.) with /broadcast and that exact message
    is copied to every registered, non-banned mini-app user.

    Uses copyMessage so media attachments, caption formatting, and spoiler
    entities are preserved verbatim — the old mini-app textarea broadcast
    could only send plain text. Rate-limited to ~20 msg/s to stay under
    Telegram's global cap. Banned users are skipped.
    """
    msg = update.effective_message
    src = msg.reply_to_message if msg else None
    if not src:
        await msg.reply_text(
            "📣 Usage: reply to the message you want to broadcast with /broadcast.\n"
            "Works with text, photos, videos, documents, and styled/spoiler "
            "captions — whatever you reply to is forwarded verbatim to every user."
        )
        return

    # Build the recipient list from the miniapp users collection (same source
    # the retired mini-app broadcast used), skipping banned users.
    conn = db.connect()
    try:
        rows = list(conn.db["miniapp_users"].find(
            {}, {"_id": 1, "banned": 1}))
    finally:
        conn.close()
    recipients = []
    for r in rows:
        try:
            if r.get("banned"):
                continue
            uid = int(r.get("_id"))
            if uid > 0:
                recipients.append(uid)
        except Exception:
            continue

    if not recipients:
        await msg.reply_text("No registered mini-app users to broadcast to.")
        return

    status = await msg.reply_text(
        f"📣 Broadcasting to {len(recipients)} user(s)… (~20 msg/s)"
    )

    sent = failed = 0
    src_chat = src.chat_id
    src_msg_id = src.message_id
    for uid in recipients:
        try:
            await ctx.bot.copy_message(
                chat_id=uid,
                from_chat_id=src_chat,
                message_id=src_msg_id,
            )
            sent += 1
        except Exception:
            failed += 1
        # ~20 msg/s pacing; also yields the event loop so other handlers run.
        await asyncio.sleep(0.05)

    try:
        await status.edit_text(
            f"📣 Broadcast complete.\n✅ Sent: {sent}\n❌ Failed: {failed}"
        )
    except Exception:
        pass


def _glink(gid) -> str:
    """v11.4 — canonical gallery link for a numeric gallery id."""
    return f"https://nhentai.net/g/{gid}/"


@only_admin
async def cmd_topsave(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """v11.4 — /topsave. Lists the most-saved doujinshi across ALL users,
    ranked by how many distinct users bookmarked each gallery."""
    conn = db.connect()
    try:
        rows = list(conn.db["miniapp_bookmarks"].aggregate([
            {"$group": {
                "_id": "$gallery_id",
                "count": {"$sum": 1},
                "title": {"$first": "$title"},
            }},
            {"$sort": {"count": -1}},
            {"$limit": 15},
        ]))
    finally:
        conn.close()

    if not rows:
        await update.effective_message.reply_text("📚 No saved doujinshi yet.")
        return

    lines = ["🔥 Most saved doujinshi (all users)\n"]
    for i, r in enumerate(rows, 1):
        gid = r.get("_id")
        title = (r.get("title") or f"#{gid}")
        if len(title) > 60:
            title = title[:57] + "..."
        # escape the [ ] in titles so Markdown links don't break
        title = title.replace("[", "(").replace("]", ")")
        lines.append(
            f"{i}. [{title}]({_glink(gid)}) — saved by {r.get('count', 0)} user(s)"
        )
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# v11.7 — /weekly digest
# ---------------------------------------------------------------------------
_WEEKLY_DIGEST_TASK_KEY     = "_weekly_digest_task"
_WEEKLY_DIGEST_DEFAULT_TIME = "10:00"   # IST (UTC+5:30)
_WEEKLY_DIGEST_DEFAULT_DOW  = 6         # 0=Mon..6=Sun


def _weekly_digest_last_run(conn) -> str:
    return db.get_flag(conn, "weekly_digest_last_run", "")


def _weekly_digest_mark_ran(conn, when_iso: str) -> None:
    db.set_flag(conn, "weekly_digest_last_run", when_iso)


def _compose_weekly_digest(conn) -> Optional[str]:
    """Return a Markdown-formatted digest, or None if there were no saves this week."""
    week_ago = dt.datetime.utcnow() - dt.timedelta(days=7)
    rows = list(conn.db["miniapp_bookmarks"].aggregate([
        {"$match": {"created_at": {"$gte": week_ago}}},
        {"$group": {
            "_id":   "$gallery_id",
            "count": {"$sum": 1},
            "title": {"$first": "$title"},
        }},
        {"$sort":  {"count": -1}},
        {"$limit": 5},
    ]))
    if not rows:
        return None
    lines = ["🔥 *This week's top 5 saves*", ""]
    for i, r in enumerate(rows, 1):
        gid = r.get("_id")
        title = (r.get("title") or f"#{gid}")
        if len(title) > 60:
            title = title[:57] + "..."
        title = title.replace("[", "(").replace("]", ")")
        lines.append(f"{i}. [{title}]({_glink(gid)}) — saved by {r.get('count', 0)} user(s)")
    lines.append("")
    lines.append("👋 Enjoying the bot? Open the mini-app and browse more!")
    return "\n".join(lines)


async def _broadcast_weekly_digest(app, body: str) -> tuple[int, int]:
    """Send the digest to every non-banned mini-app user. Returns (ok, fail)."""
    conn = db.connect()
    try:
        users = list(conn.db["miniapp_users"].find(
            {"banned": {"$ne": True}}, {"_id": 1}))
    finally:
        conn.close()
    ok = fail = 0
    for u in users:
        uid = int(u.get("_id") or 0)
        if not uid:
            continue
        try:
            await app.bot.send_message(
                chat_id=uid, text=body,
                parse_mode="Markdown", disable_web_page_preview=True,
            )
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            log.debug("weekly digest to %s failed: %s", uid, e)
        await asyncio.sleep(0.06)   # ~16 msg/s
    return ok, fail


# ---------------------------------------------------------------------------
# v11.8 (#8) — /addimp <text>
# Records an admin-authored improvement note that surfaces in the mini-app
# Settings tab ("What's new" panel). Backed by `miniapp_improvements`.
# ---------------------------------------------------------------------------
@only_admin
async def cmd_addimp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    import uuid as _uuid
    text = " ".join(ctx.args or []).strip()
    if not text:
        await update.effective_message.reply_text(
            "Usage: /addimp <message>\n\nExample:\n"
            "  /addimp Added 9 new theme palettes to the mini-app.")
        return
    if len(text) > 2000:
        await update.effective_message.reply_text(
            "❌ Too long — keep it under 2000 characters.")
        return
    conn = db.connect()
    try:
        u = update.effective_user
        conn.db["miniapp_improvements"].insert_one({
            "_id":         str(_uuid.uuid4()),
            "text":        text,
            "author_id":   int(u.id if u else 0),
            "author_name": (u.first_name if u else "admin") or "admin",
            "ts":          dt.datetime.utcnow(),
        })
    finally:
        conn.close()
    await update.effective_message.reply_text(
        "✅ Improvement posted — users will see it in Settings → What's new.")


@only_admin
async def cmd_weekly(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually broadcast this week's top-5 saves right now."""
    conn = db.connect()
    try:
        body = _compose_weekly_digest(conn)
    finally:
        conn.close()
    if not body:
        await update.effective_message.reply_text(
            "📚 No saves this week — nothing to broadcast.")
        return
    await update.effective_message.reply_text("📤 Broadcasting weekly digest…")
    ok, fail = await _broadcast_weekly_digest(ctx.application, body)
    conn = db.connect()
    try:
        _weekly_digest_mark_ran(conn, dt.datetime.utcnow().isoformat())
    finally:
        conn.close()
    await update.effective_message.reply_text(
        f"✅ Weekly digest sent — {ok} delivered, {fail} failed.")


async def _weekly_digest_tick(app) -> None:
    """Called every 60s. Fires once/week on the configured DOW+time in IST."""
    try:
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    except Exception:  # noqa: BLE001
        return
    if ist_now.weekday() != _WEEKLY_DIGEST_DEFAULT_DOW:
        return
    if ist_now.strftime("%H:%M") != _WEEKLY_DIGEST_DEFAULT_TIME:
        return
    conn = db.connect()
    try:
        last = _weekly_digest_last_run(conn)
        if last:
            try:
                prev = dt.datetime.fromisoformat(last)
                if (dt.datetime.utcnow() - prev).total_seconds() < 6 * 24 * 3600:
                    return
            except (TypeError, ValueError):
                pass
        body = _compose_weekly_digest(conn)
    finally:
        conn.close()
    if not body:
        return
    log.info("weekly digest: firing automatic broadcast")
    ok, fail = await _broadcast_weekly_digest(app, body)
    conn = db.connect()
    try:
        _weekly_digest_mark_ran(conn, dt.datetime.utcnow().isoformat())
    finally:
        conn.close()
    log.info("weekly digest: automatic broadcast done — ok=%s fail=%s", ok, fail)


async def _weekly_digest_loop(app) -> None:
    await asyncio.sleep(30)
    log.info("weekly-digest loop started (60s poll)")
    while True:
        try:
            await _weekly_digest_tick(app)
        except Exception as e:  # noqa: BLE001
            log.exception("weekly-digest tick crashed (non-fatal): %s", e)
        await asyncio.sleep(60)


def _ensure_weekly_digest_running(app) -> None:
    existing = app.bot_data.get(_WEEKLY_DIGEST_TASK_KEY)
    if existing is not None and not existing.done():
        return
    task = asyncio.get_event_loop().create_task(_weekly_digest_loop(app))
    app.bot_data[_WEEKLY_DIGEST_TASK_KEY] = task
    log.info("weekly-digest background task spawned")


@only_admin
async def cmd_allsaved(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """v11.4 — /allsaved. Per-user save summary: username, total saves, and
    that user's 5 most recent saved doujinshi (title + link). Shows the top
    10 users by total saves to keep the message within Telegram limits."""
    conn = db.connect()
    try:
        # Top users by total bookmarks
        top_users = list(conn.db["miniapp_bookmarks"].aggregate([
            {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 10},
        ]))
        if not top_users:
            await update.effective_message.reply_text("📚 No saved doujinshi yet.")
            return

        # Resolve usernames in one query
        uids = [int(r["_id"]) for r in top_users if r.get("_id") is not None]
        udocs = list(conn.db["miniapp_users"].find(
            {"_id": {"$in": uids}}, {"_id": 1, "username": 1, "first_name": 1}))
        umap = {int(u["_id"]): u for u in udocs}

        blocks = ["📚 Saved doujinshi per user\n"]
        for r in top_users:
            uid = int(r["_id"])
            total = r.get("count", 0)
            u = umap.get(uid, {})
            who = ("@" + u["username"]) if u.get("username") \
                else (u.get("first_name") or f"user {uid}")

            recent = list(conn.db["miniapp_bookmarks"].find(
                {"user_id": uid}).sort("created_at", -1).limit(5))

            block = f"\n👤 {who} — {total} saved"
            for b in recent:
                t = (b.get("title") or f"#{b.get('gallery_id')}")
                if len(t) > 50:
                    t = t[:47] + "..."
                # escape the [ ] in titles so Markdown links don't break
                t = t.replace("[", "(").replace("]", ")")
                block += f"\n  • [{t}]({_glink(b.get('gallery_id'))})"
            blocks.append(block)
    finally:
        conn.close()

    # Split into <=3500-char chunks so long lists don't hit Telegram's 4096 cap
    chunk = blocks[0]
    for b in blocks[1:]:
        if len(chunk) + len(b) > 3500:
            await update.effective_message.reply_text(
                chunk, parse_mode="Markdown", disable_web_page_preview=True)
            chunk = b.lstrip("\n")
        else:
            chunk += b
    await update.effective_message.reply_text(
        chunk, parse_mode="Markdown", disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# v11.5 — Manual safety-net commands.
#
# Purpose: when the automatic pipeline fails to relay a gallery (e.g. the
# post is stuck "queued" in the mini-app and neither Force Re-scrape nor
# Force-Delete unblocks it), the admin can drop into a manual fallback:
#
#   /coverpost <url>              — the bot scrapes metadata, posts the
#                                   cover to the DB channel itself, and
#                                   replies with the message-id so the admin
#                                   can find it easily.
#   /verify <url> <cover_msg_id>  — bind an already-posted cover (posted
#                                   manually or by /coverpost) to the gallery's
#                                   MongoDB doc. Marks it COMPLETED with the
#                                   right db_cover_msg_id + open_link so the
#                                   mini-app forwards straight from the DB
#                                   channel next time anyone taps it.
# ---------------------------------------------------------------------------
def _build_open_link(channel_id: int, msg_id: int) -> str:
    """Same convention cover_poster.build_open_link uses:
    channel id -100xxxxxxxxxx -> t.me/c/xxxxxxxxxx/<msg_id>.
    """
    cid = str(int(channel_id))
    if cid.startswith("-100"):
        cid = cid[4:]
    elif cid.startswith("-"):
        cid = cid[1:]
    return f"https://t.me/c/{cid}/{int(msg_id)}"


@only_admin
async def cmd_coverpost(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually post a cover for a nhentai gallery to the DB channel.

    Usage: /coverpost <url|gallery_id>

    The bot scrapes metadata via hf_scraper, builds the standard cover
    caption, and posts the cover photo to `database_channel_id` using
    PTB's send_photo (which uploads by URL). Replies to the admin with
    the resulting message-id + open-link so they know exactly what to
    /verify next.
    """
    msg = update.effective_message
    args = ctx.args or []
    if not args:
        await msg.reply_text(
            "Usage: /coverpost <url_or_gallery_id>\n"
            "Example: /coverpost 393878\n"
            "Example: /coverpost https://nhentai.net/g/393878/"
        )
        return

    raw = args[0].strip()
    url = f"https://nhentai.net/g/{raw}/" if raw.isdigit() else raw

    await msg.reply_text(f"🔎 Scraping metadata for {url} …")

    try:
        meta = await hf_scraper.fetch_gallery_meta(url)
    except Exception as e:  # noqa: BLE001
        await msg.reply_text(f"❌ hf_scraper raised: {e!s}")
        return

    if meta is None or not (getattr(meta, "title", None)
                            or getattr(meta, "gallery_id", None)):
        await msg.reply_text(
            "❌ hf_scraper returned no metadata. The gallery may have been "
            "removed, or the source is currently rate-limiting us."
        )
        return

    # Build the caption using cover_poster's helper so it looks identical to
    # the automatic pipeline (grouped tags, gallery-id line, no URL).
    try:
        import cover_poster as _cp
        caption = _cp._format_caption(
            title=str(getattr(meta, "title", "") or ""),
            tags=getattr(meta, "tags", []) or [],
            pages=getattr(meta, "pages", None),
            url=url,
            requester_handle=None,
            gallery_id=str(getattr(meta, "gallery_id", "") or ""),
        )
    except Exception as e:  # noqa: BLE001
        # Fallback caption if cover_poster's helper isn't importable here.
        log.warning("cmd_coverpost: _format_caption failed: %s", e)
        caption = (f"**{getattr(meta, 'title', '(untitled)')}**\n\n"
                   f"➤ #{getattr(meta, 'gallery_id', '?')}")

    cover_url = getattr(meta, "cover_url", None)
    if not cover_url:
        await msg.reply_text(
            "❌ hf_scraper returned no cover_url for this gallery. "
            "You'll need to post the cover manually."
        )
        return

    channel_id = int(settings.database_channel_id)
    try:
        sent = await ctx.bot.send_photo(
            chat_id=channel_id,
            photo=cover_url,
            caption=caption,
            parse_mode="Markdown",
        )
    except Exception as e:  # noqa: BLE001
        await msg.reply_text(
            f"❌ Failed to send_photo to DB channel: {e!s}\n"
            "You can post the cover manually and then use /verify."
        )
        return

    open_link = _build_open_link(channel_id, sent.message_id)
    await msg.reply_text(
        "✅ Cover posted to DB channel.\n\n"
        f"📑 Message id: `{sent.message_id}`\n"
        f"🔗 Open link: {open_link}\n\n"
        f"Now post the PDF in the DB channel, then run:\n"
        f"`/verify {url} {sent.message_id}`\n\n"
        "That binds this cover to the gallery so the mini-app forwards "
        "from here next time anyone taps this post.",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


@only_admin
async def cmd_verify(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Bind an already-posted cover (posted manually or via /coverpost) to a
    gallery's MongoDB doc, marking it COMPLETED so the mini-app forwards
    from the DB channel next time anyone taps this post.

    Usage: /verify <url|gallery_id> <cover_msg_id> [pdf_msg_id]

    Also clears the processed_urls / queue tombstones so the doc is clean.
    """
    msg = update.effective_message
    args = ctx.args or []
    if len(args) < 2:
        await msg.reply_text(
            "Usage: /verify <url_or_gallery_id> <cover_msg_id> [pdf_msg_id]\n"
            "Example: /verify 393878 12345\n"
            "Example: /verify https://nhentai.net/g/393878/ 12345 12346"
        )
        return

    raw = args[0].strip()
    try:
        cover_msg_id = int(args[1])
    except (TypeError, ValueError):
        await msg.reply_text("cover_msg_id must be an integer.")
        return
    pdf_msg_id: Optional[int] = None
    if len(args) >= 3:
        try:
            pdf_msg_id = int(args[2])
        except (TypeError, ValueError):
            await msg.reply_text("pdf_msg_id (optional) must be an integer.")
            return

    url = f"https://nhentai.net/g/{raw}/" if raw.isdigit() else raw

    # Extract the gallery_id from the URL exactly like the pipeline does.
    try:
        gid = _gs.extract_gallery_id(url)
    except Exception as e:  # noqa: BLE001
        await msg.reply_text(f"Failed to extract gallery_id: {e!s}")
        return
    if not gid:
        await msg.reply_text("Could not extract a numeric gallery id from that URL.")
        return

    # Best-effort metadata scrape so the doc carries title/pages/tags.
    meta_title = None
    meta_pages = None
    try:
        meta = await hf_scraper.fetch_gallery_meta(url)
        if meta:
            meta_title = getattr(meta, "title", None)
            meta_pages = getattr(meta, "pages", None)
    except Exception as e:  # noqa: BLE001
        log.warning("/verify: metadata fetch failed for %s: %s", url, e)

    channel_id = int(settings.database_channel_id)
    open_link = _build_open_link(channel_id, cover_msg_id)
    now_ts = _time_mod.time()

    conn = db.connect()
    try:
        # 1) Upsert the galleries doc as COMPLETED with the manual msg ids.
        set_doc = {
            "status": "COMPLETED",
            "gallery_id": str(gid),
            "open_link": open_link,
            "db_cover_msg_id": int(cover_msg_id),
            "completed_at": now_ts,
            "updated_at": now_ts,
            "manual_verified": True,
            "manual_verified_by": int(update.effective_user.id),
        }
        if pdf_msg_id is not None:
            set_doc["db_pdf_msg_id"] = int(pdf_msg_id)
        if meta_title:
            set_doc["title"] = meta_title
        if meta_pages:
            set_doc["pages"] = int(meta_pages)

        conn.galleries.update_one(
            {"_id": str(gid)},
            {"$set": set_doc, "$setOnInsert": {"created_at": now_ts}},
            upsert=True,
        )

        # 2) Clear stale queue rows + processed_urls tombstone by url_hash.
        purged_queue = 0
        purged_processed = 0
        try:
            from url_utils import parse_batch as _pb
            parsed = _pb(url, max_links=1)
            if parsed.accepted:
                p = parsed.accepted[0]
                r = conn.queue.delete_many({"url_hash": p.url_hash})
                purged_queue = int(r.deleted_count)
                # We're rewriting the tombstone below — clear the old one first.
                r2 = conn.processed_urls.delete_many({"_id": p.url_hash})
                purged_processed = int(r2.deleted_count)
                # 3) Write a fresh processed_urls tombstone with completed_at
                #    so any second attempt at auto-relaying this URL will be
                #    correctly recognised as "already done".
                conn.processed_urls.update_one(
                    {"_id": p.url_hash},
                    {"$set": {
                        "_id":           p.url_hash,
                        "url":           p.normalised,
                        "first_seen_at": now_ts,
                        "completed_at":  now_ts,
                        "manual":        True,
                    }},
                    upsert=True,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("/verify: queue/processed_urls cleanup failed: %s", e)
    finally:
        try: conn.close()
        except Exception: pass

    await msg.reply_text(
        "✅ Gallery verified & bound.\n\n"
        f"• gallery_id  : `{gid}`\n"
        f"• status      : COMPLETED\n"
        f"• cover_msg_id: `{cover_msg_id}`"
        + (f"\n• pdf_msg_id  : `{pdf_msg_id}`" if pdf_msg_id is not None else "")
        + f"\n• open_link   : {open_link}\n"
        f"• title       : {meta_title or '(unavailable)'}\n"
        f"• queue rows purged     : {purged_queue}\n"
        f"• processed_urls purged : {purged_processed}\n\n"
        "The mini-app will now forward directly from the DB channel when "
        "anyone taps this post.",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


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


# --- Improvement #2: track request-to-join events so force_join can treat
# users with a PENDING join request as members (Bot API's getChatMember
# returns status="left" for those users until an admin approves them).
async def cb_chat_join_request(update: Update,
                               ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when a user taps an invite link on a request-to-join channel
    where this bot is an admin. Upserts a row into
    `miniapp_pending_join_requests` so force_join treats the user as a
    member while they wait for admin approval."""
    req = update.chat_join_request
    if not req or not req.from_user or not req.chat:
        return
    try:
        uid = int(req.from_user.id)
        cid = int(req.chat.id)
    except (TypeError, ValueError):
        return
    try:
        conn = db.connect()
        try:
            conn.db["miniapp_pending_join_requests"].update_one(
                {"_id": f"{uid}:{cid}"},
                {"$set": {
                    "_id":        f"{uid}:{cid}",
                    "user_id":    uid,
                    "chat_id":    cid,
                    "status":     "pending",
                    "created_at": _time_mod.time(),
                }},
                upsert=True,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        log.warning("chat_join_request upsert failed uid=%s cid=%s: %s",
                    uid, cid, e)


async def cb_chat_member_update(update: Update,
                                ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when a user's membership status in a chat changes (admin
    approved/declined their request, they left, etc.). We flip the
    pending row to `approved` on success so force_join keeps letting
    them through even if getChatMember is slow to propagate. Non-fatal
    on any failure — the pending row alone already unblocks the user."""
    upd = update.chat_member or update.my_chat_member
    if not upd or not upd.new_chat_member or not upd.chat:
        return
    member = upd.new_chat_member
    user = getattr(member, "user", None)
    if not user:
        return
    try:
        uid = int(user.id)
        cid = int(upd.chat.id)
    except (TypeError, ValueError):
        return
    status = str(getattr(member, "status", "") or "").lower()
    _MEMBER_OK = {"creator", "administrator", "member", "restricted"}
    try:
        conn = db.connect()
        try:
            col = conn.db["miniapp_pending_join_requests"]
            if status in _MEMBER_OK:
                col.update_one(
                    {"_id": f"{uid}:{cid}"},
                    {"$set": {"status":     "approved",
                              "user_id":    uid,
                              "chat_id":    cid,
                              "updated_at": _time_mod.time()}},
                    upsert=False,
                )
            elif status in ("left", "kicked", "banned"):
                # Admin declined or user left — drop the pending row so
                # force_join stops treating them as a member.
                col.delete_one({"_id": f"{uid}:{cid}"})
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        log.info("chat_member update handling failed uid=%s cid=%s: %s",
                 uid, cid, e)


# Silent handler for anything else — never confirm existence to strangers.
async def cb_force_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the '✅ I've joined — deliver my file' button.

    Callback data shape:  fj:check:<gallery_id>
    Sent by the force-join gate (mini-app's force_join service and
    relay_v2's auto-DM path) when a user tried to receive a gallery while
    not a member of the required channel(s).

    On tap:
      1. Re-check membership via Bot API getChatMember for every
         configured channel.
      2. If the user has joined all of them → deliver the remembered
         gallery via copyMessage (same pipeline as the mini-app's
         /api/queue/deliver), pop the pending row, and answer the
         callback with a toast.
      3. Otherwise answer with a 'still missing' toast and keep the
         pending row so they can try again after joining.
    """
    q = update.callback_query
    if not q or not q.from_user:
        return
    data = q.data or ""
    if not data.startswith("fj:"):
        return

    try:
        uid = int(q.from_user.id)
    except (TypeError, ValueError):
        try:
            await q.answer("Couldn't identify you — try again.")
        except Exception:  # noqa: BLE001
            pass
        return

    gallery_id = ""
    parts = data.split(":", 2)
    if len(parts) == 3:
        gallery_id = (parts[2] or "").strip()

    conn = db.connect()
    try:
        try:
            missing = await feature_flags.check_membership(conn, uid)
        except Exception as e:  # noqa: BLE001
            log.warning("fj:check membership check failed for uid=%s: %s",
                        uid, e)
            missing = []

        if missing:
            # Still not joined. Re-send the prompt buttons so the user can
            # tap again after joining.
            try:
                await q.answer("❌ You haven't joined yet — please join first.",
                               show_alert=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                await feature_flags.send_join_prompt(uid, missing,
                                                     gallery_id=gallery_id)
            except Exception as e:  # noqa: BLE001
                log.warning("fj:check re-prompt failed for uid=%s: %s", uid, e)
            return

        # Membership confirmed. Answer the callback first so the button
        # stops spinning immediately.
        try:
            await q.answer("✅ Verified — delivering your file…")
        except Exception:  # noqa: BLE001
            pass

        # Decide what to deliver: either the gallery the prompt was for,
        # or anything else remembered as pending for this user.
        to_deliver: list[str] = []
        if gallery_id:
            to_deliver.append(gallery_id)
        else:
            try:
                rows = conn.db["miniapp_pending_deliveries"].find(
                    {"user_id": uid}).limit(10)
                for r in rows:
                    gid = r.get("gallery_id")
                    if gid:
                        to_deliver.append(str(gid))
            except Exception:
                pass

        if not to_deliver:
            # Nothing pending — nothing to do.
            return

        # ---- Deliver each pending gallery via Bot API copyMessage --------
        from_chat = int(getattr(settings, "database_channel_id", 0) or 0)
        token = settings.admin_bot_token
        if not from_chat or not token:
            log.warning("fj:check: DATABASE_CHANNEL_ID / BOT_TOKEN missing")
            return

        import httpx as _hx

        async def _copy_one(from_id: int, to_id: int, mid: int) -> dict:
            url = f"https://api.telegram.org/bot{token}/copyMessage"
            payload = {
                "chat_id":      int(to_id),
                "from_chat_id": int(from_id),
                "message_id":   int(mid),
            }
            if feature_flags.share_disabled(conn):
                payload["protect_content"] = True
            async with _hx.AsyncClient(timeout=15) as c:
                r = await c.post(url, json=payload)
            try:
                return r.json() or {}
            except Exception:
                return {"ok": False,
                        "description": f"HTTP {r.status_code}"}

        for gid in to_deliver:
            try:
                doc = _gs.get(conn, gid) or {}
            except Exception:
                doc = {}
            cover_mid = doc.get("db_cover_msg_id")
            pdf_mid   = doc.get("db_pdf_msg_id")
            if not cover_mid and not pdf_mid:
                # Gallery not posted yet (or doc missing). Leave the
                # pending row in place — the next tap on Queue will
                # retry.
                log.info("fj:check: gallery %s has no stored msg IDs yet", gid)
                continue

            delivered_any = False
            sent_msg_ids: list[int] = []
            for mid in (cover_mid, pdf_mid):
                if not mid:
                    continue
                try:
                    r = await _copy_one(from_chat, uid, int(mid))
                    if r.get("ok"):
                        delivered_any = True
                        new_mid = int((r.get("result") or {}).get("message_id") or 0)
                        if new_mid:
                            sent_msg_ids.append(new_mid)
                    else:
                        log.warning("fj:check copy failed uid=%s gid=%s mid=%s: %s",
                                    uid, gid, mid, r.get("description"))
                except Exception as e:  # noqa: BLE001
                    log.warning("fj:check copy raised uid=%s gid=%s: %s",
                                uid, gid, e)

            if delivered_any:
                # Confirmation text in the same DM thread.
                try:
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    async with _hx.AsyncClient(timeout=10) as c:
                        conf = await c.post(url, json={
                            "chat_id": uid,
                            "text":    "📨 Sent to your DM",
                        })
                    conf_data = conf.json() or {}
                    if conf_data.get("ok"):
                        conf_mid = int((conf_data.get("result") or {}).get("message_id") or 0)
                        if conf_mid:
                            sent_msg_ids.append(conf_mid)
                except Exception as e:  # noqa: BLE001
                    log.info("fj:check confirmation sendMessage failed: %s", e)

                # Feature 1 (auto-delete): schedule deletion if enabled.
                try:
                    feature_flags.schedule_deletes(conn, uid, sent_msg_ids)
                except Exception as e:  # noqa: BLE001
                    log.warning("fj:check deletion scheduling failed: %s", e)

                # Clear the pending row now that delivery succeeded.
                try:
                    feature_flags.pop_pending(conn, uid, gid)
                except Exception:
                    pass
                log.info("fj:check: delivered gid=%s to uid=%s", gid, uid)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


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


# ---------------------------------------------------------------------------
# /users — v12.1 (D): admin equivalent of the mini-app's Admin → Users pane.
# Lists every known user with their username, id, today's usage vs cap,
# ban state, and last-seen timestamp. Sorted by used_today DESC so the
# heaviest users float to the top (same as mini-app default).
# ---------------------------------------------------------------------------
@only_admin
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List users with today's usage, cap, ban state, last-seen.

    Mirrors GET /api/admin/users (miniapp/backend/app/routes/admin.py).
    Output is chunked to Telegram's 4096-char message ceiling; when the
    list is longer, additional messages are sent so nothing is truncated.
    Usage:
      /users            first 200 rows, sorted by used_today desc
      /users banned     show banned users only
      /users active     show users who have used >=1 token today
    """
    msg = update.effective_message
    if msg is None:
        return
    filter_mode = ""
    if ctx.args:
        filter_mode = str(ctx.args[0]).strip().lower()

    conn = db.connect()
    try:
        rows = db.list_all_user_tokens(conn)
        cap = db.get_freepost(conn)
        # Enrich with ban state where the model tracks it. list_all_user_tokens
        # is the same source the mini-app Admin → Users pane reads today.
        try:
            admins = {int(a["user_id"]): a for a in db.list_admins(conn)}
        except Exception:
            admins = {}
    finally:
        conn.close()

    if filter_mode == "active":
        rows = [r for r in rows if int(r.get("used") or 0) > 0]
    elif filter_mode == "banned":
        # Ban state lives per-user in the mini-app admin API; the bot's own
        # db.list_all_user_tokens does not carry it, so filter is best-effort:
        # empty list rather than a lie.
        rows = []

    if not rows:
        await msg.reply_text(
            "No users to show" + (f" (filter={filter_mode})" if filter_mode else ".")
        )
        return

    header_parts = [
        f"👥 Users — cap = {cap}/day, resets 00:00 UTC",
        f"Showing {len(rows)} user(s)"
              + (f" (filter={filter_mode})" if filter_mode else "")
              + ", sorted by used_today desc",
        "",
    ]
    lines: list[str] = list(header_parts)
    total_used = 0
    for r in rows:
        uid = int(r["user_id"])
        uname = f"@{r['username']}" if r.get("username") else "(no username)"
        used = int(r.get("used") or 0)
        left = int(r.get("remaining") or 0)
        role = ""
        if uid in admins:
            role = " 👑 super" if int(admins[uid].get("is_super") or 0) == 1 else " 🛡 admin"
        lines.append(f"  {uname}  [id={uid}]  {used}/{cap} used, {left} left{role}")
        total_used += used
    lines.append("")
    lines.append(f"Totals: {len(rows)} users — {total_used} posts today.")

    # Telegram's message ceiling is 4096 chars; chunk on line boundaries.
    buf: list[str] = []
    size = 0
    for line in lines:
        add = len(line) + 1
        if size + add > 3800 and buf:
            await msg.reply_text("\n".join(buf))
            buf = []
            size = 0
        buf.append(line)
        size += add
    if buf:
        await msg.reply_text("\n".join(buf))


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
    """V2: /search has moved to the Mini App.

    The old text-based picker (search_picker.start_search) is no longer
    the primary search UX. We reply with a friendly redirect + a Web App
    button so users can hit the same functionality in a much nicer UI.

    The `search_picker` module and its callback handler remain wired up
    so anyone deep-linking directly into a legacy picker session still
    works — we only replaced the ENTRY POINT.
    """
    msg = update.effective_message
    if not msg:
        return

    # Pull the (optional) query so we can pre-fill it in the Mini App.
    query = ""
    if msg.text:
        parts = msg.text.split(None, 1)
        if len(parts) > 1:
            query = parts[1].strip()

    # If MINIAPP_URL isn't configured yet, fall back to the legacy picker
    # so /search never simply breaks on older deploys.
    if not MINIAPP_URL or MINIAPP_URL == "/":
        if not query:
            await msg.reply_text("Usage: /search <keyword>")
            return
        await ctx.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
        await search_picker.start_search(update, ctx, query)
        return

    # Build the Mini App URL, deep-linking to the query when the user gave one.
    from urllib.parse import quote
    target_url = MINIAPP_URL
    if query:
        target_url = f"{MINIAPP_URL}#/search?q={quote(query)}"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔎 Open in Mini App",
            web_app=WebAppInfo(url=target_url),
        )
    ]])

    body = (
        "Search has moved to the Mini App ✨\n\n"
        "You'll get a proper grid with covers, filters, tag chips, "
        "bookmarks, and one-tap queueing — everything /search used to "
        "do, only much faster.\n\n"
        "Tap the button below to open it."
    )
    if query:
        body += f"\n\nYour query “{query}” will be pre-filled."

    await msg.reply_text(body, reply_markup=kb)


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
    """v11.6 — SLIM /help.

    Per user's rules for v11.6, the everyday listing is now short.
    The following commands have been REMOVED from /help but remain
    registered internally (back-compat, unchanged behaviour):
      /search /token /queue /status /fetch
      /pause /resume /last /health
      /alltoken
      /autoon /autooff /autotime /autocooldown /autostatus

    For a full screenshot-style command reference (bullet + example
    per command), see /description.
    """
    uid = int(update.effective_user.id)
    is_super = _is_super(uid)
    is_admin = _is_admin(uid)
    is_public = _is_public()

    lines: list[str] = ["Available commands:", ""]

    # ---- Everyday --------------------------------------------------------
    lines.append("🔹 Everyday:")
    lines.append("  /help          compact command list")
    lines.append("  /description   full screenshot-style command reference")
    lines.append("")

    # ---- Admin -----------------------------------------------------------
    if is_admin:
        lines.append("🔹 Admin:")
        lines.append("  /diag                              scraper diagnostics")
        lines.append("  /coverpost <url>                   post a cover to the DB channel")
        lines.append("  /verify <url> <cover_id> [pdf_id]  bind a cover to a gallery")
        lines.append("  /broadcast                         reply to a message to broadcast it")
        lines.append("  /topsave                           most-saved doujinshi across all users")
        lines.append("  /allsaved                          per-user save summary")
        lines.append("  /users [active|banned]             list users with today's usage")
        lines.append("  /popupmsg <text> [+photo]          set the mini-app popup message/image")
        lines.append("  /popuptime <hours>                 popup cooldown per user (default 2)")
        lines.append("  /popupon  /popupoff  /popupstatus  toggle + inspect the popup")
        lines.append("  /prefetch [status|now]             v12.4 cache warmer status / manual sweep")
        lines.append("  /app                               open the mini-app")
        lines.append("  /appon                             show mini-app to everyone")
        lines.append("  /appoff                            hide mini-app from non-admins")
        lines.append("")

    # ---- Super-admin -----------------------------------------------------
    if is_super:
        lines.append("🔹 Super-admin only:")
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


# ---------------------------------------------------------------------------
# /description — v11.6 screenshot-style FULL command reference.
#
# Matches the layout the user pointed at (Doujinshi File bot screenshot):
# each command appears as a bullet with a short description and an example.
# Uses Markdown so the example lines render as inline code. Web-page preview
# is disabled so the trailing t.me invite doesn't blow up into a big card.
# ---------------------------------------------------------------------------
@only_public
async def cmd_description(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Full command reference, category-wise, bullet+example per command."""
    from telegram.constants import ParseMode  # local import, avoid top-level churn

    uid = int(update.effective_user.id)
    is_super = _is_super(uid)
    is_admin = _is_admin(uid)

    L: list[str] = []
    L.append("📖 *Full command reference*")
    L.append("")

    # ---- General (everyone) ---------------------------------------------
    L.append("👤 *General*")
    L.append("• /help — compact command list")
    L.append("    _example:_ `/help`")
    L.append("• /description — this full reference")
    L.append("    _example:_ `/description`")
    L.append("")

    if is_admin:
        # ---- Admin: diagnostics ------------------------------------------
        L.append("🩺 *Admin — diagnostics*")
        L.append("• /diag — scraper diagnostics (source, endpoint, live search)")
        L.append("    _example:_ `/diag`")
        L.append("")

        # ---- Admin: manual relay safety-net ------------------------------
        L.append("🛠 *Admin — manual relay safety-net*")
        L.append("• /coverpost `<url_or_id>` — post a cover to the DB channel; "
                 "replies with the message id and open link")
        L.append("    _example:_ `/coverpost https://nhentai.net/g/393878/`")
        L.append("• /verify `<url_or_id> <cover_msg_id> [pdf_msg_id]` — bind an "
                 "already-posted cover to a gallery so the mini-app forwards "
                 "from the DB channel")
        L.append("    _example:_ `/verify 393878 12345 12346`")
        L.append("")

        # ---- Admin: broadcast --------------------------------------------
        L.append("📣 *Admin — broadcast*")
        L.append("• /broadcast — reply to any message (text / photo / video / "
                 "document / styled caption with spoilers) to copy it to every "
                 "non-banned mini-app user (~20 msg/s)")
        L.append("    _example:_ reply `/broadcast` to the message you want sent")
        L.append("")

        # ---- Admin: saves stats ------------------------------------------
        L.append("⭐ *Admin — saves stats*")
        L.append("• /topsave — most-saved doujinshi across all users")
        L.append("    _example:_ `/topsave`")
        L.append("• /allsaved — per-user save summary (top 10 users + each "
                 "user's 5 most recent saved links)")
        L.append("    _example:_ `/allsaved`")
        L.append("")

        # ---- Admin: mini-app control -------------------------------------
        L.append("📱 *Admin — mini-app control*")
        L.append("• /app — open the mini-app (WebApp button)")
        L.append("    _example:_ `/app`")
        L.append("• /appon — make the mini-app publicly visible")
        L.append("    _example:_ `/appon`")
        L.append("• /appoff — hide the mini-app from non-admins")
        L.append("    _example:_ `/appoff`")
        L.append("")

    if is_super:
        L.append("👑 *Super-admin only*")
        L.append("• /onpublic — open bot to any user (public mode ON)")
        L.append("    _example:_ `/onpublic`")
        L.append("• /offpublic — close bot to admins only (public mode OFF)")
        L.append("    _example:_ `/offpublic`")
        L.append("• /freepost `<n>` — set daily token cap for regular users")
        L.append("    _example:_ `/freepost 10`")
        L.append("• /settoken `@user <n>` — set a user's remaining tokens for today")
        L.append("    _example:_ `/settoken @alice 5`")
        L.append("• /resettokens — reset everyone's used_today to 0 now")
        L.append("    _example:_ `/resettokens`")
        L.append("• /addadmin `<id>` — grant regular admin")
        L.append("    _example:_ `/addadmin 123456789`")
        L.append("• /removeadmin `<id>` — revoke admin (any tier)")
        L.append("    _example:_ `/removeadmin 123456789`")
        L.append("• /addsuperadmin `<id>` — grant super-admin")
        L.append("    _example:_ `/addsuperadmin 123456789`")
        L.append("• /removesuperadmin `<id>` — demote super-admin to regular")
        L.append("    _example:_ `/removesuperadmin 123456789`")
        L.append("• /listadmins — show all admins and tiers")
        L.append("    _example:_ `/listadmins`")
        L.append("")

    # ---- Hidden-but-registered (back-compat; still work if typed) --------
    if is_admin:
        L.append("💤 *Still registered (hidden from /help)*")
        L.append("_These work but are intentionally not listed in /help:_")
        L.append("`/search  /token  /queue  /status  /fetch  /pause  /resume  "
                 "/last  /health  /alltoken  /autoon  /autooff  /autotime  "
                 "/autocooldown  /autostatus`")
        L.append("")

    L.append("📢 All doujinshi posted here → https://t.me/+uyNxVAVPdUBlOWU9")

    await update.effective_message.reply_text(
        "\n".join(L),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


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

# ---------------------------------------------------------------------------
# v12.3 — Mini-app popup commands.
#
#   /popupmsg <text>     set the popup body copy (pass "clear" to wipe)
#                        attach a photo to the SAME message to also set image
#   /popuptime <hours>   min hours between shows per user (default 2, 0 = every open)
#   /popupon             enable the popup
#   /popupoff            disable the popup
#   /popupstatus         show current config
#
# All state lives in control_flags; the mini-app polls GET /api/popup on open.
# ---------------------------------------------------------------------------

@only_admin
async def cmd_popupmsg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the popup message (and optional image when a photo is attached)."""
    msg = update.effective_message
    if msg is None:
        return
    # Photo attached to the SAME message carrying /popupmsg → store its file_id.
    photo_file_id = ""
    if msg.photo:
        photo_file_id = msg.photo[-1].file_id  # largest size
    text = ""
    if ctx.args:
        text = " ".join(ctx.args).strip()
    elif msg.caption:
        # /popupmsg sent as a photo caption — ctx.args is empty, use caption body.
        parts = msg.caption.split(maxsplit=1)
        if len(parts) > 1:
            text = parts[1].strip()
    conn = db.connect()
    try:
        if text.lower() == "clear":
            db.set_flag(conn, "popup_message", "")
            db.set_flag(conn, "popup_image_file_id", "")
            await msg.reply_text("🧹 Popup message and image cleared.")
            return
        if text:
            db.set_flag(conn, "popup_message", text)
        if photo_file_id:
            db.set_flag(conn, "popup_image_file_id", photo_file_id)
    finally:
        conn.close()
    if not text and not photo_file_id:
        await msg.reply_text(
            "Usage: /popupmsg <text>\n"
            "Attach a photo to the same message to also set the popup image.\n"
            "Use /popupmsg clear to wipe both."
        )
        return
    bits = []
    if text:
        bits.append(f"📝 message set ({len(text)} chars)")
    if photo_file_id:
        bits.append("🖼 image set")
    conn = db.connect()
    try:
        enabled = db.get_flag(conn, "popup_enabled", "0") == "1"
    finally:
        conn.close()
    await msg.reply_text(
        "✅ Popup updated — " + ", ".join(bits)
        + ("" if enabled else "\n(Popup is currently OFF — /popupon to enable.)")
    )


@only_admin
async def cmd_popuptime(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the per-user cooldown between popup shows, in hours."""
    msg = update.effective_message
    if msg is None:
        return
    if not ctx.args:
        conn = db.connect()
        try:
            cur = db.get_flag(conn, "popup_freq_hours", "2")
        finally:
            conn.close()
        await msg.reply_text(
            f"Usage: /popuptime <hours>\n"
            f"Current: every {cur} hour(s) per user (0 = show every open)."
        )
        return
    try:
        hours = int(str(ctx.args[0]).strip())
        if hours < 0:
            raise ValueError
    except ValueError:
        await msg.reply_text("Hours must be a whole number ≥ 0 (0 = show on every open).")
        return
    conn = db.connect()
    try:
        db.set_flag(conn, "popup_freq_hours", str(hours))
    finally:
        conn.close()
    label = "on every mini-app open" if hours == 0 else f"every {hours} hour(s) per user"
    await msg.reply_text(f"⏱ Popup frequency: {label}.")


@only_admin
async def cmd_popupon(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        db.set_flag(conn, "popup_enabled", "1")
        has_content = bool(
            db.get_flag(conn, "popup_message", "").strip()
            or db.get_flag(conn, "popup_image_file_id", "").strip()
        )
    finally:
        conn.close()
    note = "" if has_content else "\n⚠ No message/image set yet — use /popupmsg first."
    await update.effective_message.reply_text("🔔 Popup ON." + note)


@only_admin
async def cmd_popupoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db.connect()
    try:
        db.set_flag(conn, "popup_enabled", "0")
    finally:
        conn.close()
    await update.effective_message.reply_text("🔕 Popup OFF.")


@only_admin
async def cmd_popupstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick sanity-check of the current popup config."""
    conn = db.connect()
    try:
        enabled = db.get_flag(conn, "popup_enabled", "0") == "1"
        message = db.get_flag(conn, "popup_message", "")
        image = db.get_flag(conn, "popup_image_file_id", "")
        freq = db.get_flag(conn, "popup_freq_hours", "2")
    finally:
        conn.close()
    preview = (message[:80] + "…") if len(message) > 80 else message
    lines = [
        f"🔔 Popup status: {'ON' if enabled else 'OFF'}",
        f"  frequency: every {freq} hour(s) per user",
        f"  message:   {preview if preview else '(none)'}",
        f"  image:     {'set' if image else '(none)'}",
    ]
    await update.effective_message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# v12.4 — /prefetch admin command.
#
# Subcommands (72 % ships "status" only; 78 % adds "now"):
#   /prefetch                → alias for /prefetch status
#   /prefetch status         → last_run_summary() + turso_client.health()
#   /prefetch now            → fire one sweep immediately (78 %)
#
# The mini-app tree may not be importable in every deployment (e.g. a bot-only
# Render service without libsql-client). Both imports are guarded and the
# handler degrades gracefully rather than 500ing on the admin.
# ---------------------------------------------------------------------------
try:
    from miniapp.backend.app.services import prefetch_cron as _prefetch_cron_admin  # noqa: WPS433
except Exception as _e_pref:  # noqa: BLE001
    _prefetch_cron_admin = None
    _prefetch_cron_admin_err = _e_pref
else:
    _prefetch_cron_admin_err = None

try:
    from miniapp.backend.app.services import turso_client as _turso_admin  # noqa: WPS433
except Exception as _e_turso:  # noqa: BLE001
    _turso_admin = None
    _turso_admin_err = _e_turso
else:
    _turso_admin_err = None


def _fmt_epoch(ts) -> str:
    if not ts:
        return "never"
    try:
        import datetime as _dt
        return _dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:  # noqa: BLE001
        return str(ts)


def _fmt_seconds(sec) -> str:
    if sec is None:
        return "—"
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return str(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h}h {m}m"


def _render_prefetch_status() -> str:
    if _prefetch_cron_admin is None:
        return (
            "🔄 Prefetch status\n"
            f"  module:  import FAILED ({_prefetch_cron_admin_err})\n"
            "  ↳ the worker booted without the mini-app tree — sweep disabled."
        )

    try:
        snap = _prefetch_cron_admin.last_run_summary()
    except Exception as e:  # noqa: BLE001
        return f"🔄 Prefetch status\n  ⚠ last_run_summary() raised: {e}"

    if _turso_admin is not None:
        try:
            turso = _turso_admin.health()
        except Exception as e:  # noqa: BLE001
            turso = {"available": False, "reason": f"health raised: {e}"[:120]}
    else:
        turso = {"available": False, "reason": f"import failed: {_turso_admin_err}"}

    now = snap.get("now") or 0
    started = snap.get("started_at")
    finished = snap.get("finished_at")
    since_finish = (now - finished) if (now and finished) else None

    lines = [
        "🔄 Prefetch status",
        f"  enabled:      {'ON' if snap.get('enabled') else 'OFF'}",
        f"  interval:     every {_fmt_seconds(snap.get('interval_sec'))}",
        f"  max pages:    {snap.get('max_pages')} per sort",
        f"  delay:        {snap.get('delay_sec')}s between pages",
        f"  sorts:        {', '.join(snap.get('sorts') or [])}",
        "",
        f"  sweeps done:  {snap.get('sweep_count')}",
        f"  last start:   {_fmt_epoch(started)}",
        f"  last finish:  {_fmt_epoch(finished)}"
        + (f"  ({_fmt_seconds(since_finish)} ago)" if since_finish is not None else ""),
        f"  last dur:     {_fmt_seconds(snap.get('duration_sec'))}",
        f"  pages ok:     {snap.get('pages_ok')} / {snap.get('pages_planned')}",
        f"  skipped:      {snap.get('pages_skipped')}  (bucket / 429)",
        f"  failed:       {snap.get('pages_failed')}",
        f"  last error:   {snap.get('last_error') or '—'}",
        "",
        f"  turso:        {'✅ up' if turso.get('available') else '❌ down'}"
        + (f"  ({turso.get('latency_ms')}ms)" if turso.get('latency_ms') is not None else "")
        + (f"  reason: {turso.get('reason')}" if turso.get('reason') else ""),
    ]
    return "\n".join(lines)


@only_admin
async def cmd_prefetch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/prefetch [status|now]  — v12.4 cache-warmer inspector."""
    args = (ctx.args or [])
    sub = (args[0].strip().lower() if args else "status")

    if sub in ("", "status"):
        await update.effective_message.reply_text(_render_prefetch_status())
        return

    # "now" subcommand — fire one sweep immediately, then reply with fresh status.
    if sub == "now":
        if _prefetch_cron_admin is None:
            await update.effective_message.reply_text(
                "⚠ Prefetch module is not importable in this deployment — nothing to run.\n"
                f"reason: {_prefetch_cron_admin_err}"
            )
            return
        try:
            enabled = bool(_prefetch_cron_admin._enabled())
        except Exception:  # noqa: BLE001
            enabled = False
        if not enabled:
            await update.effective_message.reply_text(
                "⚠ Prefetch is disabled (PREFETCH_ENABLED=0 or empty _SORTS). "
                "Set PREFETCH_ENABLED=1 in Render and restart the worker to re-enable."
            )
            return
        await update.effective_message.reply_text("⏳ Kicking off a manual sweep…")
        try:
            await _prefetch_cron_admin.trigger_now()
        except Exception as e:  # noqa: BLE001
            await update.effective_message.reply_text(f"❌ trigger_now() raised: {e}")
            return
        await update.effective_message.reply_text(_render_prefetch_status())
        return

    await update.effective_message.reply_text(
        "Usage: /prefetch [status|now]"
    )


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
    _ensure_weekly_digest_running(app)   # v11.7: weekly digest scheduler

    # Regular-admin commands
    app.add_handler(CommandHandler("fetch", cmd_fetch))
    app.add_handler(CommandHandler("search", cmd_search))
    # Interactive picker callback (buttons on /search results)
    app.add_handler(CallbackQueryHandler(cb_search_picker, pattern=r"^sp\|"))
    # Force-join '✅ I've joined' callback (feature 3)
    app.add_handler(CallbackQueryHandler(cb_force_join, pattern=r"^fj:"))
    # Improvement #2: track request-to-join taps + approval events so
    # force_join can treat pending-request users as members. MUST be
    # registered BEFORE the catch-all MessageHandler(filters.ALL, swallow).
    app.add_handler(ChatJoinRequestHandler(cb_chat_join_request))
    app.add_handler(ChatMemberHandler(cb_chat_member_update,
                                      ChatMemberHandler.ANY_CHAT_MEMBER))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("users", cmd_users))     # v12.1 (D)
    app.add_handler(CommandHandler("topsave", cmd_topsave))
    app.add_handler(CommandHandler("weekly", cmd_weekly))   # v11.7
    app.add_handler(CommandHandler("addimp", cmd_addimp))   # v11.8 (#8)
    app.add_handler(CommandHandler("allsaved", cmd_allsaved))
    app.add_handler(CommandHandler("coverpost", cmd_coverpost))
    app.add_handler(CommandHandler("verify", cmd_verify))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("help", cmd_help))
    # v11.6: full screenshot-style command reference.
    app.add_handler(CommandHandler("description", cmd_description))
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
    # v12.3 — mini-app popup admin commands
    app.add_handler(CommandHandler("popupmsg", cmd_popupmsg))
    app.add_handler(CommandHandler("popuptime", cmd_popuptime))
    app.add_handler(CommandHandler("popupon", cmd_popupon))
    app.add_handler(CommandHandler("popupoff", cmd_popupoff))
    app.add_handler(CommandHandler("popupstatus", cmd_popupstatus))
    app.add_handler(CommandHandler("prefetch", cmd_prefetch))   # v12.4

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
