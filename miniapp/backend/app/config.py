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
    bot_token: str = os.environ.get("BOT_TOKEN", "")
    admin_user_id: int = _int("ADMIN_USER_ID", 0)

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
