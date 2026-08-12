"""
config.py — Loads .env / environment variables and exposes typed settings.

Rules from Brief §10:
- Never echo secrets. `__repr__` on Settings redacts them.
- Redaction list is used by logging_setup.py as well.

MIGRATION NOTES (SQLite → MongoDB, VPS → serverless hosting)
------------------------------------------------------------
1. ADDED `mongo_uri` — read from the MONGO_URI environment variable. This is
   the MongoDB Atlas connection string that replaces the old local queue.db.

2. ADDED SHORT ENV-VAR ALIASES. On Hugging Face Spaces / Render you type
   variable names by hand into a web form, so the short names are far easier:

       API_ID           (or the original TELEGRAM_API_ID)
       API_HASH         (or TELEGRAM_API_HASH)
       STRING_SESSION   (or TELEGRAM_SESSION_STRING)
       BOT_TOKEN        (or ADMIN_BOT_TOKEN)
       MONGO_URI

   Either spelling works. If BOTH are set, the short name wins.

3. TELEGRAM_PHONE IS NOW OPTIONAL. It was only ever needed for interactive
   login; because we authenticate with a StringSession, a missing phone number
   must not stop the bot from booting on a server with no keyboard attached.

4. `.env` is still loaded if present (handy for local testing) but is no
   longer required — real environment variables take priority, which is
   exactly how hosting platforms inject secrets.

5. `BOT1_USERNAME` IS DEPRECATED (V2 architecture, docs/ARCHITECTURE_V2.md).
   Bot 1 was removed in favour of an in-house cover poster; the value is
   still accepted so V2 rollback via `SELF_COVER_POST_ENABLED=0` continues
   to work, but it is no longer required. A `DeprecationWarning` is logged
   at startup if the variable is set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Load .env if present, but NEVER let it override real environment variables
# (override=False). On Hugging Face / Render the platform-injected variables
# must always win.
load_dotenv(ENV_PATH, override=False)


def _first_env(*names: str) -> str:
    """Return the first non-empty value among several env var names.

    Lets one setting be supplied under either a short name (API_ID) or the
    project's original long name (TELEGRAM_API_ID).
    """
    for n in names:
        v = os.getenv(n)
        if v is None:
            continue
        v = v.strip()
        if v and not v.startswith("<REDACTED"):
            return v
    return ""


def _req(*names: str) -> str:
    """Required setting: raise a clear, actionable error if it is missing."""
    v = _first_env(*names)
    if not v:
        primary = names[0]
        alts = ("  (aliases accepted: " + ", ".join(names[1:]) + ")") if len(names) > 1 else ""
        raise RuntimeError(
            f"Missing required environment variable: {primary}{alts}\n"
            f"Set it in your hosting platform's Secrets/Variables panel, "
            f"or in {ENV_PATH} for local runs."
        )
    return v


def _opt(*names: str, default: str = "") -> str:
    v = _first_env(*names)
    return v if v else default


def _int_req(*names: str) -> int:
    raw = _req(*names)
    try:
        return int(raw)
    except (TypeError, ValueError) as e:
        raise RuntimeError(
            f"Environment variable {names[0]} must be a whole number, got {raw!r}"
        ) from e


def _int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # Userbot
    api_id: int
    api_hash: str
    phone: str
    session_string: str

    # Admin bot
    admin_bot_token: str
    admin_user_id: int

    # Telegram targets
    bot1_username: str
    bot2_username: str
    database_channel_id: int
    doujinshibot_username: str

    # Database (NEW — replaces the local SQLite file)
    mongo_uri: str
    mongo_db_name: str

    # Fallback API (may be empty)
    source_api_base: str
    source_api_key: str

    # Runtime
    timezone: str
    log_level: str
    inter_job_delay_min: int
    inter_job_delay_max: int
    bot1_post_timeout_sec: int
    bot2_pdf_timeout_sec: int
    bot2_retry_count: int
    batch_max_links: int
    auto_fetch_domains: tuple

    # Paths
    base_dir: Path = field(default=BASE_DIR)

    # ---- Secret handling ----
    _SECRET_FIELDS = (
        "api_hash",
        "session_string",
        "admin_bot_token",
        "source_api_key",
        "mongo_uri",          # contains the DB password — must be redacted
    )

    def secrets(self) -> List[str]:
        """Return the raw secret values (used by log redaction)."""
        out: List[str] = []
        for f in self._SECRET_FIELDS:
            v = getattr(self, f, "")
            if v:
                out.append(str(v))
        return out

    def __repr__(self) -> str:  # never print secrets
        parts = []
        for f in self.__dataclass_fields__:  # type: ignore[attr-defined]
            v = getattr(self, f)
            if f in self._SECRET_FIELDS and v:
                v = "<redacted>"
            parts.append(f"{f}={v!r}")
        return "Settings(" + ", ".join(parts) + ")"


def _domains(name: str, default: str) -> tuple:
    """Parse a comma-separated list of hostnames into a lowercased tuple."""
    raw = _opt(name, default=default) or default
    out = []
    for chunk in raw.split(","):
        h = chunk.strip().lower().lstrip("@")
        if "://" in h:
            h = h.split("://", 1)[1]
        if "/" in h:
            h = h.split("/", 1)[0]
        if h:
            out.append(h)
    return tuple(out)


def load_settings() -> Settings:
    return Settings(
        # Short names first so they take priority over the long originals.
        api_id=_int_req("API_ID", "TELEGRAM_API_ID"),
        api_hash=_req("API_HASH", "TELEGRAM_API_HASH"),
        # Optional: StringSession auth means no phone number is needed to boot.
        phone=_opt("PHONE", "TELEGRAM_PHONE"),
        session_string=_req("STRING_SESSION", "TELEGRAM_SESSION_STRING", "SESSION_STRING"),
        admin_bot_token=_req("BOT_TOKEN", "ADMIN_BOT_TOKEN"),
        admin_user_id=_int_req("ADMIN_USER_ID", "ADMIN_ID", "OWNER_ID"),
        # DEPRECATED in V2 (docs/ARCHITECTURE_V2.md §7). Legacy relay.py
        # still references it, so we accept the value with an empty default
        # to keep the V1 code path importable when SELF_COVER_POST_ENABLED=0
        # is used for rollback. A warning is emitted at startup (see
        # _emit_bot1_deprecation_warning below).
        bot1_username=(_opt("BOT1_USERNAME") or "").lstrip("@"),
        bot2_username=_req("BOT2_USERNAME").lstrip("@"),
        database_channel_id=_int_req("DATABASE_CHANNEL_ID", "CHANNEL_ID"),
        # Correct spelling is Doug-in-shibot (with a 'g'), NOT Dou-jin-shibot.
        # The 'j' variant is a different bot; sending /mpost there does nothing.
        doujinshibot_username=(_opt("DOUJINSHIBOT_USERNAME", default="Douginshibot") or "").lstrip("@"),
        # NEW — MongoDB
        mongo_uri=_req("MONGO_URI", "MONGODB_URI"),
        mongo_db_name=_opt("MONGO_DB_NAME", default="relaybot") or "relaybot",
        source_api_base=_opt("SOURCE_API_BASE"),
        source_api_key=_opt("SOURCE_API_KEY"),
        timezone=_opt("TIMEZONE", default="UTC") or "UTC",
        log_level=_opt("LOG_LEVEL", default="INFO") or "INFO",
        inter_job_delay_min=_int("INTER_JOB_DELAY_MIN", 20),
        inter_job_delay_max=_int("INTER_JOB_DELAY_MAX", 60),
        bot1_post_timeout_sec=_int("BOT1_POST_TIMEOUT_SEC", 15),
        bot2_pdf_timeout_sec=_int("BOT2_PDF_TIMEOUT_SEC", 60),
        bot2_retry_count=_int("BOT2_RETRY_COUNT", 1),
        batch_max_links=_int("BATCH_MAX_LINKS", 25),
        auto_fetch_domains=_domains("AUTO_FETCH_DOMAINS", "hentaifox.com,nhentai.net"),

    )


# Convenience singleton (import-time is fine; process is short-lived per run)
try:
    settings = load_settings()
except Exception as _e:  # noqa: BLE001
    # Import-safe: consumers that need settings call load_settings() explicitly
    # and get a clear error. Tools like the startup self-test print the error.
    settings = None  # type: ignore[assignment]
    SETTINGS_ERROR: Optional[str] = str(_e)
else:
    SETTINGS_ERROR = None


# Make MONGO_URI visible to db.py even when it was supplied only under the
# MONGODB_URI alias or via the .env file. db.py reads os.environ directly so it
# stays importable without config.py.
if settings is not None and settings.mongo_uri:
    os.environ.setdefault("MONGO_URI", settings.mongo_uri)
    os.environ.setdefault("MONGO_DB_NAME", settings.mongo_db_name)


# ---------------------------------------------------------------------------
# V2 deprecation warnings (docs/ARCHITECTURE_V2.md §7)
# ---------------------------------------------------------------------------
# BOT1_USERNAME is no longer required. If the operator still has it set on
# their Render service we emit a one-line warning at import time so it's
# visible in the startup logs but doesn't break anything.
#
# We use logging directly (not warnings.warn) so the message shows up in the
# platform's log viewer where the operator will actually see it.
def _emit_bot1_deprecation_warning() -> None:
    v = os.environ.get("BOT1_USERNAME", "").strip()
    if not v:
        return
    self_cover = (os.environ.get("SELF_COVER_POST_ENABLED", "1") or "1").strip().lower()
    if self_cover in ("0", "false", "no", "off"):
        # V2 rollback in effect — the legacy path genuinely needs BOT1.
        return
    try:
        import logging as _logging
        _logging.getLogger("config").warning(
            "BOT1_USERNAME=%r is set but V2 no longer uses Bot 1. "
            "You can remove it from the environment; see "
            "docs/ARCHITECTURE_V2.md §7 and docs/MIGRATION_V2.md §5. "
            "To keep the legacy path, also set SELF_COVER_POST_ENABLED=0.",
            v,
        )
    except Exception:  # noqa: BLE001
        pass


_emit_bot1_deprecation_warning()
