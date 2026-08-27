"""
relay_v2.py — The V2 per-link flow (Bot 1 removed, Mongo dedup gate).

For ONE job:
  1) Dedup gate on `galleries` collection (keyed on gallery_id).
       COMPLETED  → tell the caller; no work performed here.
       PROCESSING → tell the caller; no work performed here.
       Fresh/retryable → claim a PROCESSING doc, proceed.
  2) Scrape gallery meta + post cover to the Database Channel ourselves
     (cover_poster.post_cover).
  3) DM URL to Bot 2 (@Gallery_DLBot).
  4) Wait for Bot 2's PDF reply in DM (bot2_client.wait_for_pdf).
  5) On PDF: forward the PDF into the Database Channel with drop_author,
     mark galleries[gid] COMPLETED, optionally fire /mpost.
  6) On Bot 2 error text: delete the cover post, purge the galleries doc,
     notify the admin, return the outcome for the user.
  7) On Bot 2 timeout: leave the cover post, tombstone the doc as
     FAILED_TIMEOUT so a manual retry can distinguish "never tried" from
     "tried and Bot 2 didn't answer".

Fallbacks:
  - hf_scraper.fetch_gallery_meta returns None → FAILED_SCRAPE tombstone
    and inform the user (no cover posted, no Bot 2 DM sent).
  - FloodWaitError (§7a preserved from V1): sleep e.seconds + 5, retry.

Rollback:
  worker.py chooses between `relay.process_job` and
  `relay_v2.process_job` based on `SELF_COVER_POST_ENABLED`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from telethon import TelegramClient
from telethon.errors import FloodWaitError

import db
import feature_flags
import gallery_state as gs
import cover_poster
import bot2_client
from config import settings
# v11.3: adaptive Bot 2 timeout that scales with page count.
# @Gallery_DLBot needs ~1.6s per page to build the PDF; 200-page
# galleries (~150 MB) were timing out at the flat 60/480s cap.
from pdf_wait_timing import (
    compute_pdf_timeout,
    describe_timeout,
    record_bot2_latency,   # v11.7: auto-tuning telemetry
)

log = logging.getLogger("relay_v2")


# ---------------------------------------------------------------------------
# v10: granular progress events for the mini-app live progress card.
# Writes one row per pipeline phase into `progress_events`; the mini-app's
# services/progress.py reads the newest row to render phase + pct + detail.
# Fire-and-forget: any failure is logged and swallowed so the download
# pipeline never blocks on a metrics write.
# ---------------------------------------------------------------------------
_PHASE_PCT = {
    "scrape":     20,
    "cover":      40,
    "bot2_send":  55,
    "bot2_wait":  70,
    "pdf_received": 85,
    "delivered":  100,
}


def _emit_progress(conn, gallery_id: str, phase: str, detail: str = "",
                   pct: Optional[int] = None) -> None:
    try:
        conn.db["progress_events"].insert_one({
            "gallery_id": str(gallery_id),
            "phase": phase,
            "detail": (detail or "")[:200],
            "pct": int(pct if pct is not None
                       else _PHASE_PCT.get(phase, 0)),
            "ts": time.time(),
        })
    except Exception as e:  # noqa: BLE001
        log.info("progress event write failed (non-fatal): %s", e)

# Sentinel status codes — kept aligned with V1 relay.py so worker.py can log
# them uniformly.
DONE          = "done"
PARTIAL       = "partial"

# ---------------------------------------------------------------------------
# User-facing strings.
#
# progress_tracker renders a job's `detail` field VERBATIM into the chat of
# whoever queued the link. Anything technical (Bot 2's raw error text, stack
# traces, entity IDs) must NEVER go in `detail` — it goes to the admin DM.
# These constants are the only things the end user ever reads on failure.
# ---------------------------------------------------------------------------
USER_MSG_SOURCE_ERROR = "source error — please pick another gallery"
USER_MSG_TIMEOUT      = "took too long — please try again later"
USER_MSG_SCRAPE_FAIL  = "could not read this gallery — please pick another"
FAILED_NO_PDF = "failed: no PDF"
FAILED_SOURCE = "failed: source error"
FAILED_SCRAPE = "failed: scrape returned nothing"
FAILED_DUP    = "duplicate"           # already COMPLETED / PROCESSING
FAILED_OTHER  = "failed: other"


@dataclass
class JobOutcome:
    """Same shape as V1 relay.JobOutcome for worker.py compatibility."""
    status: str
    detail: str = ""
    open_link: Optional[str] = None      # populated on DONE / already_completed


# ---------------------------------------------------------------------------
# Small helpers reused from V1 relay.py's spirit
# ---------------------------------------------------------------------------

async def _with_flood(coro_factory, *, context: str, conn):
    """Run an awaitable, retrying on FloodWaitError (§7a preserved from V1)."""
    while True:
        try:
            return await coro_factory()
        except FloodWaitError as e:
            secs = int(getattr(e, "seconds", 0)) + 5
            log.warning("FloodWait in %s: sleeping %ss", context, secs)
            try:
                db.log_flood(conn, secs, context)
            except Exception:
                pass
            await asyncio.sleep(secs)


async def _get_bot2_entity(client: TelegramClient):
    return await client.get_entity(settings.bot2_username)


# ---------------------------------------------------------------------------
# Auto-DM helpers (BUG FIX — previously the userbot couldn't resolve the
# requester's PeerUser because it had never seen them, so nothing was sent)
# ---------------------------------------------------------------------------
_TG_BOT_API = "https://api.telegram.org"
_AUTO_DM_HTTP_TIMEOUT = 15.0


def _admin_bot_token() -> str:
    """Resolve the admin bot token from settings / env. Same fallback chain
    the mini-app uses."""
    tok = getattr(settings, "admin_bot_token", "") or ""
    if tok:
        return tok
    for name in ("BOT_TOKEN", "ADMIN_BOT_TOKEN"):
        v = os.environ.get(name)
        if v:
            return v
    return ""


async def _bot_api_call(method: str, payload: dict) -> dict:
    """Bot API call from inside the async worker. Returns the JSON envelope
    (with a normalised {'ok':False,'description':...} on failure)."""
    token = _admin_bot_token()
    if not token:
        return {"ok": False, "description": "admin bot token not configured"}
    url = f"{_TG_BOT_API}/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=_AUTO_DM_HTTP_TIMEOUT) as c:
            r = await c.post(url, json=payload)
        try:
            data = r.json() or {}
        except Exception:
            return {"ok": False,
                    "description": f"non-JSON response HTTP {r.status_code}"}
        if not data.get("ok") and "error_code" not in data:
            data["error_code"] = r.status_code
        return data
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": f"http error: {e!s}"}


async def _copy_message_via_bot(
    from_chat_id: int, to_chat_id: int, message_id: int, conn=None,
) -> dict:
    """Copy a single message via Bot API copyMessage; fall back to
    forwardMessage if the copy is refused for a non-permission reason.
    When the admin's 'Disable sharing' toggle is on, protect_content is
    set so the recipient can't forward/save the DM."""
    payload = {
        "chat_id":       int(to_chat_id),
        "from_chat_id":  int(from_chat_id),
        "message_id":    int(message_id),
    }
    if conn is not None and feature_flags.share_disabled(conn):
        payload["protect_content"] = True
    r = await _bot_api_call("copyMessage", payload)
    if r.get("ok"):
        return r
    desc = (r.get("description") or "").lower()
    # Hard failures — no point retrying with forwardMessage.
    if any(k in desc for k in (
        "bot can't initiate", "user is deactivated",
        "chat not found", "blocked",
    )):
        return r
    r2 = await _bot_api_call("forwardMessage", payload)
    return r2 if r2.get("ok") else r


async def _send_message_via_bot(chat_id: int, text: str) -> dict:
    return await _bot_api_call(
        "sendMessage",
        {"chat_id": int(chat_id), "text": text},
    )


async def _auto_dm_requester(
    client: TelegramClient,
    user_id: int,
    *,
    cover_msg_id: int,
    pdf_msg_id: int,
    channel,
    conn,
    gallery_id: str = "",
    is_admin_requester: bool = False,
) -> None:
    """Deliver the cover + PDF into the requester's DM automatically
    after a fresh completion (no second tap on Queue required).

    STRATEGY (in order):
      1. **Bot API `copyMessage`** via the admin bot token. This is the
         PRIMARY path — the admin bot can DM any user who has ever
         `/start`'d it, and mini-app users have (initData signing requires
         the WebApp to be opened from a Bot 1 chat button). No
         `get_input_entity` limitation — numeric `chat_id` is enough.
      2. **Userbot `forward_messages`** as a fallback. Only useful when
         the userbot has already seen the user (rare for mini-app users).

    On success we also send a plain "📨 Sent to your DM" text so the user
    always gets an explicit confirmation.

    Skips when the requester IS the admin (they already see the channel).

    Best-effort: never raises. Logs on failure.
    """
    if is_admin_requester:
        log.info("auto-DM skipped: requester %s is admin", user_id)
        return
    if not user_id or user_id <= 0:
        return
    if not cover_msg_id and not pdf_msg_id:
        log.info("auto-DM skipped: no cover/pdf msg IDs for uid=%s", user_id)
        return

    # -------- 0) Force-join gate (admin feature 3) ------------------------
    try:
        missing = await feature_flags.check_membership(conn, int(user_id))
    except Exception as e:  # noqa: BLE001
        log.warning("force_join check failed (letting user through): %s", e)
        missing = []
    if missing:
        feature_flags.remember_pending(conn, int(user_id), str(gallery_id or ""))
        await feature_flags.send_join_prompt(int(user_id), missing,
                                             gallery_id=str(gallery_id or ""))
        log.info("auto-DM blocked by force-join for uid=%s (%d channels)",
                 user_id, len(missing))
        return

    # v1.22 BackupDB: when /usebackupDB is ON and this gallery has backup
    # ids, deliver from the Backup Database Channel instead of Main (the
    # disaster-recovery path if Main ever gets banned/deleted).
    from_chat = int(getattr(settings, "database_channel_id", 0) or 0)
    msg_ids = [int(m) for m in (cover_msg_id, pdf_msg_id) if m]
    try:
        import backup_db as _bdb
        from_chat, msg_ids = _bdb.delivery_source(
            conn, str(gallery_id or ""), from_chat, cover_msg_id, pdf_msg_id)
    except Exception as _be:  # noqa: BLE001
        log.warning("backup_db delivery_source failed (using Main): %s", _be)

    # -------- 1) Primary path: Bot API copyMessage -------------------------
    if from_chat and _admin_bot_token():
        delivered_any = False
        last_error = ""
        sent_msg_ids: list[int] = []  # for the auto-delete scheduler
        # Cover first so the PDF replies to a message the user has seen.
        for mid in msg_ids:
            r = await _copy_message_via_bot(from_chat, int(user_id), mid, conn=conn)
            if r.get("ok"):
                delivered_any = True
                new_mid = int((r.get("result") or {}).get("message_id") or 0)
                if new_mid:
                    sent_msg_ids.append(new_mid)
            else:
                last_error = str(r.get("description") or "unknown")
                log.warning(
                    "auto-DM copyMessage failed uid=%s msg_id=%s: %s",
                    user_id, mid, last_error,
                )
                desc = last_error.lower()
                # Hard failures for THIS user — stop trying further msgs.
                if any(k in desc for k in (
                    "bot can't initiate", "blocked",
                    "user is deactivated", "chat not found",
                )):
                    break

        if delivered_any:
            # Send an explicit confirmation text so the user sees
            # "📨 Sent to your DM" in the same DM thread.
            conf = await _send_message_via_bot(
                int(user_id), "📨 Sent to your DM",
            )
            if conf.get("ok"):
                conf_mid = int((conf.get("result") or {}).get("message_id") or 0)
                if conf_mid:
                    sent_msg_ids.append(conf_mid)
            else:
                log.info(
                    "auto-DM confirmation sendMessage failed uid=%s: %s",
                    user_id, conf.get("description"),
                )
            # Feature 1 (Auto-delete): schedule deletion of the delivered
            # messages after N hours (no-op unless the admin enabled it).
            try:
                feature_flags.schedule_deletes(conn, int(user_id), sent_msg_ids)
            except Exception as e:  # noqa: BLE001
                log.warning("deletion scheduling failed (non-fatal): %s", e)
            # Force-join pending cleanup.
            try:
                feature_flags.pop_pending(conn, int(user_id),
                                          str(gallery_id or ""))
            except Exception:
                pass
            log.info("auto-DM: delivered via Bot API to uid=%s", user_id)
            return

        # Cover + PDF both refused via Bot API. If it's the "user never
        # /start'd the bot" case, no fallback will help — log and stop.
        if last_error and ("initiate conversation" in last_error.lower()
                           or "blocked" in last_error.lower()):
            log.info(
                "auto-DM: user %s hasn't /start'd the bot (or blocked it) — "
                "skipping userbot fallback", user_id,
            )
            return

    # -------- 2) Fallback: userbot forward_messages ------------------------
    # v12.12 (#1): Telethon forward_messages CANNOT carry protect_content.
    # When the admin's Disable-sharing toggle is on, delivering via this
    # path would hand the user a FORWARDABLE copy — defeating the toggle
    # entirely. Gate the fallback off in that case; the user can retry
    # once the Bot API path recovers (or after they /start the bot).
    try:
        if feature_flags.share_disabled(conn):
            log.info(
                "auto-DM: userbot fallback suppressed for uid=%s — "
                "share_disabled is ON and userbot forwards are unprotectable",
                user_id,
            )
            return
    except Exception:  # noqa: BLE001
        pass  # never let a settings read failure block delivery
    try:
        target = await _with_flood(
            lambda: client.get_input_entity(int(user_id)),
            context="resolve_requester", conn=conn,
        )
    except Exception as e:  # noqa: BLE001
        log.info(
            "auto-DM: userbot can't resolve requester %s (%s) — giving up. "
            "Dedup path on next Queue tap will still deliver.",
            user_id, e,
        )
        return

    try:
        await _with_flood(
            lambda: client.forward_messages(
                target, msg_ids, from_peer=channel, drop_author=True,
            ),
            context="auto_dm_forward", conn=conn,
        )
        log.info(
            "auto-DM: userbot forwarded %d msgs to uid=%s",
            len(msg_ids), user_id,
        )
        # Confirmation text via the userbot too.
        try:
            await _with_flood(
                lambda: client.send_message(target, "📨 Sent to your DM"),
                context="auto_dm_confirm", conn=conn,
            )
        except Exception as e:  # noqa: BLE001
            log.info("auto-DM confirmation via userbot failed uid=%s: %s",
                     user_id, e)
    except Exception as e:  # noqa: BLE001
        log.warning("auto-DM userbot forward failed uid=%s: %s", user_id, e)


async def _send_mpost(
    client: TelegramClient, open_link: Optional[str], conn
) -> None:
    """Fire-and-forget: DM @Doujinshibot with /mpost <open_link>. Same
    contract as V1 relay._send_mpost."""
    if not open_link:
        log.warning("mpost skipped: no open_link captured")
        return
    username = getattr(settings, "doujinshibot_username", "") or ""
    if not username:
        log.info("mpost skipped: doujinshibot_username not configured")
        return
    try:
        await _with_flood(
            lambda: client.send_message(username, f"/mpost {open_link}"),
            context="mpost",
            conn=conn,
        )
        log.info("mpost sent to @%s: /mpost %s", username, open_link)
    except Exception as e:  # noqa: BLE001
        log.warning("mpost send failed for %s (non-fatal): %s", open_link, e)


async def _notify_admin_failure(
    client: TelegramClient,
    url: str,
    gallery_id: str,
    reason: str,
    conn,
) -> None:
    """DM the admin with an offending link + Bot 2's error text."""
    admin_id = int(getattr(settings, "admin_user_id", 0) or 0)
    if not admin_id:
        return
    text = (
        "⚠️ Bot 2 rejected a gallery\n"
        f"gallery_id: {gallery_id}\n"
        f"url: {url}\n"
        f"reason: {reason[:400]}"
    )
    try:
        await _with_flood(
            lambda: client.send_message(admin_id, text),
            context="admin_failure_dm",
            conn=conn,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("admin failure DM failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _bot2_timeout_sec(pages: Optional[int] = None) -> int:
    """Bot 2 PDF wait timeout in seconds.

    v11.3: was a flat env-var (default 60, hardcoded ceiling 480); now
    scales adaptively with ``pages`` so 200-page (~150 MB) doujinshi get
    the ~7 min they need while 20-page galleries still time out fast if
    Bot 2 misbehaves. The old env-var still acts as the FLOOR so any ops
    knob set today stays honoured as the lower bound.
    """
    try:
        base = int(getattr(settings, "bot2_pdf_timeout_sec", 0) or 0)
    except (TypeError, ValueError):
        base = 0
    base = base if base > 0 else 90   # v11.3 floor — was 480 flat pre-v11.3
    return compute_pdf_timeout(pages, base_timeout_sec=base)


def _self_cover_enabled() -> bool:
    """Env-var master switch (docs/MIGRATION_V2.md §5). Default ON."""
    return (os.getenv("SELF_COVER_POST_ENABLED", "1") or "1").strip() not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# v11.3 / v12.33 — cover-post <-> PDF pairing serialisation
# ---------------------------------------------------------------------------
#
# v11.3 root cause: bot2_client.wait_for_pdf pairs covers to PDFs by
# timestamp only (since_ts), not by a per-cover anchor. When Bot 2 is
# slow (large galleries) the next cover post could be sent while the
# previous PDF was still being generated — and the first PDF to arrive
# would be claimed by the wrong job. v11.3 fixed this by putting the
# ENTIRE post_cover → Bot2 send → wait-for-PDF chain under one
# process-local asyncio.Lock. Correct but serial: with 1 userbot the
# queue drained at Bot 2's speed.
#
# v12.33 refactor (multi-userbot pool): the wait_for_pdf race is now
# closed by bot2_client's PER-CLIENT `_last_sent_msg_id_by_client` floor
# (each userbot has its own DM history with Bot 2, so the message-id
# floor is naturally isolated). The lock's job shrinks to: keep the two
# DB-channel writes (post cover, forward PDF) atomic so the channel
# always reads cover_A, pdf_A, cover_B, pdf_B — never interleaved.
#
# Concretely: post_cover and forward_messages are held under the pool's
# `channel_write()` lock; the send-URL-to-Bot2 + wait-for-PDF section
# runs OUTSIDE the lock so 2+ userbots wait on Bot 2 in parallel. That
# is where the throughput comes from.
#
# The pool's channel lock lives on the UserbotPool singleton
# (userbot_pool.get_global().channel_write()). See userbot_pool.py.


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def process_job(
    client: TelegramClient,
    url: str,
    url_hash: str,
    job_id: Optional[int] = None,
    via_search: bool = False,
    username: Optional[str] = None,
    mpost_enabled: bool = False,
    submitted_by: Optional[int] = None,
) -> JobOutcome:
    """Execute the V2 per-link flow once."""
    conn = db.connect()
    try:
        # ---- 0) Gallery ID extraction ----------------------------------
        gid = gs.extract_gallery_id(url)
        if not gid:
            log.warning("relay_v2: no gallery_id extractable from url=%r", url)
            # Fall back to V1's url_hash dedup so a malformed URL still can't
            # be enqueued in an infinite loop.
            if db.has_completed(conn, url_hash):
                if job_id is not None:
                    db.upsert_job_progress(conn, job_id, db.PHASE_DONE,
                                           detail="already processed (url_hash)")
                return JobOutcome(DONE, "already in processed_urls")
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_FAILED,
                                       detail="could not extract gallery_id")
            return JobOutcome(FAILED_OTHER, "no gallery_id extractable")

        # ---- 1) Dedup gate ----------------------------------------------
        decision = gs.dedup_check(
            conn, gid,
            url=url, url_hash=url_hash,
            requested_by=submitted_by,
        )

        if decision.action == "already_completed":
            if job_id is not None:
                # Stamp the cached deep-link on THIS job row so the live
                # progress-tracker message in the requester's chat can
                # surface "Open Post" — same delivery UX as a fresh run.
                if decision.open_link:
                    try:
                        db.set_cover_link(conn, job_id, decision.open_link)
                    except Exception as e:  # noqa: BLE001
                        log.warning("set_cover_link on dedup-hit failed: %s", e)
                db.upsert_job_progress(
                    conn, job_id, db.PHASE_DONE,
                    title=(decision.title or url)[:80],
                    detail="already on file",
                )
                # Mark the queue row done so the batch summary counts this
                # as a success instead of leaving a lingering row.
                try:
                    db.mark_status(conn, job_id, "done", "already on file")
                except Exception as e:  # noqa: BLE001
                    log.warning("mark_status(done) on dedup-hit failed: %s", e)
            log.info("relay_v2 dedup: gallery_id=%s already %s → %s",
                     gid, decision.status, decision.open_link)
            return JobOutcome(
                DONE, "already on file",
                open_link=decision.open_link,
            )

        if decision.action == "already_processing":
            if job_id is not None:
                db.upsert_job_progress(
                    conn, job_id, db.PHASE_FAILED,
                    detail="another worker is already downloading",
                )
            log.info("relay_v2 dedup: gallery_id=%s already PROCESSING", gid)
            return JobOutcome(
                FAILED_DUP,
                "already being downloaded by another worker",
            )

        # decision.action in ("proceed", "stale_reset") → we own the PROCESSING slot.
        if job_id is not None:
            db.upsert_job_progress(conn, job_id, db.PHASE_PENDING, title=url)

        # ---- 2) Resolve entities ---------------------------------------
        try:
            channel = await _with_flood(
                lambda: client.get_entity(settings.database_channel_id),
                context="resolve_channel", conn=conn,
            )
            bot2 = await _with_flood(
                lambda: _get_bot2_entity(client),
                context="resolve_bot2", conn=conn,
            )
        except Exception as e:  # noqa: BLE001
            gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_OTHER,
                           reason=f"entity resolve failed: {e!s}"[:400])
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_FAILED,
                                       detail=f"entity resolve failed: {e!s}"[:180])
            return JobOutcome(FAILED_OTHER, f"entity resolve failed: {e!s}")

        # ---- 3) In-house cover post (replaces Bot 1) -------------------
        requester_handle: Optional[str] = None
        if via_search and username:
            requester_handle = username if username.startswith("@") else f"@{username}"

        if not _self_cover_enabled():
            # Safety valve for V2 rollback — return early so worker.py's
            # V1 relay path picks the job back up on the next tick.
            log.warning("SELF_COVER_POST_ENABLED=0 → releasing gid=%s back to V1", gid)
            gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_OTHER,
                           reason="V2 disabled at runtime", purge=True)
            return JobOutcome(FAILED_OTHER, "V2 disabled via SELF_COVER_POST_ENABLED=0")

        # ---- v12.34: scrape+fetch UNLOCKED → ONE locked channel pair ------
        # v12.33 held the channel lock TWICE per job (cover post in one
        # window, PDF forward in a later window). Two slots could interleave
        # cover_A, cover_B, pdf_A, pdf_B — the exact "jumbled channel" bug
        # from prod (screenshot 2026-08-21). v12.34 moves ALL channel writes
        # into ONE lock window at the very end:
        #   prepare (scrape+cover download) → Bot2 send → wait for PDF
        #   all run UNLOCKED and fully parallel across slots; only after the
        #   PDF is in hand do we acquire the lock and post cover + forward
        #   the PDF back-to-back.
        # Consequence (user-approved): on Bot2 timeout/error NOTHING is
        # posted to the channel — no orphan covers, nothing to roll back.
        from userbot_pool import get_global as _get_pool
        _pool = _get_pool()

        if _pool is not None:
            _cw_factory = _pool.channel_write
        else:
            # Fallback for tests / legacy callers where the pool hasn't been
            # wired: a no-op contextmanager.
            from contextlib import asynccontextmanager as _acm
            @_acm
            async def _cw_factory():
                yield

        _emit_progress(conn, gid, "scrape", "Scraping gallery metadata + cover")

        # ---- 3) Prepare cover (scrape + download) — UNLOCKED ------------
        try:
            prepared = await _with_flood(
                lambda: cover_poster.prepare_cover(
                    url, requester_handle=requester_handle,
                ),
                context="prepare_cover", conn=conn,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("relay_v2: prepare_cover raised: %s", e)
            prepared = None

        if prepared is None:
            reason = "cover scrape/prepare returned nothing"
            gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_SCRAPE,
                           reason=reason)
            await _notify_admin_failure(
                client, url, gid,
                "SCRAPE FAILED — hf_scraper returned no metadata (gallery "
                "deleted, private, or the v2 API changed shape). Bot 2 was "
                "NOT contacted; nothing posted to the channel.",
                conn,
            )
            if job_id is not None:
                db.upsert_job_progress(
                    conn, job_id, db.PHASE_FAILED,
                    detail=USER_MSG_SCRAPE_FAIL,
                )
                try:
                    db.mark_status(conn, job_id, "failed", reason)
                except Exception as e:  # noqa: BLE001
                    log.warning("mark_status(failed) on scrape fail failed: %s", e)
            return JobOutcome(FAILED_SCRAPE, reason)

        # ---- 4) DM Bot 2 (UNLOCKED) -------------------------------------
        if job_id is not None:
            db.upsert_job_progress(
                conn, job_id, db.PHASE_SENT_BOTS,
                title=(prepared.title or url)[:80],
                detail="metadata ready, contacting Bot 2",
            )
        _emit_progress(conn, gid, "bot2_send",
                       "Contacting PDF generator bot")
        try:
            since_ts = await _with_flood(
                lambda: bot2_client.send_link(client, bot2, url),
                context="dm_bot2", conn=conn,
            )
            db.touch_bot_ping(conn, "bot2")
        except Exception as e:  # noqa: BLE001
            # v12.34: no cover exists yet — nothing to roll back in the
            # channel, just tombstone.
            gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_OTHER,
                           reason=f"bot2 send failed: {e!s}"[:400])
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_FAILED,
                                       detail=f"bot2 send failed: {e!s}"[:180])
            return JobOutcome(FAILED_OTHER, f"bot2 send failed: {e!s}")

        # ---- 5) Wait for Bot 2's PDF (UNLOCKED — parallelism) -----------
        if job_id is not None:
            db.upsert_job_progress(
                conn, job_id, db.PHASE_WAIT_PDF,
                title=(prepared.title or url)[:80],
                detail="waiting for PDF from Bot 2",
            )
        _emit_progress(conn, gid, "bot2_wait",
                       "Your PDF is being generated…")

        # v12.34: page count is known BEFORE contacting Bot 2 (prepare
        # already scraped it), so the adaptive timeout is right on the
        # first try — v12.33's cancel-and-restart wait hack is gone.
        _pdf_wait = _bot2_timeout_sec(prepared.pages)
        _emit_progress(
            conn, gid, "bot2_wait",
            f"Waiting up to {describe_timeout(prepared.pages, _pdf_wait)}",
        )
        try:
            outcome = await bot2_client.wait_for_pdf(
                client, bot2, since_ts, _pdf_wait,
            )
        except FloodWaitError as e:
            secs = int(getattr(e, "seconds", 0)) + 5
            log.warning("FloodWait during Bot 2 wait: sleeping %ss", secs)
            db.log_flood(conn, secs, "wait_bot2")
            # v12.33: cool THIS userbot slot in the pool (admin alert fires).
            if _pool is not None:
                for _s in _pool.slots:
                    if _s.client is client:
                        await _pool.mark_flood(_s, secs, context="wait_bot2")
                        break
            await asyncio.sleep(secs)
            gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_TIMEOUT,
                           reason="flood-wait during Bot 2 wait")
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_FAILED,
                                       detail="flood-wait during Bot 2 wait")
            return JobOutcome(FAILED_NO_PDF, "flood-wait during Bot 2 wait")

        # ---- 6) Branch on Bot 2 outcome ---------------------------------

        if outcome.kind == bot2_client.OUTCOME_TEXT_REPLY:
            # Bot 2 rejected the link. v12.34: nothing was ever posted to
            # the channel (no cover to delete) — just purge + notify.
            gs.mark_failed(
                conn, gid, status=gs.STATUS_FAILED_BOT2,
                reason=f"bot2 said: {outcome.error_text}"[:400],
                purge=True,   # <-- purge per spec (§4)
            )
            await _notify_admin_failure(
                client, url, gid, outcome.error_text or "(no text)", conn,
            )
            if job_id is not None:
                # `detail` is rendered verbatim into the requester's chat by
                # progress_tracker — keep it friendly. Bot 2's raw error text
                # goes ONLY to the admin DM above.
                db.upsert_job_progress(
                    conn, job_id, db.PHASE_FAILED,
                    detail=USER_MSG_SOURCE_ERROR,
                )
                try:
                    db.mark_status(
                        conn, job_id, "failed",
                        f"bot2 error: {outcome.error_text[:400]}",
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("mark_status(failed) on bot2 error failed: %s", e)
            return JobOutcome(
                FAILED_SOURCE,
                f"bot2 error: {outcome.error_text[:120]}",
            )

        if outcome.kind == bot2_client.OUTCOME_TIMEOUT:
            # v12.34: NO orphan cover in the channel — tombstone only. The
            # tombstone lets the user retry after BOT2_PDF_TIMEOUT + a
            # follow-up admin resetdoc if needed.
            gs.mark_failed(
                conn, gid, status=gs.STATUS_FAILED_TIMEOUT,
                reason=outcome.error_text or "no PDF within deadline",
                purge=False,
            )
            await _notify_admin_failure(
                client, url, gid,
                f"TIMEOUT after {_pdf_wait}s — Bot 2 never sent a PDF "
                f"(nothing posted to the channel; doc tombstoned as "
                f"FAILED_TIMEOUT)",
                conn,
            )
            if job_id is not None:
                db.upsert_job_progress(
                    conn, job_id, db.PHASE_FAILED,
                    detail=USER_MSG_TIMEOUT,
                )
                try:
                    db.mark_status(
                        conn, job_id, "failed",
                        f"no PDF within {_pdf_wait}s",
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("mark_status(failed) on timeout failed: %s", e)
            return JobOutcome(FAILED_NO_PDF, "no PDF within deadline")

        # OUTCOME_OK — we have the PDF message.
        bot2_msg = outcome.pdf_message
        db.touch_bot_ping(conn, "bot2")

        # v11.7: telemetry for the auto-tuner. Latency = (now - Bot2 send ts).
        # Best-effort; never raises upward.
        try:
            _lat = float(int(time.time()) - int(since_ts or 0))
            if _lat > 0:
                record_bot2_latency(prepared.pages, _lat)
        except Exception:  # noqa: BLE001
            pass

        _emit_progress(conn, gid, "pdf_received",
                       "PDF received — posting to channel")

        # ---- 7) ONE locked window: post cover + forward PDF -------------
        # THE v12.34 guarantee: the DB channel always reads
        # cover_A, pdf_A, cover_B, pdf_B because BOTH writes happen inside a
        # single channel_write() acquisition. No other slot's writes can
        # interleave between the cover and its PDF.
        if job_id is not None:
            db.upsert_job_progress(
                conn, job_id, db.PHASE_FORWARDING,
                title=(prepared.title or url)[:80],
                detail="posting cover + PDF to channel",
            )

        async with _cw_factory():
            # 7a) cover post (inside the lock). On failure we release the
            # lock WITHOUT forwarding — the channel stays clean (no orphan
            # cover, no orphan PDF).
            try:
                cover = await _with_flood(
                    lambda: cover_poster.post_prepared_cover(
                        client, prepared,
                        channel_id=int(settings.database_channel_id),
                    ),
                    context="post_prepared_cover", conn=conn,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("relay_v2: post_prepared_cover raised: %s", e)
                cover = None

            if cover is None:
                gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_OTHER,
                               reason="cover post failed at delivery step"[:400])
                if job_id is not None:
                    db.upsert_job_progress(conn, job_id, db.PHASE_FAILED,
                                           detail="cover post failed"[:180])
                return JobOutcome(FAILED_OTHER, "cover post failed at delivery")

            _emit_progress(conn, gid, "cover",
                           "Cover posted to DB channel")

            # 7b) forward the PDF immediately after the cover (SAME lock).
            forwarded = None
            forward_exc: Optional[Exception] = None
            try:
                forwarded = await _with_flood(
                    lambda: client.forward_messages(
                        channel, bot2_msg, drop_author=True,
                    ),
                    context="forward_pdf", conn=conn,
                )
            except Exception as e:  # noqa: BLE001
                forward_exc = e

            if forward_exc is not None:
                # Roll back the just-posted cover so the channel never shows
                # a cover without its PDF.
                await cover_poster.delete_cover(
                    client, channel_id=cover.channel_id, msg_id=cover.msg_id,
                )
                gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_OTHER,
                               reason=f"forward failed: {forward_exc!s}"[:400])
                if job_id is not None:
                    db.upsert_job_progress(conn, job_id, db.PHASE_FAILED,
                                           detail=f"forward failed: {forward_exc!s}"[:180])
                return JobOutcome(FAILED_OTHER, f"forward failed: {forward_exc!s}")

        # Extract the PDF's new message ID inside the DB channel.
        pdf_msg_id = 0
        if forwarded:
            m = forwarded[0] if isinstance(forwarded, list) else forwarded
            pdf_msg_id = int(getattr(m, "id", 0) or 0)

        # ---- v1.22 BackupDB: best-effort mirror of the just-posted pair --
        # Server-side forward into the Backup Database Channel and stamp the
        # backup msg ids onto the same galleries doc. NEVER blocks the user
        # download — failures are logged + counted only.
        try:
            import backup_db as _bdb
            await _bdb.mirror_pair_to_backup(
                client, conn, settings, gid, channel,
                int(getattr(cover, "msg_id", 0) or 0), pdf_msg_id,
                log_prefix="relay_v2",
            )
        except Exception as _be:  # noqa: BLE001
            log.warning("backup_db relay_v2 mirror raised (non-fatal): %s", _be)

        db.record_processed(conn, url, url_hash)

        # ---- 7) Persist COMPLETED --------------------------------------
        # BUG FIX (caption meta rows): cover.tags is now the TYPED list
        # ({'name','type'} dicts) that hf_scraper preserves. Persist it as
        # such so the mini-app can rebuild grouped rows and so mark_partial
        # / re-post paths keep the same shape. If cover.tags happens to be
        # a legacy flat name list, coerce each entry to {'name','type':'tag'}
        # so we never break the DB schema.
        _persist_tags = []
        for _t in (cover.tags or []):
            if isinstance(_t, dict) and _t.get("name"):
                _persist_tags.append({
                    "name": str(_t["name"]),
                    "type": str(_t.get("type") or "tag"),
                })
            elif _t:
                _persist_tags.append({"name": str(_t), "type": "tag"})

        gs.mark_completed(
            conn, gid,
            title=cover.title,
            pages=cover.pages,
            tags=_persist_tags,
            cover_url=cover.cover_url,
            db_cover_msg_id=cover.msg_id,
            db_pdf_msg_id=pdf_msg_id,
            open_link=cover.open_link,
            job_id=job_id,
        )
        if cover.open_link and job_id is not None:
            db.set_cover_link(conn, job_id, cover.open_link)

        # ---- 8) Optional /mpost ----------------------------------------
        if mpost_enabled:
            if job_id is not None:
                db.upsert_job_progress(
                    conn, job_id, db.PHASE_MPOSTING,
                    title=(cover.title or url)[:80],
                    detail="sending /mpost",
                )
            await _send_mpost(client, cover.open_link, conn)

        # ---- 9) AUTO-DM the requester (BUG FIX) ------------------------
        # After a fresh completion, forward the cover + PDF straight into
        # the requester's DM using the userbot session (which is admin in
        # the DB channel). No second tap on Queue required.
        #
        # Rules:
        #   - Only when we know who submitted the job (`submitted_by`).
        #   - Skip if the requester IS the admin (avoids double-DM: the
        #     admin already sees the post in the channel).
        #   - Best-effort: any failure is logged and the job still returns
        #     DONE (the cover + PDF are already in the channel).
        try:
            admin_id = int(getattr(settings, "admin_user_id", 0) or 0)
        except (TypeError, ValueError):
            admin_id = 0
        if submitted_by and int(submitted_by) > 0:
            try:
                await _auto_dm_requester(
                    client, int(submitted_by),
                    cover_msg_id=int(cover.msg_id or 0),
                    pdf_msg_id=int(pdf_msg_id or 0),
                    channel=channel,
                    conn=conn,
                    gallery_id=str(gid or ""),
                    is_admin_requester=(int(submitted_by) == admin_id),
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "auto-DM to requester %s failed (non-fatal): %s",
                    submitted_by, e,
                )

        if job_id is not None:
            db.upsert_job_progress(
                conn, job_id, db.PHASE_DONE,
                title=(cover.title or url)[:80],
                detail="posted ✅",
            )
        _emit_progress(conn, gid, "delivered",
                       "Ready — delivering to your DM…")
        return JobOutcome(DONE, "cover + PDF posted", open_link=cover.open_link)

    finally:
        conn.close()
