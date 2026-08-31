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
    # v1.22.2: set this to the service's public URL (e.g.
    # https://scraperbackup.onrender.com) and the webhook keeper in
    # main.py auto-registers /telegram?s=<secret> at boot and re-verifies
    # it every 6h — no manual setWebhook after token/Render-account moves.
    public_base_url: str = os.getenv("BOT1_PUBLIC_BASE_URL", "").strip()

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
    list_max_pages: int = _env_int("LIST_MAX_PAGES", 20)  # v1.25: was 30
    # v1.15 (#4): adaptive tick. Instead of a fixed 6 h phase gap, the
    # sweeper self-tunes: a clean phase (0 skips, 0 errors) shortens the
    # next gap toward list_tick_min_sec (fresher cache); a phase with any
    # skips or errors lengthens it toward list_tick_max_sec (eases off the
    # bucket). Off by default — set ADAPTIVE_TICK_ENABLED=1 to enable.
    adaptive_tick_enabled: bool = _env_bool("ADAPTIVE_TICK_ENABLED", False)
    list_tick_min_sec: int = _env_int("LIST_TICK_MIN_SEC", 10800)   # 3 h
    list_tick_max_sec: int = _env_int("LIST_TICK_MAX_SEC", 43200)   # 12 h
    # v1.13: tag sorts sweep fewer pages than chip sorts. Chip sorts (the
    # four in `list_sorts`) are what users see on the Discover screen so
    # they get the full LIST_MAX_PAGES depth. Tag sorts (trending +
    # EXTRA_TAG_SORTS) are typed-search fodder — users almost never scroll
    # past page 7, and going deeper burns the shared /search bucket for no
    # visible win. Env-overridable via LIST_TAG_MAX_PAGES.
    list_tag_max_pages: int = _env_int("LIST_TAG_MAX_PAGES", 5)  # v1.25: was 7
    list_tick_sec: int = _env_int("LIST_TICK_SEC", 21600)   # 6 hours

    # v1.22.4: per-sort freshness scheduling. Instead of sweeping ALL sorts
    # every phase, each sort runs only when its own interval has elapsed.
    # Cadence matches how often each nhentai sort actually changes:
    #   date          — new uploads every few minutes  → 2 h, deep crawl 24 h
    #   popular-today — reshuffles a few times a day   → 6 h
    #   popular-week  — slow drift                     → 12 h
    #   popular       — all-time classics, near-static → 24 h
    #   tag:<slug>    — trending tags drift weekly     → 24 h
    # All overridable via env: LIST_TICK_DATE_SEC, LIST_TICK_POPULAR_TODAY_SEC,
    # LIST_TICK_POPULAR_WEEK_SEC, LIST_TICK_POPULAR_SEC, LIST_TICK_TAG_SEC,
    # LIST_DATE_DEEP_PAGES / LIST_DATE_DEEP_SEC.
    list_tick_date_sec: int = _env_int("LIST_TICK_DATE_SEC", 7200)          # 2 h
    list_tick_popular_today_sec: int = _env_int("LIST_TICK_POPULAR_TODAY_SEC", 21600)   # 6 h
    list_tick_popular_week_sec: int = _env_int("LIST_TICK_POPULAR_WEEK_SEC", 43200)     # 12 h
    list_tick_popular_sec: int = _env_int("LIST_TICK_POPULAR_SEC", 86400)   # 24 h
    list_tick_tag_sec: int = _env_int("LIST_TICK_TAG_SEC", 86400)           # 24 h
    list_date_shallow_pages: int = _env_int("LIST_DATE_SHALLOW_PAGES", 5)
    list_date_deep_pages: int = _env_int("LIST_DATE_DEEP_PAGES", 15)
    list_date_deep_sec: int = _env_int("LIST_DATE_DEEP_SEC", 86400)         # deep crawl daily
    list_delay_sec: float = _env_float("LIST_DELAY_SEC", 1.0)
    # Sleep after a bucket-skip. Short (1s) matches BOT 0 — the bucket is
    # the throttle, not this sleep.
    list_skip_sleep_sec: float = _env_float("LIST_SKIP_SLEEP_SEC", 1.0)

    # v1.18: jittered inter-attempt pacing for the list sweep. Previously a
    # deterministic ~2s cadence (list_delay_sec + fetch time) produced a
    # metronome pattern that nhentai rate-limits easily, and on
    # bucket-exhausted we immediately moved to the next key with only a 1s
    # pause — burning attempts while the shared bucket was still dry.
    # Now: a random sleep in [LIST_INTER_ATTEMPT_MIN_SEC, MAX] between
    # scrape attempts, a longer pause on bucket exhaustion so the shared
    # token bucket actually gets to refill, and honoring nhentai's
    # Retry-After on 429s (capped at LIST_429_SLEEP_CAP_SEC).
    list_inter_attempt_min_sec: float = _env_float("LIST_INTER_ATTEMPT_MIN_SEC", 3.0)
    list_inter_attempt_max_sec: float = _env_float("LIST_INTER_ATTEMPT_MAX_SEC", 6.0)
    list_bucket_skip_wait_sec: float = _env_float("LIST_BUCKET_SKIP_WAIT_SEC", 8.0)
    list_429_sleep_cap_sec: float = _env_float("LIST_429_SLEEP_CAP_SEC", 300.0)

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
    # v1.14: BOT 1 self-caps the /search bucket to leave headroom for BOT 0.
    # Both bots now share the same Turso `nhentai_ratelimit` row; BOT 0
    # (user-facing) uses the full 10/min anon limit, so BOT 1 (background
    # scraper) deliberately under-consumes so user requests always win.
    # Default 8/min = 10/min limit minus a 2-token reserve for BOT 0.
    bucket_search_scraper: int = _env_int("BUCKET_SEARCH_SCRAPER", 8)

    # v1.19: region-aware Turso token-bucket split.
    # BOT 0 (Oregon) and BOT 1 (now also deployable to Singapore) both
    # consume the SHARED Turso `nhentai_ratelimit` row keyed by bucket_id.
    # When BOT 1 runs on a DIFFERENT Render region (different egress IP),
    # sharing the same bucket row lets one side throttle the other from a
    # different IP — which defeats the point of the region move. Setting
    # BOT1_REGION (e.g. "ap-singapore") suffixes every bucket_id with
    # "_<region>" so BOT 1 spends from its OWN row
    # (e.g. bucket "search_ap-singapore") instead of the legacy "search".
    # EMPTY (default) = legacy behavior, byte-identical bucket ids, safe
    # to deploy to the existing Oregon service with no behavior change.
    # The INSERT OR IGNORE bootstrap auto-creates the new row on first use;
    # NO Turso schema migration is needed.
    region_suffix: str = os.getenv("BOT1_REGION", "").strip()

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO"

    # Manual per-tag sweeps (always included on top of trending tags).
    extra_tag_sorts: List[str] = field(default_factory=lambda: _env_csv(
        "EXTRA_TAG_SORTS", ["incest"]))

    # Trending-tag auto-discovery — scrapes nhentai.net/tags/popular HTML
    # once every trending_tags_refresh_sec to pick up the current top N.
    # v1.24 (2026-08-30): English-only enforcement for BOT 1 scraping.
    # When true (default), hf_scraper_lite appends "language:english" to
    # every non-empty /search query (tag pages + typed queries), and
    # trending_tags harvests only English-tagged galleries. Set
    # ENGLISH_ONLY=0 to revert to the pre-v1.24 all-languages behavior.
    english_only: bool = _env_bool("ENGLISH_ONLY", True)
    trending_tags_enabled: bool = _env_bool("TRENDING_TAGS_ENABLED", True)
    trending_tags_top_n: int   = _env_int("TRENDING_TAGS_TOP_N", 10)
    trending_tags_refresh_sec: int = _env_int("TRENDING_TAGS_REFRESH_SEC", 24 * 3600)

    # Live channel dashboard
    log_channel_id: str = os.getenv("BOT1_LOG_CHANNEL_ID", "-1003796521529").strip()
    # v1.22.8: default 5s → 15s. The dashboard was doing a Mongo stats read
    # + Telegram editMessageText every 5s for the process lifetime — 3x the
    # churn for zero visible benefit. Still snappy for the log channel, and
    # /time <n> can override at runtime as before.
    channel_refresh_sec: int = _env_int("BOT1_CHANNEL_REFRESH_SEC", 15)

    # v1.25: daily admin digest — at DIGEST_TIME_IST (default 10:00 IST)
    # every day, broadcast to BOT1_ADMIN_USER_IDS: per sort/tag, per page,
    # how many NEW galleries were fetched in the last 24h. Pure
    # observability — zero scraping cost. Set DIGEST_ENABLED=0 to mute.
    digest_enabled: bool = _env_bool("BOT1_DIGEST_ENABLED", True)
    digest_time_ist: str = (os.getenv("BOT1_DIGEST_TIME_IST", "10:00").strip()
                            or "10:00")

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
