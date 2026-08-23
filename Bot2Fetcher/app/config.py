"""
config.py — Bot2Fetcher env loader.

Mirrors repo-root config.py conventions: short env aliases, secrets never
echoed, .env optional (real env wins). All knobs have safe defaults so the
service boots with only the required five groups set.

Required on Render:
    API_ID  API_HASH            Telegram app creds (same app the session
                                strings were generated with)
    STRING_SESSION              userbot session #1 (DMs @Gallery_DLBot)
    MONGO_URI                   Bot 0's Mongo (relaybot db) — read/claim/
                                complete the SAME `galleries` collection
    TURSO_DATABASE_URL          shared cache DB (turso:// or https:// both OK)
    TURSO_AUTH_TOKEN
    DB_CHANNEL_ID               private DB channel (same value Bot 0 uses)

Optional:
    STRING_SESSION_2            userbot session #2 (second parallel fetcher)
    BOT2_USERNAME               default @Gallery_DLBot
    LOG_CHANNEL_ID              Telegram log channel for live dashboard msg
    MONGO_DB_NAME               default relaybot
    FETCH_GAP_MIN_S / MAX_S     pause between jobs per slot (default 20/60)
    STALE_PROCESSING_S          re-claim window for stuck PROCESSING (900)
    RESCAN_SLEEP_S              sleep after a full Turso scan (300)
    PORT                        injected by Render automatically
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except Exception:  # dotenv optional
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
    sessions: tuple           # 1..N string sessions
    mongo_uri: str
    mongo_db: str
    turso_url: str
    turso_token: str
    db_channel_id: str        # kept raw; resolved to int at connect time
    bot2_username: str
    log_channel_id: str
    fetch_gap_min: int
    fetch_gap_max: int
    stale_processing_s: int
    rescan_sleep_s: int
    port: int

    def __repr__(self) -> str:  # never echo secrets
        return (f"Settings(api_id={self.api_id}, sessions={len(self.sessions)}, "
                f"db_channel={self.db_channel_id}, bot2={self.bot2_username!r})")


def load() -> Settings:
    sessions = []
    for name in ("STRING_SESSION", "STRING_SESSION_2"):
        v = _first(name)
        if v:
            sessions.append(v)
    if not sessions:
        raise RuntimeError("Missing required env var: STRING_SESSION")
    return Settings(
        api_id=int(_req("API_ID", "TELEGRAM_API_ID")),
        api_hash=_req("API_HASH", "TELEGRAM_API_HASH"),
        sessions=tuple(sessions),
        mongo_uri=_req("MONGO_URI", "MONGODB_URI"),
        mongo_db=_first("MONGO_DB_NAME") or "relaybot",
        turso_url=_req("TURSO_DATABASE_URL"),
        turso_token=_req("TURSO_AUTH_TOKEN"),
        db_channel_id=_req("DB_CHANNEL_ID", "DATABASE_CHANNEL_ID"),
        bot2_username=_first("BOT2_USERNAME") or "Gallery_DLBot",
        log_channel_id=_first("LOG_CHANNEL_ID"),
        fetch_gap_min=_int("FETCH_GAP_MIN_S", 20),
        fetch_gap_max=_int("FETCH_GAP_MAX_S", 60),
        stale_processing_s=_int("STALE_PROCESSING_S", 900),
        rescan_sleep_s=_int("RESCAN_SLEEP_S", 300),
        port=_int("PORT", 8080),
    )
