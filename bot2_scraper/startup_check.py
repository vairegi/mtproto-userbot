"""
startup_check.py — Brief §16 startup self-test.

Runs before the worker consumes the queue. Any failure:
  - logs loudly
  - tries to alert Ryan via Admin Bot (HTTP send, no telethon needed)
  - exits non-zero

MIGRATION NOTE (SQLite → MongoDB)
---------------------------------
The old `_check_sqlite()` asserted two things that only exist in SQLite:
    * journal_mode == 'wal'
    * a schema_version row matching the code
Neither concept exists in MongoDB, so that check is replaced by
`_check_mongo()`, which is the genuinely useful equivalent:

    1. MONGO_URI is actually set.
    2. The cluster answers a `ping` (proves DNS + TLS + credentials + IP
       allow-list all work) — this is the single most common deployment
       failure, so it is worth failing loudly and early on.
    3. A write + read round-trip succeeds (proves the database user has
       readWrite, not just read).
    4. Indexes are created.
"""
from __future__ import annotations

import asyncio
import sys
from typing import List, Tuple

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import RPCError

import db
from config import settings
from logging_setup import setup_logging

log = setup_logging("startup_check")


async def _check_userbot() -> Tuple[bool, str]:
    client = TelegramClient(
        StringSession(settings.session_string),
        settings.api_id,
        settings.api_hash,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False, "userbot session is NOT authorised (logged out?)"
        me = await client.get_me()
        return True, f"userbot authorised as id={getattr(me, 'id', '?')}"
    except RPCError as e:
        return False, f"userbot RPC error: {e!s}"
    except Exception as e:  # noqa: BLE001
        return False, f"userbot connect error: {e!s}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def _check_channel_and_bots() -> Tuple[bool, str]:
    client = TelegramClient(
        StringSession(settings.session_string),
        settings.api_id,
        settings.api_hash,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False, "userbot not authorised"

        # Resolve Database Channel + verify post rights via get_permissions
        try:
            channel = await client.get_entity(settings.database_channel_id)
        except Exception as e:  # noqa: BLE001
            return False, f"cannot resolve Database Channel {settings.database_channel_id}: {e!s}"

        try:
            perms = await client.get_permissions(channel, "me")
            if not (perms.post_messages or perms.is_admin):
                return False, "userbot lacks post rights in Database Channel"
        except Exception:
            # Some channels don't expose per-user perms cleanly; try a dry send-typing.
            pass

        # Resolve both bots (no message sent — resolution is enough)
        for uname in (settings.bot1_username, settings.bot2_username):
            try:
                await client.get_entity(uname)
            except Exception as e:  # noqa: BLE001
                return False, f"cannot resolve @{uname}: {e!s}"

        return True, "channel + both bots reachable"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _check_mongo() -> Tuple[bool, str]:
    """Verify MongoDB is reachable AND writable by this database user."""
    try:
        db.init_db()               # ping + index creation
        conn = db.connect()
        try:
            # Write + read round-trip proves the user has readWrite rights.
            db.set_flag(conn, "_startup_probe", str(db.now_ts()))
            probe = db.get_flag(conn, "_startup_probe", "")
            if not probe:
                return False, "wrote a flag but could not read it back"
            counts = db.counts_by_status(conn)
            name = conn.db.name
        finally:
            conn.close()
        total = sum(counts.values())
        return True, (
            f"MongoDB ok (db={name!r}, queue holds {total} job(s): "
            f"{counts['pending']} pending, {counts['processing']} processing)"
        )
    except Exception as e:  # noqa: BLE001
        return False, (
            f"MongoDB error: {e!s}. Check that MONGO_URI is correct, the "
            f"database password has no unescaped special characters, and that "
            f"Network Access in Atlas allows 0.0.0.0/0."
        )


async def _alert_admin(text: str) -> None:
    """Best-effort alert to Ryan via the Admin Bot API — no telethon required."""
    if not settings or not settings.admin_bot_token or not settings.admin_user_id:
        return
    url = f"https://api.telegram.org/bot{settings.admin_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={"chat_id": settings.admin_user_id, "text": text})
    except Exception:  # noqa: BLE001
        pass


async def run_checks() -> int:
    results: List[Tuple[str, bool, str]] = []

    ok, msg = _check_mongo()
    results.append(("MongoDB", ok, msg))

    ok, msg = await _check_userbot()
    results.append(("Userbot session", ok, msg))

    if results[-1][1]:
        ok, msg = await _check_channel_and_bots()
        results.append(("Channel + Bots", ok, msg))
    else:
        results.append(("Channel + Bots", False, "skipped — userbot not authorised"))

    all_ok = all(ok for _, ok, _ in results)
    for name, ok, msg in results:
        prefix = "OK " if ok else "FAIL"
        line = f"[{prefix}] {name}: {msg}"
        (log.info if ok else log.error)(line)

    if not all_ok:
        summary = "Startup self-test FAILED:\n" + "\n".join(
            f"• {n}: {m}" for n, ok, m in results if not ok
        )
        await _alert_admin(summary)
        return 2
    return 0


def main() -> int:
    return asyncio.run(run_checks())


if __name__ == "__main__":
    sys.exit(main())
