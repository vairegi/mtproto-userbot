"""app.turso_cache — shared canonical Turso payload layer (v12.47).
Pure functions, zero I/O; transports stay per-bot.

v12.48 (sync-audit) F7: the three copies of this file live at
  * common/turso_cache/__init__.py           (repo-root deploy: BOT 0)
  * ScraperBot/app/turso_cache/__init__.py   (subtree deploy: BOT 1)
  * Bot2Fetcher/app/turso_cache/__init__.py  (subtree deploy: BOT 2)
They are re-exports only — no runtime state, no code that could drift.
Only the docstring's leading package name differs (common. vs app.) so
that each import path reads correctly under its own deploy root; the
imported symbols and __version__ are byte-identical. tests/
test_turso_cache.py enforces byte-parity on normalize.py and writer.py.
"""
from .normalize import (  # noqa: F401
    normalize_for_write, normalize_gallery_payload, normalize_search_payload,
    coerce_int, coerce_str, construct_cover_url,
)
from .writer import build_upsert_sql, build_update_payload_sql  # noqa: F401

__version__ = "12.47"
