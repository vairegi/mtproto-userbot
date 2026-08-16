"""
config.py — Environment config for the Mini App backend.

Reuses the same env vars as admin_bot.py where possible. When run in the same
process/container as the bot, it also tries to import the bot's settings module
so we stay in sync automatically.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # Auth / Telegram
    bot_token: str = os.environ.get("BOT_TOKEN", "") or os.environ.get("ADMIN_BOT_TOKEN", "")
    admin_user_id: int = _int("ADMIN_USER_ID", 0)
    # v12.25: optional static key for the read-only / cache-rewrite
    # maintenance endpoints (shape-audit, renormalize, dry-run, hitmiss).
    # Lets curl / scripts / scheduled jobs drive them without a Telegram
    # initData session. EMPTY = disabled (those routes still accept initData).
    admin_static_key: str = os.environ.get("ADMIN_STATIC_KEY", "")

    # v12.28: region-aware Turso token-bucket split (paired with BOT 1
    # v1.19). When BOT 0 is deployed to a region OTHER than the one BOT 1
    # occupies, set BOT0_REGION (e.g. "ap-singapore") so its
    # nhentai_ratelimit bucket_ids are suffixed "_<region>" and it spends
    # from its own row instead of contending with the other-region bot.
    # EMPTY (default) = legacy bucket ids, byte-identical behavior — the
    # current Oregon backend runs with this unset, so v12.28 is a pure
    # no-op until BOT0_REGION is explicitly set.
    bot0_region: str = os.environ.get("BOT0_REGION", "").strip()

    # V2 DM-delivery on dedup (BUG 1) — the database channel the admin bot
    # copyMessages from when a user taps Queue on an already-completed
    # gallery. Same env var as the parent bot uses.
    database_channel_id: int = _int("DATABASE_CHANNEL_ID", _int("CHANNEL_ID", 0))

    # Mongo — reuse the same URI as the bot
    mongo_uri: str = os.environ.get("MONGO_URI", "")
    mongo_db: str = os.environ.get("MONGO_DB", "doujinshi")

    # App behaviour
    default_public_mode: bool = os.environ.get("MINIAPP_PUBLIC_DEFAULT", "1") == "1"
    default_daily_limit: int = _int("MINIAPP_DEFAULT_DAILY_LIMIT", 20)
    default_cooldown_s: int = _int("MINIAPP_DEFAULT_COOLDOWN_S", 0)

    # Where to import the parent bot's helpers from. If the Mini App is
    # deployed alongside admin_bot.py this can stay as-is.
    bot_module_root: str = os.environ.get("MINIAPP_BOT_ROOT", "..")


settings = Settings()
