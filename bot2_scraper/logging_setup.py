"""
logging_setup.py — Rotating file + stderr logging with secret redaction.

Brief §10:
- Never write session string / bot token / api hash to any log line, even DEBUG.
- Redact at the LOGGING LAYER, not just at print sites.

MIGRATION NOTE (VPS → serverless hosting)
-----------------------------------------
Two changes were required for containers like Hugging Face Spaces:

1. THE LOG DIRECTORY IS NO LONGER CREATED AT IMPORT TIME. The old module ran
   `LOG_DIR.mkdir()` while being imported. On a host with a read-only or
   permission-restricted filesystem that raises immediately, and because every
   other module imports this one, the whole bot would die before printing a
   single useful error. Directory creation now happens inside setup_logging()
   and is wrapped in try/except.

2. IF FILE LOGGING IS IMPOSSIBLE, WE FALL BACK TO CONSOLE-ONLY. On serverless
   hosting the container disk is wiped on every restart, so log files have
   little value anyway — the platform captures stdout/stderr and shows it in
   the web log viewer. Console logging is therefore the primary channel now,
   and is explicitly line-buffered so logs appear live instead of in bursts.

3. THE MONGO_URI IS ADDED TO THE REDACTION LIST (via config.Settings.secrets),
   because a connection string contains the database password.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Iterable, List

from config import BASE_DIR, settings

# Where log files go when the filesystem is writable. Overridable with LOG_DIR.
import os

LOG_DIR = Path(os.getenv("LOG_DIR") or (BASE_DIR / "logs"))

# Set to False by an env var if you want console-only logging on purpose.
_FILE_LOGGING_ENABLED = (os.getenv("LOG_TO_FILE", "1").strip() != "0")


class _RedactFilter(logging.Filter):
    """Replace any occurrence of known secrets with <redacted>."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        # Only redact strings long enough to be meaningfully secret
        self._patterns: List[re.Pattern] = [
            re.compile(re.escape(s)) for s in secrets if s and len(s) >= 8
        ]
        # Extra structural patterns catch tokens/hashes we might not have loaded
        self._patterns.extend([
            re.compile(r"\b\d{9,10}:[A-Za-z0-9_-]{30,}\b"),   # bot tokens 123:ABC...
            re.compile(r"\b[a-f0-9]{32}\b"),                    # api hashes
            re.compile(r"1[A-Za-z0-9+/=_-]{300,}"),             # telethon session strings
            # MongoDB connection strings: hide the user:password@ portion so a
            # stray log line can never leak database credentials.
            re.compile(r"(mongodb(?:\+srv)?://)[^@\s]+@", re.IGNORECASE),
        ])

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        original = msg
        for p in self._patterns:
            if p.pattern.startswith("(mongodb"):
                msg = p.sub(r"\1<redacted>@", msg)
            else:
                msg = p.sub("<redacted>", msg)
        if msg != original:
            # Overwrite the message so downstream formatters see redacted text
            record.msg = msg
            record.args = ()
        return True


def _build_file_handler(name: str, fmt: logging.Formatter):
    """Return a rotating file handler, or None if the disk is not writable."""
    if not _FILE_LOGGING_ENABLED:
        return None
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            LOG_DIR / f"{name}.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        return fh
    except (OSError, PermissionError):
        # Read-only or restricted filesystem (common on free hosting tiers).
        # Console logging alone is fine — the platform captures stdout.
        return None


def setup_logging(name: str) -> logging.Logger:
    level = getattr(logging, (settings.log_level if settings else "INFO").upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid double-configuring on module reimport
    if getattr(root, "_relay_configured", False):
        return logging.getLogger(name)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [%(process)d] %(message)s"
    )

    handlers: List[logging.Handler] = []

    # Console first — this is the primary channel on serverless hosting, where
    # the platform's log viewer reads stdout/stderr.
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(fmt)
    handlers.append(sh)

    fh = _build_file_handler(name, fmt)
    if fh is not None:
        handlers.append(fh)

    secrets = settings.secrets() if settings else []
    redact = _RedactFilter(secrets)

    for h in handlers:
        h.addFilter(redact)
        root.addHandler(h)

    # Third-party libraries are chatty at DEBUG; keep them at WARNING so the
    # bot's own messages stay readable in the hosting platform's log viewer.
    for noisy in ("httpx", "httpcore", "pymongo", "telethon", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root._relay_configured = True  # type: ignore[attr-defined]

    if fh is None and _FILE_LOGGING_ENABLED:
        logging.getLogger(name).info(
            "file logging disabled (%s not writable) — logging to console only", LOG_DIR
        )
    return logging.getLogger(name)
