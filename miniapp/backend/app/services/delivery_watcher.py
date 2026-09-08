"""
delivery_watcher.py — v12.87: Bot 0 auto-DM on Bot 2 completion.

The missing last mile of the user-download flow. When a mini-app user taps
Download on a gallery that is not yet in the DB channel, Bot 0 writes a
'pending' row into the `queue` ledger and Bot 2 picks it up, fetches the
PDF, posts cover+PDF into the DB channel and flips the ledger row to
'completed' (mongo_state.mark_queue_status). Before v12.87, NOTHING
delivered the result: the live progress card silently finished and the
user had to tap Download a second time (dedup path) to actually receive
the PDF in their DM.

This loop closes the gap — every POLL tick:

  1. Read up to BATCH_LIMIT `queue` rows with status='completed',
     delivered != True, updated_at >= boot watermark (oldest first, FIFO).
     The boot watermark (now - LOOKBACK_S, default 1h) means pre-v12.87
     history (hundreds of old completed rows) is NEVER retro-spammed; only
     completions from the last hour — which covers redeploy windows — are
     eligible.
  2. Require the galleries doc to be COMPLETED/PARTIAL with BOTH
     db_cover_msg_id and db_pdf_msg_id present. This defends against a
     racy ledger flip landing before Bot 2's mark_completed write.
  3. DM cover+PDF to queue.submitted_by via the EXISTING
     dm_delivery.deliver_to_dm() — force-join gate, /usebackupDB toggle,
     share-guard protect_content and the auto-delete scheduler all apply
     unchanged, exactly as if the user had tapped Download on a cached
     gallery.
  4. Ledger stamping (delivered field):
       True         — DM sent, done.
       "force_join" — user is gated; force_join.remember_pending already
                      armed the 'I've joined' callback, which re-triggers
                      deliver_to_dm itself. Watcher stops polling the row.
       "failed"     — permanent failure (user never /start'd the bot, bot
                      blocked, or the galleries doc never became
                      deliverable within MAX_WAIT_S). Carries
                      delivery_error for admin triage. Never retried.
       (untouched)  — transient error; retried next tick until MAX_WAIT_S
                      (default 6h) since the row's completion, then
                      auto-stamps "failed" so nothing spins forever.

Admin-submitted rows (auto-queue, /fetch) are stamped "skipped" — admins
read the channel, not their DM. Rows with no requester are "skipped" too.

All Mongo work runs via asyncio.to_thread so the FastAPI event loop is
never blocked (same contract as the rest of the mini-app: db.connect()
is a cheap handle over the shared pool, close() is a no-op). RAM cost:
one small indexed find per tick — negligible on the 512 MB budget.

Env knobs (all optional):
  DELIVERY_WATCHER_INTERVAL_S   — poll cadence seconds (default 10)
  DELIVERY_WATCHER_BATCH        — rows per tick (default 20)
  DELIVERY_WATCHER_LOOKBACK_S   — boot watermark lookback (default 3600)
  DELIVERY_WATCHER_MAX_WAIT_S   — give-up window per row (default 21600)
  DELIVERY_WATCHER_OFF=1        — hard disable (rollback switch)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, Optional

from ..config import settings

log = logging.getLogger("miniapp.delivery_watcher")

# --- Repo-root bot DB + gallery_state, same loader pattern as dm_delivery ---
try:
    try:
        from ..rootdb import load as _lrd
    except ImportError:  # services imported as top-level package
        from rootdb import load as _lrd
    _bot_db = _lrd()
    import gallery_state as _gs  # type: ignore  # noqa
    HAVE_BOT = _bot_db is not None
except Exception as e:  # noqa: BLE001
    _bot_db = None
    _gs = None
    HAVE_BOT = False
    log.warning("delivery_watcher: bot db/gallery_state unavailable — disabled (%s)", e)


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "") or 0)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


_POLL_S = _env_int("DELIVERY_WATCHER_INTERVAL_S", 10)
_BATCH = _env_int("DELIVERY_WATCHER_BATCH", 20)
_MAX_WAIT_S = _env_int("DELIVERY_WATCHER_MAX_WAIT_S", 21600)   # 6h
_OFF = os.environ.get("DELIVERY_WATCHER_OFF", "0").strip() in ("1", "true", "yes")

# Boot watermark: only completions newer than (boot - lookback) are
# eligible. Protects existing users from a retro-storm of old completed
# rows on first deploy, while still covering completions that landed
# during a short Bot 0 redeploy.
_BOOT_TS = time.time() - _env_int("DELIVERY_WATCHER_LOOKBACK_S", 3600)

_GID_RE = re.compile(r"/g/(\d+)")

# dm_delivery is imported lazily inside the tick so a broken import there
# can never kill the watcher at module load.
_dm = None


def _dm_delivery():
    global _dm
    if _dm is None:
        from . import dm_delivery as _mod
        _dm = _mod
    return _dm


def _admin_id() -> int:
    try:
        return int(getattr(settings, "admin_user_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _epoch(v: Any) -> float:
    try:
        import datetime as _dt
        if isinstance(v, _dt.datetime):
            return v.timestamp()
        return float(v or 0)
    except Exception:
        return 0.0


def _mark(row: Dict[str, Any], value: Any, error: str = "") -> None:
    """Stamp the ledger row. Best-effort; a failed stamp just means the row
    is re-evaluated next tick (deliver_to_dm itself is idempotent-safe:
    copyMessage of the same ids is cheap and the user sees a duplicate at
    worst — but Mongo blips are rare and short)."""
    try:
        conn = _bot_db.connect()
        try:
            upd: Dict[str, Any] = {
                "delivered": value,
                "delivered_at": time.time(),
                "delivery_source": "delivery_watcher",
            }
            if error:
                upd["delivery_error"] = str(error)[:200]
            conn.queue.update_one({"_id": row["_id"]}, {"$set": upd})
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        log.warning("delivery_watcher: stamp failed for row %s: %s",
                    row.get("_id"), e)


def _process_row(row: Dict[str, Any]) -> int:
    """Handle one completed ledger row. Returns 1 if a DM was sent."""
    gid = str(row.get("gallery_id") or "").strip()
    if not gid:
        m = _GID_RE.search(str(row.get("url") or ""))
        gid = m.group(1) if m else ""
    if not gid:
        _mark(row, "skipped", "no gallery id on queue row")
        return 0

    try:
        uid = int(row.get("submitted_by") or 0)
    except (TypeError, ValueError):
        uid = 0
    if uid <= 0:
        _mark(row, "skipped", "no requester")
        return 0
    if _admin_id() and uid == _admin_id():
        _mark(row, "skipped", "admin request")
        return 0

    # Require the galleries doc to be fully deliverable before DMing.
    conn = _bot_db.connect()
    try:
        doc = _gs.get(conn, gid) or {}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    status = str(doc.get("status") or "").upper()
    deliverable = (
        status in ("COMPLETED", "PARTIAL")
        and doc.get("db_cover_msg_id")
        and doc.get("db_pdf_msg_id")
    )
    if not deliverable:
        # Ledger flipped before the galleries doc caught up (race), or the
        # completion is a PARTIAL without a PDF. Retry until MAX_WAIT_S.
        if time.time() - _epoch(row.get("updated_at")) > _MAX_WAIT_S:
            _mark(row, "failed",
                  f"galleries doc never became deliverable (status={status})")
        return 0

    res = _dm_delivery().deliver_to_dm(gid, uid)
    if res.get("delivered"):
        _mark(row, True)
        log.info("📨 delivery_watcher: #%s auto-DM'd to user %s", gid, uid)
        return 1

    if res.get("blocked_by_force_join"):
        # force_join.remember_pending already armed the 'I've joined'
        # callback which re-triggers deliver_to_dm — stop polling this row.
        _mark(row, "force_join")
        log.info("🔒 delivery_watcher: #%s gated by force-join for user %s",
                 gid, uid)
        return 0

    reason = str(res.get("reason") or res.get("delivery_error") or "")
    permanent = ("initiate" in reason or "Forbidden" in reason
                 or "bot was blocked" in reason or "chat not found" in reason)
    if permanent:
        _mark(row, "failed", reason or "telegram refused DM")
        log.warning("delivery_watcher: #%s permanent DM failure for %s: %s",
                    gid, uid, reason[:120])
    elif time.time() - _epoch(row.get("updated_at")) > _MAX_WAIT_S:
        _mark(row, "failed", reason or "delivery failed (max wait exceeded)")
        log.warning("delivery_watcher: #%s gave up on user %s after %ds: %s",
                    gid, uid, _MAX_WAIT_S, reason[:120])
    # else: transient — leave unmarked, retried next tick.
    return 0


def _tick() -> int:
    """One poll pass. Sync; runs in a worker thread via asyncio.to_thread."""
    if not HAVE_BOT:
        return 0
    conn = _bot_db.connect()
    try:
        rows = list(conn.queue.find(
            {"status": "completed",
             "delivered": {"$ne": True},
             "updated_at": {"$gte": _BOOT_TS}},
            {"url": 1, "gallery_id": 1, "submitted_by": 1,
             "username": 1, "updated_at": 1},
        ).sort("_id", 1).limit(_BATCH))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    sent = 0
    for row in rows:
        try:
            if _process_row(row):
                sent += 1
                time.sleep(1.0)   # gentle Bot API pacing between users
        except Exception as e:  # noqa: BLE001
            log.warning("delivery_watcher: row %s crashed (non-fatal): %s",
                        row.get("_id"), e)
    return sent


async def _run_forever() -> None:
    log.info("📨 delivery_watcher loop starting (interval=%ss batch=%s "
             "lookback watermark=%d)", _POLL_S, _BATCH, int(_BOOT_TS))
    while True:
        try:
            n = await asyncio.to_thread(_tick)
            if n:
                log.info("📨 delivery_watcher: auto-delivered %d queued "
                         "download(s) this tick", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # The loop must NEVER die — a crash here would silently disable
            # all auto-delivery until the next redeploy.
            log.warning("delivery_watcher tick failed (non-fatal): %s", e)
        await asyncio.sleep(_POLL_S)


_task: Optional[asyncio.Task] = None


def start_background_loop() -> None:
    """Idempotent: schedule the loop on the running event loop if not
    already running. Same contract as deletion_scheduler.start_background_loop.
    """
    global _task
    if _OFF:
        log.warning("delivery_watcher disabled via DELIVERY_WATCHER_OFF=1")
        return
    if not HAVE_BOT:
        log.warning("delivery_watcher not started — bot db unavailable")
        return
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _task = loop.create_task(_run_forever())
