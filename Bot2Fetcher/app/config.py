"""
config.py — Bot2Fetcher env loader.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except Exception:
    pass


def _first(*names: str) -> str:
    for n in names:
        v = os.getenv(n)
        if v and v.strip():
            return v.strip()
    return ""


def _req(*names: str) -> str:
    v = _first(*names)
    if not v:
        raise RuntimeError(f"Missing required env var: {names[0]}")
    return v


def _int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    sessions: tuple
    mongo_uri: str
    mongo_db: str
    turso_url: str
    turso_token: str
    db_channel_id: str
    bot2_username: str
    # v12.49: fallback downloader bot chain (@pdfdownloadcinbot).
    # Empty fallback_username = feature fully inert (pre-v12.49 behavior).
    fallback_username: str
    fallback_timeout_s: int
    both_fail_park_s: int
    fallback_lease_s: int
    log_channel_id: str
    log_bot_token: str
    fetch_gap_min: int
    fetch_gap_max: int
    retry_cooldown_s: int   # v12.88: transient-failure cooldown (replaces the
                          # permanent _known_failed blackhole on drop paths)
    stale_processing_s: int
    rescan_sleep_s: int
    scan_max_galleries: int
    log_summary_hours: int
    port: int

    def __repr__(self) -> str:
        return (f"Settings(api_id={self.api_id}, sessions={len(self.sessions)}, "
                f"db_channel={self.db_channel_id}, bot2={self.bot2_username!r})")


def load() -> Settings:
    # v13.10: workers (Telethon sessions) are MONGO-BACKED (app/workers.py)
    # and hot-reloaded by the Fetcher — managed live via the admin commands
    # /addworker /listworker /removeworker /changeapiandhash (BOT2_TOKEN).
    # Bot 2 must BOOT WITH ZERO WORKERS (standby), never crash on a missing
    # session. STRING_SESSION* env vars are NO LONGER READ (Mongo is the
    # single source). settings.sessions stays empty at boot; the fetcher's
    # reconcile loop populates real slots from Mongo.
    sessions: list = []
    import logging as _lg
    _lg.getLogger("bot2fetcher").info(
        "📱 Workers: Mongo-backed (v13.10) — standby until /addworker")
    return Settings(
        # v13.10: api creds are optional in env now — the Mongo worker store
        # is authoritative (set via /addworker or /changeapiandhash). Env
        # values (if present) act only as a bootstrap fallback.
        api_id=int(_first("API_ID", "TELEGRAM_API_ID") or 0),
        api_hash=_first("API_HASH", "TELEGRAM_API_HASH"),
        sessions=tuple(sessions),
        mongo_uri=_req("MONGO_URI", "MONGODB_URI"),
        mongo_db=_first("MONGO_DB_NAME") or "relaybot",
        turso_url=_req("TURSO_DATABASE_URL"),
        turso_token=_req("TURSO_AUTH_TOKEN"),
        db_channel_id=_req("DB_CHANNEL_ID", "DATABASE_CHANNEL_ID"),
        bot2_username=(_first("PDF_DOWNLOADER_USERNAME", "BOT2_USERNAME")
                       or "Gallery_DLBot").lstrip("@"),
        # v12.49: fallback bot. Set BACKUP_PDF_USERNAME=pdfdownloadcinbot.
        fallback_username=(_first("BACKUP_PDF_USERNAME") or "").lstrip("@"),
        fallback_timeout_s=_int("BACKUP_PDF_TIMEOUT_S", 300),   # 5 min
        both_fail_park_s=_int("BOTH_FAIL_PARK_S", 43200),       # 12 h
        fallback_lease_s=_int("FALLBACK_LEASE_S", 420),         # > timeout
        log_channel_id=_first("LOG_CHANNEL_ID"),
        # v12.40k: preferred log-channel transport is a Bot API bot.
        # Env var name per operator: BOT_2_PDF_FECTHER (kept verbatim).
        # Aliases also accepted so a rename never breaks the deploy.
        log_bot_token=_first("BOT_2_PDF_FECTHER", "LOG_BOT_TOKEN",
                             "DASHBOARD_BOT_TOKEN", "TELEGRAM_LOG_BOT_TOKEN"),
        fetch_gap_min=_int("FETCH_GAP_MIN_S", 3),
        fetch_gap_max=_int("FETCH_GAP_MAX_S", 8),
        # v12.88: how long a transient drop_claim failure (primary timeout,
        # unusable meta) keeps a gid out of the scan before auto-retry.
        retry_cooldown_s=_int("RETRY_COOLDOWN_S", 1800),          # 30 min
        stale_processing_s=_int("STALE_PROCESSING_S", 900),
        rescan_sleep_s=_int("RESCAN_SLEEP_S", 300),
        scan_max_galleries=_int("SCAN_MAX_GALLERIES", 2000),
        log_summary_hours=_int("LOG_SUMMARY_HOURS", 5),
        port=_int("PORT", 8080),
    )
