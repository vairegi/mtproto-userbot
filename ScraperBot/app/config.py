"""
config.py — env-driven settings for ScraperBot.

All knobs are Render-env-tunable so ops can retune without a redeploy.
Values default to the same numbers BOT 0's crons use, so cache writes
from BOT 1 look identical to BOT 0's from Turso's point of view.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _env_csv(name: str, default: List[str]) -> List[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_admin_ids() -> List[int]:
    raw = (os.getenv("BOT1_ADMIN_USER_IDS") or "").strip()
    if not raw:
        return []
    out: List[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


@dataclass
class Settings:
    # Shared with BOT 0
    mongo_uri: str = os.getenv("MONGO_URI", "").strip()
    mongo_db_name: str = os.getenv("MONGO_DB_NAME", "relaybot").strip() or "relaybot"
    turso_url: str = os.getenv("TURSO_DATABASE_URL", "").strip()
    turso_token: str = os.getenv("TURSO_AUTH_TOKEN", "").strip()

    # BOT 1 only
    bot_token: str = os.getenv("BOT1_TOKEN", "").strip()
    admin_key: str = os.getenv("BOT1_ADMIN_KEY", "").strip()
    admin_user_ids: List[int] = field(default_factory=_env_admin_ids)
    webhook_secret: str = os.getenv("BOT1_WEBHOOK_SECRET", "").strip()

    # Scraper toggles
    scraper_enabled: bool = _env_bool("SCRAPER_ENABLED", True)
    nhentai_api_key: str = os.getenv("NHENTAI_API_KEY", "").strip()
    user_agent: str = (
        os.getenv("NHENTAI_USER_AGENT")
        or "DoujinshiUniverse-ScraperBot/1.0 (+https://github.com/vairegi/mtproto-userbot)"
    )

    # List sweep — v1.6 pacing matches BOT 0's prefetch_cron exactly:
    #   PREFETCH_DELAY_SEC=1s, PREFETCH_INTERVAL_SEC=6h, PREFETCH_MAX_PAGES=30
    # The real anti-ban mechanism is the shared token bucket (10/min for
    # /search) + the 6-hour inter-phase gap, NOT the per-fetch delay.
    list_sorts: List[str] = field(default_factory=lambda: _env_csv(
        "LIST_SORTS", ["popular", "date", "popular-today", "popular-week"]))
    list_max_pages: int = _env_int("LIST_MAX_PAGES", 30)
    # v1.13: tag sorts sweep fewer pages than chip sorts. Chip sorts (the
    # four in `list_sorts`) are what users see on the Discover screen so
    # they get the full LIST_MAX_PAGES depth. Tag sorts (trending +
    # EXTRA_TAG_SORTS) are typed-search fodder — users almost never scroll
    # past page 7, and going deeper burns the shared /search bucket for no
    # visible win. Env-overridable via LIST_TAG_MAX_PAGES.
    list_tag_max_pages: int = _env_int("LIST_TAG_MAX_PAGES", 7)
    list_tick_sec: int = _env_int("LIST_TICK_SEC", 21600)   # 6 hours
    list_delay_sec: float = _env_float("LIST_DELAY_SEC", 1.0)
    # Sleep after a bucket-skip. Short (1s) matches BOT 0 — the bucket is
    # the throttle, not this sleep.
    list_skip_sleep_sec: float = _env_float("LIST_SKIP_SLEEP_SEC", 1.0)

    # Detail sweep
    details_tick_sec: int = _env_int("DETAILS_TICK_SEC", 60)
    details_rest_sec: float = _env_float("DETAILS_REST_SEC", 3.0)
    details_per_tick: int = _env_int("DETAILS_PER_TICK", 5)
    details_page_cap: int = _env_int("DETAILS_PAGE_CAP", 20)

    # TTLs (must match BOT 0)
    ttl_gallery_sec: int = _env_int("NHCACHE_TTL_GALLERY_SEC", 30 * 24 * 3600)
    ttl_search_sec: int = _env_int("NHCACHE_TTL_SEARCH_SEC", 3 * 24 * 3600)
    ttl_trending_sec: int = _env_int("NHCACHE_TTL_TRENDING_SEC", 1800)

    # Buckets
    bucket_search: int = _env_int("BUCKET_SEARCH", 10)
    bucket_galleries: int = _env_int("BUCKET_GALLERIES", 20)

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO"

    # Manual per-tag sweeps (always included on top of trending tags).
    extra_tag_sorts: List[str] = field(default_factory=lambda: _env_csv(
        "EXTRA_TAG_SORTS", ["incest"]))

    # Trending-tag auto-discovery — scrapes nhentai.net/tags/popular HTML
    # once every trending_tags_refresh_sec to pick up the current top N.
    trending_tags_enabled: bool = _env_bool("TRENDING_TAGS_ENABLED", True)
    trending_tags_top_n: int   = _env_int("TRENDING_TAGS_TOP_N", 10)
    trending_tags_refresh_sec: int = _env_int("TRENDING_TAGS_REFRESH_SEC", 24 * 3600)

    # Live channel dashboard
    log_channel_id: str = os.getenv("BOT1_LOG_CHANNEL_ID", "-1003796521529").strip()
    channel_refresh_sec: int = _env_int("BOT1_CHANNEL_REFRESH_SEC", 5)

    # Timezone display for dashboard timestamps (IST = UTC+05:30).
    display_tz_offset_min: int = _env_int("BOT1_DISPLAY_TZ_OFFSET_MIN", 330)
    display_tz_label: str = os.getenv("BOT1_DISPLAY_TZ_LABEL", "IST").strip() or "IST"

    def validate(self) -> list[str]:
        """Return list of human-readable errors (empty = OK)."""
        errs: list[str] = []
        if not self.mongo_uri:
            errs.append("MONGO_URI is required")
        if not self.turso_url:
            errs.append("TURSO_DATABASE_URL is required")
        if not self.turso_token:
            errs.append("TURSO_AUTH_TOKEN is required")
        if not self.admin_key:
            errs.append("BOT1_ADMIN_KEY is required (protects /trigger /pause /resume)")
        return errs


settings = Settings()
