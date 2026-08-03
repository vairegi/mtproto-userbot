"""
admin_bot_patch.py — Ready-to-paste snippets for admin_bot.py

This file is NOT imported by the Mini App. It exists purely as a reference:
copy the sections you need into your existing admin_bot.py and delete this
file (or keep it as a template — the Mini App backend ignores it either way).

Adds three admin-facing conveniences to your bot:
  1. /app command → gives the user a Web App button that opens the Mini App
  2. /appon and /appoff → flip the Mini App's public/private flag from Telegram
  3. Startup log line so you can eyeball that the Mini App URL is configured

Prerequisites:
  * Set MINIAPP_URL in your Render env vars, e.g.
        MINIAPP_URL=https://your-render-service.onrender.com/
  * python-telegram-bot >= 20 (which you already use for the existing bot)
"""
from __future__ import annotations

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

log = logging.getLogger("admin_bot")
MINIAPP_URL = os.environ.get("MINIAPP_URL", "").rstrip("/") + "/"


# ============================================================
# 1) /app  — Web App launcher button
# ============================================================
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


# ============================================================
# 2) /appon and /appoff — flip Mini App visibility from Telegram
# ============================================================
# These call the Mini App's own MongoDB flag directly, using the same db.py
# the bot already has open. No HTTP round-trip needed.
def _only_admin(handler):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        import config  # your existing config module
        if update.effective_user.id != config.settings.admin_user_id:
            return
        await handler(update, ctx)
    return wrapper


@_only_admin
async def cmd_appon(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip Mini App to PUBLIC mode."""
    _set_miniapp_visibility(True)
    await update.effective_message.reply_text(
        "✅ Mini App is now PUBLIC (all users can browse & queue).")


@_only_admin
async def cmd_appoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip Mini App to PRIVATE (admin-only) mode."""
    _set_miniapp_visibility(False)
    await update.effective_message.reply_text(
        "🔒 Mini App is now PRIVATE (admin only).")


def _set_miniapp_visibility(public: bool) -> None:
    """
    Writes miniapp_settings.public_mode in the same Mongo the Mini App reads
    from. Using the bot's existing pymongo client keeps this a single-liner.
    """
    from pymongo import MongoClient
    uri = os.environ["MONGO_URI"]
    db_name = os.environ.get("MONGO_DB", "doujinshi")
    with MongoClient(uri, serverSelectionTimeoutMS=5000) as c:
        c[db_name]["miniapp_settings"].update_one(
            {"_id": "singleton"},
            {"$set": {"public_mode": bool(public)}},
            upsert=True,
        )


# ============================================================
# 3) Wire everything in build_app()
# ============================================================
def install(application: Application) -> None:
    """Call this from your existing build_app() right before application.run_polling()."""
    application.add_handler(CommandHandler("app",    cmd_app))
    application.add_handler(CommandHandler("appon",  cmd_appon))
    application.add_handler(CommandHandler("appoff", cmd_appoff))
    if MINIAPP_URL and MINIAPP_URL != "/":
        log.info("[admin_bot] Mini App launcher wired: %s", MINIAPP_URL)
    else:
        log.warning("[admin_bot] MINIAPP_URL is not set — /app will refuse to launch")
