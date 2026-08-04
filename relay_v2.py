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

from telethon import TelegramClient
from telethon.errors import FloodWaitError

import db
import gallery_state as gs
import cover_poster
import bot2_client
from config import settings

log = logging.getLogger("relay_v2")

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

def _bot2_timeout_sec() -> int:
    try:
        v = int(getattr(settings, "bot2_pdf_timeout_sec", 0) or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else 480


def _self_cover_enabled() -> bool:
    """Env-var master switch (docs/MIGRATION_V2.md §5). Default ON."""
    return (os.getenv("SELF_COVER_POST_ENABLED", "1") or "1").strip() not in ("0", "false", "no")


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

        try:
            cover = await _with_flood(
                lambda: cover_poster.post_cover(
                    client, url,
                    channel_id=int(settings.database_channel_id),
                    requester_handle=requester_handle,
                ),
                context="post_cover", conn=conn,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("relay_v2: cover_poster raised: %s", e)
            cover = None

        if cover is None:
            reason = "cover scrape/post returned nothing"
            gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_SCRAPE,
                           reason=reason)
            await _notify_admin_failure(
                client, url, gid,
                "SCRAPE FAILED — hf_scraper returned no metadata (gallery "
                "deleted, private, or the v2 API changed shape). No cover "
                "posted, Bot 2 not contacted.",
                conn,
            )
            if job_id is not None:
                # Friendly text for the requester; technical reason stays in
                # the queue row + admin DM only.
                db.upsert_job_progress(
                    conn, job_id, db.PHASE_FAILED,
                    detail=USER_MSG_SCRAPE_FAIL,
                )
                try:
                    db.mark_status(conn, job_id, "failed", reason)
                except Exception as e:  # noqa: BLE001
                    log.warning("mark_status(failed) on scrape fail failed: %s", e)
            return JobOutcome(FAILED_SCRAPE, reason)

        if job_id is not None:
            db.upsert_job_progress(
                conn, job_id, db.PHASE_SENT_BOTS,
                title=(cover.title or url)[:80],
                detail="cover posted, contacting Bot 2",
            )

        # ---- 4) DM Bot 2 + wait for PDF --------------------------------
        try:
            since_ts = await _with_flood(
                lambda: bot2_client.send_link(client, bot2, url),
                context="dm_bot2", conn=conn,
            )
            db.touch_bot_ping(conn, "bot2")
        except Exception as e:  # noqa: BLE001
            # Send failure → roll back the cover post, tombstone as OTHER.
            await cover_poster.delete_cover(
                client, channel_id=cover.channel_id, msg_id=cover.msg_id,
            )
            gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_OTHER,
                           reason=f"bot2 send failed: {e!s}"[:400])
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_FAILED,
                                       detail=f"bot2 send failed: {e!s}"[:180])
            return JobOutcome(FAILED_OTHER, f"bot2 send failed: {e!s}")

        if job_id is not None:
            db.upsert_job_progress(
                conn, job_id, db.PHASE_WAIT_PDF,
                title=(cover.title or url)[:80],
                detail="waiting for PDF from Bot 2",
            )

        try:
            outcome = await bot2_client.wait_for_pdf(
                client, bot2, since_ts, _bot2_timeout_sec(),
            )
        except FloodWaitError as e:
            secs = int(getattr(e, "seconds", 0)) + 5
            log.warning("FloodWait during Bot 2 wait: sleeping %ss", secs)
            db.log_flood(conn, secs, "wait_bot2")
            await asyncio.sleep(secs)
            # Simplest safe recovery: mark PARTIAL if we still have a cover,
            # else FAILED_TIMEOUT. A retry from the user is cheap now that
            # the dedup gate would just return "already_completed" or
            # allow a fresh attempt after tombstone reset.
            gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_TIMEOUT,
                           reason="flood-wait during Bot 2 wait")
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_FAILED,
                                       detail="flood-wait during Bot 2 wait")
            return JobOutcome(FAILED_NO_PDF, "flood-wait during Bot 2 wait")

        # ---- 5) Branch on Bot 2 outcome --------------------------------

        if outcome.kind == bot2_client.OUTCOME_TEXT_REPLY:
            # Bot 2 rejected the link. Per spec:
            #  - delete our cover post,
            #  - purge the galleries doc so the user can retry cleanly,
            #  - notify the admin,
            #  - tell the user to pick something else.
            await cover_poster.delete_cover(
                client, channel_id=cover.channel_id, msg_id=cover.msg_id,
            )
            gs.mark_failed(
                conn, gid, status=gs.STATUS_FAILED_BOT2,
                reason=f"bot2 said: {outcome.error_text}"[:400],
                purge=True,   # <-- purge per spec (§4)
            )
            await _notify_admin_failure(
                client, url, gid, outcome.error_text or "(no text)", conn,
            )
            if job_id is not None:
                # IMPORTANT: `detail` is rendered verbatim into the requester's
                # chat by progress_tracker (see its _render lines 138-140), so
                # we keep it friendly and non-technical here. Bot 2's raw error
                # text goes ONLY to the admin DM above.
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
            # Keep the cover post AND the doc as tombstone so admin can see
            # what got stuck. The tombstone lets the user retry after
            # BOT2_PDF_TIMEOUT + a follow-up admin resetdoc if needed.
            gs.mark_failed(
                conn, gid, status=gs.STATUS_FAILED_TIMEOUT,
                reason=outcome.error_text or "no PDF within deadline",
                purge=False,
            )
            await _notify_admin_failure(
                client, url, gid,
                f"TIMEOUT after {_bot2_timeout_sec()}s — Bot 2 never sent a PDF "
                f"(cover post kept, doc tombstoned as FAILED_TIMEOUT)",
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
                        f"no PDF within {_bot2_timeout_sec()}s",
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("mark_status(failed) on timeout failed: %s", e)
            return JobOutcome(FAILED_NO_PDF, "no PDF within deadline")

        # OUTCOME_OK — we have the PDF message.
        bot2_msg = outcome.pdf_message
        db.touch_bot_ping(conn, "bot2")

        # ---- 6) Forward PDF as reply to our cover ----------------------
        if job_id is not None:
            db.upsert_job_progress(
                conn, job_id, db.PHASE_FORWARDING,
                title=(cover.title or url)[:80],
                detail="forwarding PDF to channel",
            )
        try:
            forwarded = await _with_flood(
                lambda: client.forward_messages(
                    channel, bot2_msg, drop_author=True,
                ),
                context="forward_pdf", conn=conn,
            )
        except Exception as e:  # noqa: BLE001
            gs.mark_failed(conn, gid, status=gs.STATUS_FAILED_OTHER,
                           reason=f"forward failed: {e!s}"[:400])
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_FAILED,
                                       detail=f"forward failed: {e!s}"[:180])
            return JobOutcome(FAILED_OTHER, f"forward failed: {e!s}")

        # Extract the PDF's new message ID inside the DB channel.
        pdf_msg_id = 0
        if forwarded:
            m = forwarded[0] if isinstance(forwarded, list) else forwarded
            pdf_msg_id = int(getattr(m, "id", 0) or 0)

        db.record_processed(conn, url, url_hash)

        # ---- 7) Persist COMPLETED --------------------------------------
        gs.mark_completed(
            conn, gid,
            title=cover.title,
            pages=cover.pages,
            tags=[{"name": n, "type": "tag"} for n in (cover.tags or [])],
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

        if job_id is not None:
            db.upsert_job_progress(
                conn, job_id, db.PHASE_DONE,
                title=(cover.title or url)[:80],
                detail="posted ✅",
            )
        return JobOutcome(DONE, "cover + PDF posted", open_link=cover.open_link)

    finally:
        conn.close()
