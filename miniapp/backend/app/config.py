"""
config.py — Environment config for the Mini App backend.

Reuses the same env vars as admin_bot.py where possible.

v0.3 security changes:
  * ADMIN_USER_IDS supports a comma-separated list ("111,222"). The legacy
    single ADMIN_USER_ID still works and is merged in.
  * The dev auth bypass is now OPT-IN: it requires MINIAPP_DEV_MODE=1 *and*
    an empty BOT_TOKEN *and* a configured admin. Previously an accidentally
    missing BOT_TOKEN in production silently handed every visitor admin.
  * CORS allow-list via MINIAPP_ALLOWED_ORIGINS.
  * initData replay window is configurable and defaults to 1 hour.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _admin_ids() -> frozenset[int]:
    """Parse ADMIN_USER_IDS (csv) and merge the legacy ADMIN_USER_ID."""
    ids: set[int] = set()
    for chunk in os.environ.get("ADMIN_USER_IDS", "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    legacy = _int("ADMIN_USER_ID", 0)
    if legacy:
        ids.add(legacy)
    return frozenset(ids)


def _origins() -> list[str]:
    raw = os.environ.get("MINIAPP_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return []
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


@dataclass
class Settings:
    # Auth / Telegram
    bot_token: str = os.environ.get("BOT_TOKEN", "")
    admin_user_ids: frozenset[int] = field(default_factory=_admin_ids)

    # Max age of an initData signature, in seconds. Telegram recommends a
    # short window; 1h is generous and still kills long replay attacks.
    auth_max_age_s: int = _int("MINIAPP_AUTH_MAX_AGE_S", 3600)

    # Explicit opt-in for the local no-Telegram dev bypass.
    dev_mode: bool = _bool("MINIAPP_DEV_MODE", False)

    # CORS
    allowed_origins: list[str] = field(default_factory=_origins)

    # Mongo — reuse the same URI as the bot
    mongo_uri: str = os.environ.get("MONGO_URI", "")
    mongo_db: str = os.environ.get("MONGO_DB", "doujinshi")

    # App behaviour
    default_public_mode: bool = os.environ.get("MINIAPP_PUBLIC_DEFAULT", "1") == "1"
    default_daily_limit: int = _int("MINIAPP_DEFAULT_DAILY_LIMIT", 20)
    default_cooldown_s: int = _int("MINIAPP_DEFAULT_COOLDOWN_S", 0)

    # Where to import the parent bot's helpers from.
    bot_module_root: str = os.environ.get("MINIAPP_BOT_ROOT", "..")

    # ---- Derived helpers --------------------------------------------------
    @property
    def admin_user_id(self) -> int:
        """Backwards-compatible single-admin accessor (0 if none)."""
        return min(self.admin_user_ids) if self.admin_user_ids else 0

    def is_admin(self, user_id) -> bool:
        try:
            return int(user_id) in self.admin_user_ids
        except (TypeError, ValueError):
            return False

    @property
    def dev_bypass_enabled(self) -> bool:
        """The dev bypass needs ALL THREE conditions. Never true in prod."""
        return bool(self.dev_mode and not self.bot_token and self.admin_user_ids)


settings = Settings()
