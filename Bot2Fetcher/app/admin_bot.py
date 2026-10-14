"""
admin_bot.py — v13.10 Bot 2 admin command listener.

A small python-telegram-bot app that runs INSIDE the Bot2Fetcher process,
answering worker-management commands from the admin (ADMIN_USER_ID):

    /addworker            wizard: api_id → api_hash → session (first time);
                          session only once creds are stored in Mongo
    /listworker           list numbered workers + current api_id/hash
    /removeworker <n...>  remove slot(s); "all" clears; renumbers down
    /changeapiandhash     wizard: new api_id → new api_hash (sessions kept)

State lives in Mongo via app/workers.py (single source; survives restarts).
The fetcher hot-reloads the worker list — no redeploy needed.

Runs on BOT2_TOKEN. If BOT2_TOKEN is unset the listener silently stays off
(the fetcher is unaffected). Admin gate: only ADMIN_USER_ID may use these
commands; everyone else is ignored.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, Optional

from . import workers as _workers

log = logging.getLogger("bot2fetcher.admin")

# Wizard steps
_IDLE = "idle"
_AW_API_ID = "addworker:api_id"
_AW_API_HASH = "addworker:api_hash"
_AW_SESSION = "addworker:session"
_CA_API_ID = "changeapi:api_id"
_CA_API_HASH = "changeapi:api_hash"

# Per-admin wizard state (in-memory only; expires after WIZARD_TTL_S).
# Worst case on restart: wizard resets, the admin re-runs /addworker.
# Persisted DATA (workers/creds) is always Mongo — never memory.
_WIZARD_TTL_S = 600


def _first(*names: str) -> str:
    for n in names:
        v = os.getenv(n)
        if v and v.strip():
            return v.strip()
    return ""


class WorkerAdminBot:
    """Owns the PTB app + the wizard state. start() is non-blocking."""

    def __init__(self, db, notify_reload=None, status_provider=None):
        self._db = db
        self._status_provider = status_provider
        self._notify_reload = notify_reload  # called after any worker change
        self._token = _first("BOT2_TOKEN")
        self._admin_id = 0
        try:
            self._admin_id = int(_first("ADMIN_USER_ID", "ADMIN_ID", "OWNER_ID") or 0)
        except ValueError:
            self._admin_id = 0
        self._wizard: Dict[int, Dict[str, Any]] = {}
        self._thread: Optional[threading.Thread] = None

    # -- helpers -----------------------------------------------------------
    def _is_admin(self, user_id: int) -> bool:
        return bool(self._admin_id) and int(user_id) == self._admin_id

    def _wiz(self, uid: int) -> Dict[str, Any]:
        w = self._wizard.get(uid)
        if w and time.time() - w.get("ts", 0) > _WIZARD_TTL_S:
            self._wizard.pop(uid, None)
            w = None
        return w or {"step": _IDLE, "ts": time.time()}

    def _set_wiz(self, uid: int, step: str, **kw):
        d = {"step": step, "ts": time.time()}
        d.update(kw)
        self._wizard[uid] = d

    def _reload(self):
        try:
            if self._notify_reload:
                self._notify_reload()
        except Exception as e:  # noqa: BLE001
            log.warning("worker reload notify failed: %s", e)

    # -- PTB wiring ---------------------------------------------------------
    def start(self) -> bool:
        """Start polling in a daemon thread. Returns True if started."""
        if not self._token:
            log.warning("BOT2_TOKEN not set — worker admin commands DISABLED "
                        "(fetcher unaffected)")
            return False
        if not self._admin_id:
            log.warning("ADMIN_USER_ID not set — worker admin commands DISABLED")
            return False
        try:
            from telegram.ext import (ApplicationBuilder, CommandHandler,
                                      MessageHandler, filters)
        except Exception as e:  # noqa: BLE001
            log.warning("python-telegram-bot unavailable (%s) — admin commands off", e)
            return False

        app = (ApplicationBuilder().token(self._token).build())
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("checkram", self._cmd_checkram))
        app.add_handler(CommandHandler("addworker", self._cmd_addworker))
        app.add_handler(CommandHandler("listworker", self._cmd_listworker))
        app.add_handler(CommandHandler("removeworker", self._cmd_removeworker))
        app.add_handler(CommandHandler("changeapiandhash", self._cmd_changeapi))
        app.add_handler(CommandHandler("addlog", self._cmd_addlog))
        app.add_handler(CommandHandler("removelog", self._cmd_removelog))
        app.add_handler(CommandHandler("logstatus", self._cmd_logstatus))
        app.add_handler(CommandHandler("pauseworker", self._cmd_pauseworker))
        app.add_handler(CommandHandler("resumeworker", self._cmd_resumeworker))
        app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                       self._on_text))

        def _run():
            import asyncio as _a
            loop = _a.new_event_loop()
            _a.set_event_loop(loop)
            try:
                app.run_polling(drop_pending_updates=True,
                                close_loop=False, stop_signals=None)
            except Exception as e:  # noqa: BLE001
                log.warning("admin bot polling ended: %s", e)

        self._thread = threading.Thread(target=_run, daemon=True,
                                        name="bot2-admin-bot")
        self._thread.start()
        log.info("🛠 worker admin bot started (BOT2_TOKEN) — /addworker "
                 "/listworker /removeworker /changeapiandhash")
        return True

    # -- command handlers (async, PTB) -------------------------------------
    async def _cmd_start(self, update, ctx):
        if self._is_admin(int(update.effective_user.id)):
            await update.effective_message.reply_text(
                "🛠 Bot 2 admin\n"
                "Workers: /addworker · /listworker · /removeworker <n…|all> · "
                "/changeapiandhash\n"
                "Log channel: /addlog <channel id> · /removelog · /logstatus\n"
                "Pause: /pauseworker <n…|all> · /resumeworker <n…|all>\n"
                "Misc: /checkram")

    async def _cmd_checkram(self, update, ctx):
        if self._is_admin(int(update.effective_user.id)):
            from .dashboard import _ram_line
            await update.effective_message.reply_text(_ram_line())

    async def _cmd_addworker(self, update, ctx):
        msg = update.effective_message
        uid = int(update.effective_user.id)
        if not self._is_admin(uid):
            return
        creds = _workers.get_creds(self._db)
        if creds:
            self._set_wiz(uid, _AW_SESSION)
            await msg.reply_text(
                "➕ Add worker — saved API creds found "
                f"(api_id={creds[0]}).\n\nSend the STRING SESSION for the new "
                "userbot.\nSend /cancel to abort.")
        else:
            self._set_wiz(uid, _AW_API_ID)
            await msg.reply_text(
                "➕ Add worker — no API creds saved yet (first worker).\n\n"
                "Step 1/3: send the API ID (from my.telegram.org).\n"
                "Send /cancel to abort.")

    async def _cmd_changeapi(self, update, ctx):
        msg = update.effective_message
        uid = int(update.effective_user.id)
        if not self._is_admin(uid):
            return
        self._set_wiz(uid, _CA_API_ID)
        cur = _workers.get_creds(self._db)
        cur_txt = f"current api_id={cur[0]}" if cur else "no creds saved yet"
        await msg.reply_text(
            f"🔑 Change API creds ({cur_txt}).\n\n"
            "Step 1/2: send the NEW API ID.\nSend /cancel to abort.")

    async def _cmd_cancel(self, update, ctx):
        msg = update.effective_message
        uid = int(update.effective_user.id)
        if not self._is_admin(uid):
            return
        if uid in self._wizard:
            self._wizard.pop(uid, None)
            await msg.reply_text("❎ Cancelled.")

    async def _cmd_listworker(self, update, ctx):
        msg = update.effective_message
        uid = int(update.effective_user.id)
        if not self._is_admin(uid):
            return
        creds = _workers.get_creds(self._db)
        workers = _workers.list_workers(self._db)
        runtime = self._status_provider() if self._status_provider else {}
        lines = ["🛠 Bot 2 workers", ""]
        if creds:
            lines.append(f"🔑 api_id  : {creds[0]}")
            lines.append(f"🔑 api_hash: {creds[1][:6]}…{creds[1][-4:]}")
        else:
            lines.append("🔑 api_id/hash: (not set)")
        lines.append("")
        if not workers:
            lines.append("No workers yet. Use /addworker to add one.")
        else:
            for w in workers:
                sess = str(w["session"])
                masked = f"{sess[:8]}…{sess[-6:]}" if len(sess) > 16 else "****"
                rt = runtime.get(w["slot"], {})
                live = rt.get("running", False)
                label = rt.get("state", "pending connection")
                if not live and label != "error":
                    label = "pending connection"
                if rt.get("paused"):
                    label = "⏸ paused"
                lines.append(f"  {w['slot']}. session {masked}")
                lines.append(f"     {label} · task={'running' if live else 'not running'} · "
                             f"completed={rt.get('completed', 0)}")
                if rt.get("account_id"):
                    lines.append(f"     account={rt['account_id']}")
                if rt.get("error"):
                    lines.append(f"     setup error: {rt['error']}")
                if rt.get("last_result"):
                    lines.append(f"     @{rt.get('last_downloader', '?')}: {rt['last_result']}")
                if rt.get("last_reply"):
                    lines.append(f"     reply: {rt['last_reply']}")
        lines.append("")
        lines.append("/addworker · /removeworker <n…|all> · /changeapiandhash")
        await msg.reply_text("\n".join(lines))

    async def _cmd_removeworker(self, update, ctx):
        msg = update.effective_message
        uid = int(update.effective_user.id)
        if not self._is_admin(uid):
            return
        args = [a.strip().lower() for a in (ctx.args or []) if a.strip()]
        if not args:
            await msg.reply_text("Usage: /removeworker <n> [n2 …] | all")
            return
        if "all" in args:
            n = _workers.clear_workers(self._db)
            self._reload()
            await msg.reply_text(f"🗑 Removed ALL workers ({n}). Fetcher idles "
                                 "until you /addworker.")
            return
        nums = []
        for a in args:
            if not a.isdigit():
                await msg.reply_text(f"⚠️ Bad slot number: {a!r}. "
                                     "Use numbers from /listworker, or 'all'.")
                return
            nums.append(int(a))
        removed = _workers.remove_workers(self._db, nums)
        if not removed:
            await msg.reply_text("⚠️ No matching worker slot(s). Check /listworker.")
            return
        self._reload()
        left = len(_workers.list_workers(self._db))
        await msg.reply_text(
            f"🗑 Removed worker(s): {', '.join(map(str, removed))}. "
            f"{left} worker(s) remain (renumbered).")

    # -- log channel commands (v13.12) --------------------------------------
    async def _cmd_addlog(self, update, ctx):
        msg = update.effective_message
        uid = int(update.effective_user.id)
        if not self._is_admin(uid):
            return
        args = [a.strip() for a in (ctx.args or []) if a.strip()]
        if not args:
            await msg.reply_text(
                "Usage: /addlog <channel id>\n"
                "Example: /addlog -1003796521529\n"
                "This bot must be an ADMIN in that channel with the "
                "'Post Messages' right.")
            return
        target = args[0]
        if not (re.fullmatch(r"-?\d{6,20}", target)
                or re.fullmatch(r"@[A-Za-z0-9_]{4,}", target)):
            await msg.reply_text("⚠️ Bad channel id. Send a numeric id like "
                                 "-100… or a @publicusername.")
            return
        chat_id = int(target) if re.fullmatch(r"-?\d+", target) else target
        try:
            sent = await ctx.bot.send_message(
                chat_id=chat_id,
                text="📡 Bot 2 log channel connected via /addlog.\n"
                     "Status dashboard and alerts will post here.")
        except Exception as e:  # noqa: BLE001
            await msg.reply_text(
                f"❌ Could not post to {target}: {e}\n"
                "Add this bot as a channel ADMIN with 'Post Messages' and "
                "retry /addlog.")
            return
        canon = str(getattr(getattr(sent, "chat", None), "id", chat_id))
        if _workers.set_log_channel(self._db, canon):
            self._reload()
            await msg.reply_text(
                f"✅ Log channel set to {canon}. No restart needed — the "
                "dashboard picks it up within ~20s. Remove with /removelog.")
        else:
            await msg.reply_text("❌ Test post worked but Mongo save failed. "
                                 "Check logs and retry /addlog.")

    async def _cmd_removelog(self, update, ctx):
        msg = update.effective_message
        uid = int(update.effective_user.id)
        if not self._is_admin(uid):
            return
        cur = _workers.get_log_channel(self._db)
        if not cur:
            await msg.reply_text("ℹ️ No Mongo log channel set (env "
                                 "LOG_CHANNEL_ID, if set, still applies).")
            return
        if _workers.clear_log_channel(self._db):
            self._reload()
            await msg.reply_text(
                f"🗑 Log channel {cur} removed. Dashboard and alerts stop; "
                "env LOG_CHANNEL_ID (if set) becomes the fallback again.")
        else:
            await msg.reply_text("❌ Mongo error — could not remove the log "
                                 "channel. Check logs.")

    async def _cmd_logstatus(self, update, ctx):
        msg = update.effective_message
        uid = int(update.effective_user.id)
        if not self._is_admin(uid):
            return
        mongo = _workers.get_log_channel(self._db)
        env_ch = _first("LOG_CHANNEL_ID")
        eff = mongo or env_ch or "(none)"
        tok_src = ("dashboard token env" if _first(
            "BOT_2_PDF_FECTHER", "LOG_BOT_TOKEN", "DASHBOARD_BOT_TOKEN",
            "TELEGRAM_LOG_BOT_TOKEN") else ("BOT2_TOKEN" if self._token
                                            else "(none)"))
        await msg.reply_text("\n".join([
            "📡 Bot 2 log channel",
            f"Mongo (/addlog): {mongo or '(not set)'}",
            f"Env LOG_CHANNEL_ID: {env_ch or '(not set)'}",
            f"Effective target: {eff}",
            f"Sender token: {tok_src}",
            "",
            "/addlog <channel id> · /removelog",
        ]))

    # -- pause / resume workers (v13.13) ------------------------------------
    async def _cmd_pauseworker(self, update, ctx):
        msg = update.effective_message
        if not self._is_admin(int(update.effective_user.id)):
            return
        args = [a.strip() for a in (ctx.args or []) if a.strip()]
        ws = _workers.list_workers(self._db)
        if not ws:
            await msg.reply_text("\u2139\ufe0f No workers to pause. Add one via /addworker.")
            return
        valid = {w["slot"] for w in ws}
        if args and args[0].lower() == "all":
            nums = valid
        else:
            try:
                nums = {int(a) for a in args}
            except ValueError:
                nums = set()
            bad = nums - valid
            if bad:
                await msg.reply_text(f"\u26a0\ufe0f No such worker(s): {sorted(bad)}. Check /listworker.")
                return
        if not nums:
            await msg.reply_text("Usage: /pauseworker 1 | 1 2 3 | all")
            return
        res = _workers.pause_workers(self._db, nums)
        if res is None:
            await msg.reply_text("\u274c Mongo error \u2014 could not save pause state.")
            return
        self._reload()
        await msg.reply_text(
            f"\u23f8 Paused worker(s): {sorted(nums)} \u2014 no restart needed; they "
            "disconnect within seconds. Resume with /resumeworker. "
            "/listworker to confirm.")

    async def _cmd_resumeworker(self, update, ctx):
        msg = update.effective_message
        if not self._is_admin(int(update.effective_user.id)):
            return
        args = [a.strip() for a in (ctx.args or []) if a.strip()]
        nums = None
        if args and args[0].lower() != "all":
            try:
                nums = {int(a) for a in args}
            except ValueError:
                await msg.reply_text("Usage: /resumeworker 1 | 1 2 | all")
                return
        res = _workers.resume_workers(self._db, nums)
        if res is None:
            await msg.reply_text("\u274c Mongo error \u2014 could not save resume state.")
            return
        self._reload()
        label = "all" if nums is None else " ".join(map(str, sorted(nums)))
        await msg.reply_text(
            f"\u25b6\ufe0f Resumed ({label}). Workers reconnect within "
            "seconds \u2014 /listworker to confirm.")

    async def _on_text(self, update, ctx):
        msg = update.effective_message
        uid = int(update.effective_user.id)
        if not self._is_admin(uid):
            return
        w = self._wiz(uid)
        step = w["step"]
        if step == _IDLE:
            return
        text = (msg.text or "").strip()
        if text.lower() == "/cancel":
            self._wizard.pop(uid, None)
            await msg.reply_text("❎ Cancelled.")
            return

        if step == _AW_API_ID:
            if not re.fullmatch(r"\d{4,12}", text):
                await msg.reply_text("⚠️ API ID must be a number. Try again "
                                     "(or /cancel).")
                return
            self._set_wiz(uid, _AW_API_HASH, api_id=text)
            await msg.reply_text("Step 2/3: send the API HASH.")
            return

        if step == _AW_API_HASH:
            if not re.fullmatch(r"[0-9a-fA-F]{20,64}", text):
                await msg.reply_text("⚠️ API HASH looks wrong (hex string). "
                                     "Try again (or /cancel).")
                return
            self._set_wiz(uid, _AW_SESSION, api_id=w["api_id"], api_hash=text)
            await msg.reply_text("Step 3/3: send the STRING SESSION.")
            return

        if step == _AW_SESSION:
            try:
                from telethon.sessions import StringSession
                StringSession(text)
                valid = len(text) >= 40
            except Exception:
                valid = False
            if not valid:
                await msg.reply_text("⚠️ That doesn't look like a Telethon "
                                     "string session. Try again (or /cancel).")
                return
            if any(row["session"] == text for row in _workers.list_workers(self._db)):
                await msg.reply_text("⚠️ This session is already stored. Send a different user account session.")
                return
            if "api_id" in w:  # first worker — save creds first
                _workers.set_creds(self._db, w["api_id"], w["api_hash"])
            creds = _workers.get_creds(self._db) or (w.get("api_id", ""),
                                                     w.get("api_hash", ""))
            slot = _workers.add_worker(self._db, text, creds[0], creds[1],
                                       added_by=uid)
            self._wizard.pop(uid, None)
            if slot:
                self._reload()
                await msg.reply_text(
                    f"✅ Worker #{slot} saved. Connection is pending — no restart needed. "
                    "Check /listworker for actual live status.")
            else:
                await msg.reply_text("❌ Could not save the worker (Mongo error). "
                                     "Check logs and retry /addworker.")
            return

        if step == _CA_API_ID:
            if not re.fullmatch(r"\d{4,12}", text):
                await msg.reply_text("⚠️ API ID must be a number. Try again "
                                     "(or /cancel).")
                return
            self._set_wiz(uid, _CA_API_HASH, api_id=text)
            await msg.reply_text("Step 2/2: send the NEW API HASH.")
            return

        if step == _CA_API_HASH:
            if not re.fullmatch(r"[0-9a-fA-F]{20,64}", text):
                await msg.reply_text("⚠️ API HASH looks wrong. Try again "
                                     "(or /cancel).")
                return
            ok = _workers.set_creds(self._db, w["api_id"], text)
            self._wizard.pop(uid, None)
            if ok:
                self._reload()
                await msg.reply_text("✅ API creds updated. Workers will "
                                     "reconnect with the new creds.")
            else:
                await msg.reply_text("❌ Could not save creds (Mongo error).")
            return
