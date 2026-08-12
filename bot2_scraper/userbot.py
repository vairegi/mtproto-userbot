"""
userbot.py — Telethon client factory + a small shared helper module.

Kept intentionally thin: the worker owns the client's lifecycle.

MIGRATION NOTE (serverless hosting)
-----------------------------------
`build_client()` is unchanged, so worker.py / startup_check.py keep working
exactly as before.

What was ADDED is a `__main__` block. start.sh runs this file in the
FOREGROUND (the process that holds the container open), and previously running
it did nothing at all — the module only defined a function, so it exited
instantly and the container would have been shut down by the platform.

Now, when run directly, it performs a genuine end-to-end session validation:
    1. Connects to Telegram with the STRING_SESSION.
    2. Confirms the session is authorised (not revoked/logged out).
    3. Prints which account it logged in as.
    4. Exits 0 on success, non-zero on failure.

That makes the very first thing your logs show a clear yes/no answer to
"is my session string valid?", which is the #1 deployment problem.
"""
from __future__ import annotations

import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import settings

# v12.18 (audit fix 3b): Telethon client safeguards for the 512 MB Render
# plan. During multi-MB PDF relays a FloodWait longer than the default
# 60 s makes Telethon RAISE instead of sleep; the worker's exception path
# then retains references to in-flight upload buffers until GC, which is
# a slow RAM leak under sustained flood. Sleeping inside Telethon keeps
# buffers bounded. Both knobs are env-overridable without a redeploy.
try:
    _FLOOD_SLEEP_THRESHOLD = int(os.getenv("TELETHON_FLOOD_SLEEP_SEC", "300"))
except (TypeError, ValueError):
    _FLOOD_SLEEP_THRESHOLD = 300
try:
    _REQUEST_RETRIES = int(os.getenv("TELETHON_REQUEST_RETRIES", "3"))
except (TypeError, ValueError):
    _REQUEST_RETRIES = 3


def build_client() -> TelegramClient:
    """Create (but do not connect) the Telethon client from the string session."""
    return TelegramClient(
        StringSession(settings.session_string),
        settings.api_id,
        settings.api_hash,
        flood_sleep_threshold=_FLOOD_SLEEP_THRESHOLD,  # v12.18
        request_retries=_REQUEST_RETRIES,              # v12.18
    )


async def _validate_session() -> int:
    """Connect once and report whether the session works. Returns an exit code."""
    if settings is None:
        print("userbot.py: FATAL — settings failed to load. "
              "Check your environment variables.", file=sys.stderr)
        return 1

    client = build_client()
    try:
        print("userbot.py: connecting to Telegram...")
        await client.connect()

        if not await client.is_user_authorized():
            print(
                "userbot.py: FATAL — the session string is NOT authorised.\n"
                "  The session was probably revoked (Telegram > Settings >\n"
                "  Devices > Terminate session) or copied incompletely.\n"
                "  Generate a fresh one with: python scripts/gen_session.py",
                file=sys.stderr,
            )
            return 3

        me = await client.get_me()
        uname = f"@{me.username}" if getattr(me, "username", None) else "(no username)"
        print(
            f"userbot.py: session OK — logged in as {uname} "
            f"[id={getattr(me, 'id', '?')}]"
        )
        return 0

    except Exception as e:  # noqa: BLE001
        print(f"userbot.py: FATAL — connection error: {e!s}", file=sys.stderr)
        print(
            "  Check API_ID and API_HASH are correct and belong to the same\n"
            "  Telegram app that generated STRING_SESSION.",
            file=sys.stderr,
        )
        return 4
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    try:
        return asyncio.run(_validate_session())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
