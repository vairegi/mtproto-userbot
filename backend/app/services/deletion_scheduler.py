"""
deletion_scheduler.py — Auto-delete DM'd content after N hours.

When a user receives a cover + PDF via the admin bot's copyMessage, we
record `{chat_id, message_id, delete_at}` into `miniapp_scheduled_deletes`.
A background loop polls due rows every 60s and calls Bot API
`deleteMessage` to wipe them.

Admin controls (see routes/admin.py):
  - `auto_delete_enabled` (bool)
  - `auto_delete_hours`   (int, default 24)

`schedule(chat_id, message_ids)` is safe to call from any request handler
— it's a tiny Mongo insert.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Iterable, Optional

import httpx

from .. import db as _db
from ..config import settings

log = logging.getLogger("miniapp.deletion")

_TG_API = "https://api.telegram.org"
_LOOP_INTERVAL_SEC = 60
_HTTP_TIMEOUT = 10.0


def _col():
    return _db.db()["miniapp_scheduled_deletes"]


def _bot_token() -> str:
    return (
        settings.bot_token
        or os.environ.get("BOT_TOKEN", "")
        or os.environ.get("ADMIN_BOT_TOKEN", "")
    )


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------
def is_enabled() -> bool:
    return bool(_db.get_setting("auto_delete_enabled", False))


def hours() -> int:
    try:
        h = int(_db.get_setting("auto_delete_hours", 24) or 24)
    except (TypeError, ValueError):
        h = 24
    return max(1, h)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def schedule(chat_id: int, message_ids: Iterable[int]) -> int:
    """Schedule deletion for the given messages. No-op when auto-delete is
    disabled globally. Returns the number of rows inserted."""
    if not is_enabled():
        return 0
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return 0
    ids = [int(m) for m in (message_ids or []) if m]
    if not chat_id or not ids:
        return 0

    delete_at = time.time() + (hours() * 3600.0)
    docs = [
        {"chat_id": chat_id, "message_id": mid,
         "created_at": time.time(), "delete_at": delete_at}
        for mid in ids
    ]
    try:
        _col().insert_many(docs, ordered=False)
        return len(docs)
    except Exception as e:  # noqa: BLE001
        log.warning("deletion_scheduler.schedule failed (%s)", e)
        return 0


async def _delete_one(client: httpx.AsyncClient, token: str,
                      chat_id: int, message_id: int) -> bool:
    try:
        r = await client.post(
            f"{_TG_API}/bot{token}/deleteMessage",
            json={"chat_id": int(chat_id), "message_id": int(message_id)},
            timeout=_HTTP_TIMEOUT,
        )
        data = r.json() or {}
        if data.get("ok"):
            return True
        # Already-deleted messages → treat as success (idempotent).
        desc = str(data.get("description") or "").lower()
        if "not found" in desc or "message can't be deleted" in desc:
            return True
        log.info("deleteMessage refused chat=%s msg=%s: %s",
                 chat_id, message_id, desc)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("deleteMessage error chat=%s msg=%s: %s",
                    chat_id, message_id, e)
        return False


async def _tick(client: httpx.AsyncClient, token: str) -> int:
    """Process all due rows once. Returns the number deleted."""
    now = time.time()
    rows = list(_col().find({"delete_at": {"$lte": now}}).limit(500))
    if not rows:
        return 0

    deleted = 0
    for row in rows:
        ok = await _delete_one(client, token,
                               int(row["chat_id"]), int(row["message_id"]))
        if ok:
            try:
                _col().delete_one({"_id": row["_id"]})
                deleted += 1
            except Exception:
                pass
        else:
            # Reschedule 15 min later to avoid a tight retry loop.
            try:
                _col().update_one(
                    {"_id": row["_id"]},
                    {"$set": {"delete_at": now + 900}},
                )
            except Exception:
                pass
    if deleted:
        log.info("deletion_scheduler: deleted %d messages", deleted)
    return deleted


async def _run_forever() -> None:
    log.info("deletion_scheduler loop starting (interval=%ss)", _LOOP_INTERVAL_SEC)
    while True:
        token = _bot_token()
        if token:
            try:
                async with httpx.AsyncClient() as c:
                    await _tick(c, token)
            except Exception as e:  # noqa: BLE001
                log.warning("deletion_scheduler tick failed: %s", e)
        await asyncio.sleep(_LOOP_INTERVAL_SEC)


_task: Optional[asyncio.Task] = None


def start_background_loop() -> None:
    """Idempotent: schedule the loop on the running event loop if not running."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _task = loop.create_task(_run_forever())
