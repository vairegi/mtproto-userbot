"""Bot entry point — webhook server on aiohttp, aiogram dispatcher.

/health returns 200 with ZERO DB access (Render probes it every ~5s).
/webhook is the Telegram update endpoint.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, Update

from .config import settings
from .db import init_schema as init_turso_schema
from .handlers import (admin_stats, backfill_cmds, backup_cmds, callbacks,
                       channel_posts, content_cmds, diag_cmds, forward_cmds,
                       fsub_cmds, massdlt_cmds, member_cmds, migrate_cmds,
                       queue_cmds, setup_cmds, shortener_cmds)
from .services import backup as backup_svc
from .services import scheduler, tg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("main")


# ------------------------- dispatcher -------------------------
dp = Dispatcher()
dp.include_router(channel_posts.router)
dp.include_router(callbacks.router)
dp.include_router(setup_cmds.router)
dp.include_router(backfill_cmds.router)
dp.include_router(queue_cmds.router)
dp.include_router(content_cmds.router)
dp.include_router(diag_cmds.router)
dp.include_router(massdlt_cmds.router)
dp.include_router(forward_cmds.router)
dp.include_router(fsub_cmds.router)
dp.include_router(member_cmds.router)
dp.include_router(admin_stats.router)
dp.include_router(backup_cmds.router)
dp.include_router(migrate_cmds.router)
dp.include_router(shortener_cmds.router)


USER_MENU = [
    BotCommand(command="start", description="Welcome / redeem file"),
    BotCommand(command="help", description="Show help"),
    BotCommand(command="whoami", description="Your id and role"),
    BotCommand(command="favs", description="Saved files"),
    BotCommand(command="rfavs", description="Remove favorite(s)"),
    BotCommand(command="mystats", description="Your personal stats"),
    BotCommand(command="leaderboard", description="Weekly top file fetchers"),
    BotCommand(command="similar", description="Similar recommendations on/off"),
]

ADMIN_MENU = USER_MENU + [
    BotCommand(command="queue", description="Next 10 in queue"),
    BotCommand(command="queueinfo", description="Queue overview"),
    BotCommand(command="peek", description="Next N titles only"),
    BotCommand(command="whereami", description="Current cursor + state"),
    BotCommand(command="find", description="Search captions"),
    BotCommand(command="dripnow", description="Publish next N covers now"),
    BotCommand(command="dripstop", description="Cancel running drip"),
    BotCommand(command="setschedule", description="IST slots × batch"),
    BotCommand(command="scheduleoff", description="Clear schedule"),
    BotCommand(command="pauseposting", description="Pause drip"),
    BotCommand(command="resumeposting", description="Resume drip"),
    BotCommand(command="skip", description="Skip next N / up to link"),
    BotCommand(command="skip_range", description="Skip a #A-#B range"),
    BotCommand(command="unskip", description="Requeue one #N"),
    BotCommand(command="jumpto", description="Force queue back to #N"),
    BotCommand(command="queue_reset", description="Nuclear: reset queue"),
    BotCommand(command="repost", description="Re-publish #N or code"),
    BotCommand(command="preview", description="DM-preview #N or code"),
    BotCommand(command="deletepost", description="Drop #N or code from queue"),
    BotCommand(command="spoiler", description="Spoiler 1/0"),
    BotCommand(command="protect", description="Protect-content 1/0"),
    BotCommand(command="postcaption", description="Caption extra below covers"),
    BotCommand(command="filecaption", description="Caption extra below files"),
    BotCommand(command="addchannel", description="Register a channel"),
    BotCommand(command="removechannel", description="Unregister"),
    BotCommand(command="listchannels", description="List channels"),
    BotCommand(command="setlog", description="Set log channel"),
    BotCommand(command="setcursor", description="Set DB-channel cursor"),
    BotCommand(command="tgsetapi", description="Set MTProto creds"),
    BotCommand(command="tglogin", description="MTProto login"),
    BotCommand(command="tgcode", description="Complete MTProto login"),
    BotCommand(command="tgstatus", description="MTProto status"),
    BotCommand(command="backfill_start", description="Start MTProto backfill"),
    BotCommand(command="backfill_resume", description="Resume from cursor"),
    BotCommand(command="backfill_stop", description="Stop backfill"),
    BotCommand(command="backfill_status", description="Backfill state (in-mem)"),
    BotCommand(command="backfill_reset", description="Clear backfill state"),
    BotCommand(command="massdlt", description="Bulk delete between links"),
    BotCommand(command="massdlt_status", description="/massdlt progress"),
    BotCommand(command="massdlt_stop", description="Stop /massdlt"),
    BotCommand(command="forward", description="Userbot: forward a msg range"),
    BotCommand(command="forward_status", description="/forward progress"),
    BotCommand(command="forward_stop", description="Stop /forward"),
    BotCommand(command="forward_resume", description="Resume stopped /forward"),
    BotCommand(command="autodelete", description="Self-destruct timer (8h/2m/off)"),
    BotCommand(command="fsub", description="Add join-gate channel"),
    BotCommand(command="fsublist", description="List join-gate channels"),
    BotCommand(command="fsubremove", description="Remove join-gate channel"),
    BotCommand(command="add", description="Bulk-add members to a channel (userbot)"),
    BotCommand(command="broadcast", description="Broadcast replied message to all users"),
    BotCommand(command="favsall", description="Top savers leaderboard"),
    BotCommand(command="addbackup", description="Add backup channel"),
    BotCommand(command="removebackup", description="Remove backup channel"),
    BotCommand(command="listbackup", description="List backup channels"),
    BotCommand(command="backup", description="Run backup pass"),
    BotCommand(command="backup10", description="Mirror 10 posts (test)"),
    BotCommand(command="resetbackup", description="Reset backup progress"),
    BotCommand(command="undoresetbackup", description="Undo the last reset"),
    BotCommand(command="dltbackup", description="Wipe backup progress"),
    BotCommand(command="pausebackup", description="Pause auto-backup"),
    BotCommand(command="resumebackup", description="Resume auto-backup"),
    BotCommand(command="backupstatus", description="Backup progress"),
    BotCommand(command="addsuperadmin", description="Grant super-admin"),
    BotCommand(command="migrate_mongo", description="Migrate Turso → MongoDB"),
    BotCommand(command="migrate_mongo_status", description="Migration progress"),
    BotCommand(command="fsub_sync", description="Import pending fsub join-requests"),
    BotCommand(command="debug", description="Full state dump"),
    BotCommand(command="stats", description="Count summary"),
    BotCommand(command="setstart", description="Custom /start text (HTML)"),
    BotCommand(command="setstartphoto", description="Reply to photo = /start cover"),
    BotCommand(command="setstartbtn", description="Add /start button (Label | url)"),
    BotCommand(command="clearstartbtns", description="Remove all /start buttons"),
    BotCommand(command="previewstart", description="Preview the /start message"),
]


async def push_menus(bot: Bot) -> None:
    try:
        await tg.set_my_commands(bot, USER_MENU, scope=BotCommandScopeAllPrivateChats())
    except Exception:
        log.exception("set user menu failed")


# ------------------------- aiohttp app -------------------------
async def handle_health(request: web.Request) -> web.Response:
    # ZERO DB access — Render probes this every ~5s.
    return web.Response(text="ok")


async def handle_verify(request: web.Request) -> web.Response:
    """v4.3: VPLINK landing page. Token validated exists+unexpired (peek, NOT
    consumed — the bot consumes it when the user taps start=verify_)."""
    from .services import shortener as _sh
    from .services.posting import get_bot_username
    token = (request.query.get("t") or "").strip()
    bot: Bot = request.app["bot"]
    username = await get_bot_username(bot)
    if not token or _sh.peek_token(token) is None or not username:
        return web.Response(status=400, text="Verification link is invalid or expired. Please tap Get File again.")
    heading = await _sh.get_overlay_text()
    deep_link = f"https://t.me/{username}?start=verify_{token}"
    return web.Response(text=_sh.landing_html(deep_link, heading),
                        content_type="text/html")


async def handle_webhook(request: web.Request) -> web.Response:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if settings.webhook_secret and secret != settings.webhook_secret:
        return web.Response(status=403, text="forbidden")
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")
    bot: Bot = request.app["bot"]
    try:
        update = Update.model_validate(data)
    except Exception:
        return web.Response(status=400, text="bad update")
    # Dispatch in background — webhook ACKs in <50ms so Telegram never
    # retries/stalls during Render free-tier cold starts.
    asyncio.create_task(dp.feed_update(bot=bot, update=update))
    return web.Response(text="ok")


# ------------------------- free-tier keep-alive -----------------------------
# Render free instances sleep after ~15 min without inbound traffic. Telegram's
# webhook timeout is shorter than the ~50s cold-start wake, so a sleeping
# instance silently drops updates ("bot doesn't respond until I revoke the
# token" — revoking just forced a full fresh boot). Pinging our own /health
# every 4 minutes keeps the instance warm forever.
_keepalive_task = None


async def _keepalive_loop(base_url: str) -> None:
    import aiohttp as _aio
    url = f"{base_url}/health"
    while True:
        try:
            async with _aio.ClientSession() as sess:
                async with sess.get(url, timeout=_aio.ClientTimeout(total=10)) as resp:
                    await resp.read()
        except Exception:
            pass
        await asyncio.sleep(240)


def _start_keepalive(app: web.Application) -> None:
    global _keepalive_task
    if not settings.base_webhook_url:
        return
    if _keepalive_task is None or _keepalive_task.done():
        _keepalive_task = asyncio.create_task(_keepalive_loop(settings.base_webhook_url))
        log.info("keepalive started → %s/health every 240s", settings.base_webhook_url)


async def on_startup(app: web.Application) -> None:
    bot: Bot = app["bot"]
    # Schema init follows the ACTIVE backend. Turso is skipped entirely in
    # mongo mode (zero Turso reads/writes after cutover) but its code stays
    # in place as the frozen fallback — flip DB_BACKEND back and it boots
    # on Turso again untouched.
    if settings.db_backend == "mongo":
        from . import mongo_db
        await mongo_db.init_schema()
        log.info("DB backend: MONGO (db=%s)", settings.mongodb_db_name)
    else:
        await asyncio.to_thread(init_turso_schema)
        log.info("DB backend: TURSO")
    await push_menus(bot)
    if settings.base_webhook_url:
        url = f"{settings.base_webhook_url}/webhook"
        try:
            await bot.set_webhook(
                url=url,
                secret_token=settings.webhook_secret or None,
                allowed_updates=["message", "callback_query", "channel_post", "chat_join_request"],
                drop_pending_updates=True,
            )
            log.info("webhook set → %s", url)
        except Exception:
            log.exception("set_webhook failed")
    scheduler.start(bot)
    backup_svc.start_auto(bot)
    _start_keepalive(app)


async def on_shutdown(app: web.Application) -> None:
    bot: Bot = app["bot"]
    # v2.5: DO NOT delete the webhook on shutdown. Render stops the old
    # instance only after the new one is healthy — if the dying instance
    # deletes the webhook LAST, the new instance never re-registers (it
    # already set it seconds earlier) and the bot goes deaf until a token
    # revoke. Webhooks survive process restarts by design.
    scheduler.stop()
    try:
        await bot.session.close()
    except Exception:
        pass


def build_app() -> web.Application:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN env var missing")
    bot = Bot(token=settings.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/health", handle_health)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/verify", handle_verify)
    app.router.add_post("/webhook", handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


def main() -> None:
    app = build_app()
    web.run_app(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
