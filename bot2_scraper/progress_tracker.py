"""
progress_tracker.py — Live progress messages for /fetch, /search Confirm,
and auto-fetch batches (Brief: progress bar feature, v8).

Design:
- Fully decoupled from the worker process. The worker (relay.py / worker.py)
  only writes rows into db.job_progress / db.progress_batches. This module,
  running inside the Admin Bot process, polls those tables every ~2s and
  edits a single Telegram message per batch to reflect current state.
- One batch = one Telegram message. Each line in the message corresponds
  to one job (one gallery URL), showing its current phase:
      ⏳ queued           -> pending
      📨 sent to bots      -> sent_bots
      📥 downloading PDF   -> wait_pdf
      📤 forwarding PDF    -> forwarding
      🔗 posting to channel -> mposting
      ✅ posted            -> done
      ⚠️ partial           -> partial
      ❌ failed            -> failed
- When every job in a batch reaches a terminal phase (done/partial/failed),
  the tracker waits 30 seconds, then deletes the message and cleans up the
  batch + job_progress rows (Brief: auto-delete 30s after all done).

Usage from admin_bot.py:
    from progress_tracker import start_batch_tracking, ensure_tracker_running

    # after enqueueing jobs and getting (job_id, url) pairs:
    await start_batch_tracking(ctx.application, chat_id, job_id_url_pairs)

    # once, at bot startup (build_app):
    ensure_tracker_running(app)
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Dict, List, Tuple

from telegram.error import BadRequest, TelegramError
from telegram.ext import Application

import db
from logging_setup import setup_logging

log = setup_logging("progress_tracker")

_POLL_INTERVAL_SEC = 2.0
_AUTO_DELETE_DELAY_SEC = 30
_TRACKER_KEY = "_progress_tracker_task"

# Footer lines appended to every progress message so users know where the
# posted doujinshi actually end up. Change here to update all future batches.
#
# Two-part footer:
#   line 1 = the main posting channel (where finished PDFs land)
#   line 2 = the daily-updates channel (announcements / summaries)
_POSTING_CHANNEL_URL = "https://t.me/+M6yURQt1-TY1YTZl"
_DAILY_UPDATES_URL   = "https://t.me/+uyNxVAVPdUBlOWU9"
_CHANNEL_FOOTER = (
    f"📢 Posting in this Channel: {_POSTING_CHANNEL_URL}\n"
    f"📣 Daily Updates Here — {_DAILY_UPDATES_URL}"
)
# Safety net: if a batch is somehow never marked terminal (a bug we haven't
# hit yet, or a job silently vanishing), stop hammering editMessageText and
# force-close it after this many seconds so it can never spam forever.
_STALE_BATCH_MAX_AGE_SEC = 20 * 60

_PHASE_ICONS = {
    db.PHASE_PENDING: "⏳",
    db.PHASE_SENT_BOTS: "📨",
    db.PHASE_WAIT_PDF: "📥",
    db.PHASE_FORWARDING: "📤",
    db.PHASE_MPOSTING: "🔗",
    db.PHASE_DONE: "✅",
    db.PHASE_PARTIAL: "⚠️",
    db.PHASE_FAILED: "❌",
}

_PHASE_LABELS = {
    db.PHASE_PENDING: "queued",
    db.PHASE_SENT_BOTS: "sent to bots",
    db.PHASE_WAIT_PDF: "downloading PDF",
    db.PHASE_FORWARDING: "forwarding PDF",
    db.PHASE_MPOSTING: "posting to channel",
    db.PHASE_DONE: "posted",
    db.PHASE_PARTIAL: "partial",
    db.PHASE_FAILED: "failed",
}

_TERMINAL = (db.PHASE_DONE, db.PHASE_FAILED, db.PHASE_PARTIAL)


def _short(title: str, limit: int = 48) -> str:
    t = (title or "").strip().replace("\n", " ")
    if len(t) <= limit:
        return t
    return t[: limit - 1] + "…"


def _render_batch_text(
    job_ids: List[int],
    rows_by_id: Dict[int, dict],
    header: str = "",
    token_line: str = "",
) -> Tuple[str, bool]:
    """Build the message body for one batch. Returns (text, all_terminal).

    `header`     : optional lines prepended above the progress list. Used by
                    /search Confirm to place its "queue for X: N queued" summary
                    (plus any "skipped" details) at the very top of the ONE
                    consolidated message.
    `token_line` : optional line placed BETWEEN the progress list and the
                    channel footer. Used by /search Confirm so a normal user
                    still sees "🎟 Tokens: N/M remaining today." without it
                    scrolling off the top when items advance.
    """
    total = len(job_ids)
    done_terminal = 0
    lines: List[str] = []
    if header:
        # Preserve internal newlines in the caller-supplied header exactly.
        lines.append(header.rstrip())
        lines.append("")
    lines.append(f"📊 Progress — {total} item{'s' if total != 1 else ''}")
    lines.append("")
    for jid in job_ids:
        row = rows_by_id.get(jid)
        if row is None:
            icon, label, title = "⏳", "queued", f"job #{jid}"
        else:
            phase = row.get("phase") or db.PHASE_PENDING
            icon = _PHASE_ICONS.get(phase, "•")
            label = _PHASE_LABELS.get(phase, phase)
            title = _short(row.get("title") or f"job #{jid}")
            if phase in _TERMINAL:
                done_terminal += 1
        detail = (row or {}).get("detail") or ""
        detail_suffix = f" — {detail}" if detail and detail not in (label,) else ""
        lines.append(f"{icon} {title} — {label}{detail_suffix}")

    all_terminal = done_terminal >= total
    n_done = sum(1 for jid in job_ids if (rows_by_id.get(jid) or {}).get("phase") == db.PHASE_DONE)
    n_failed = sum(1 for jid in job_ids if (rows_by_id.get(jid) or {}).get("phase") == db.PHASE_FAILED)
    n_partial = sum(1 for jid in job_ids if (rows_by_id.get(jid) or {}).get("phase") == db.PHASE_PARTIAL)
    lines.append("")
    if all_terminal:
        lines.append(f"Finished: ✅ {n_done}  ⚠️ {n_partial}  ❌ {n_failed}")
    else:
        lines.append(f"{done_terminal}/{total} finished so far…")
    if token_line:
        lines.append("")
        lines.append(token_line)
    # Always-visible destination footer so users know where the actual posts
    # land. Only appended if it fits within the 4000-char cap.
    footer = "\n\n" + _CHANNEL_FOOTER
    body = "\n".join(lines)
    if len(body) + len(footer) <= 4000:
        body = body + footer
    return body[:4000], all_terminal


# Per-batch header/token overrides supplied by callers who want the progress
# message to include their own preamble (e.g. /search Confirm's summary). We
# keep them in a process-local dict rather than adding two more DB columns
# because they are purely presentational and live-forgotten when the batch
# ends. Cleared automatically once the batch is deleted.
_BATCH_EXTRAS: Dict[str, Dict[str, str]] = {}


async def start_batch_tracking(
    app: Application,
    chat_id: int,
    job_id_url_pairs: List[Tuple[int, str]],
    *,
    header: str = "",
    token_line: str = "",
    existing_message_id: int | None = None,
) -> None:
    """Create a progress batch + initial message, and make sure the background
    polling loop is running. Call this right after enqueuing jobs (from
    cmd_fetch, the /search Confirm handler, or the auto-fetch handler).

    NEW keyword arguments (all optional, backwards-compatible):

    `header`               — static preamble rendered above the progress list.
                             /search Confirm passes its summary here so the
                             confirm-reply and the live progress become ONE
                             message instead of two.
    `token_line`           — static line rendered between the progress list
                             and the channel footer (used for the token count).
    `existing_message_id`  — if the caller has already sent a message the
                             tracker should EDIT in place, pass its id here
                             (skip the extra send_message). This is what turns
                             the /search reply itself into the progress
                             message.
    """
    if not job_id_url_pairs:
        return
    job_ids = [jid for jid, _u in job_id_url_pairs]
    batch_id = uuid.uuid4().hex[:12]

    # Remember the caller's presentational extras for this batch. The poll
    # loop reads them on every render.
    if header or token_line:
        _BATCH_EXTRAS[batch_id] = {"header": header, "token_line": token_line}

    conn = db.connect()
    try:
        db.create_progress_batch(conn, batch_id, chat_id, job_ids)
        for jid, url in job_id_url_pairs:
            db.upsert_job_progress(conn, jid, db.PHASE_PENDING, title=url)
        rows = db.get_progress_for_jobs(conn, job_ids)
    finally:
        conn.close()

    rows_by_id = {r["job_id"]: r for r in rows}
    text, _ = _render_batch_text(
        job_ids, rows_by_id, header=header, token_line=token_line
    )

    message_id: int | None = existing_message_id
    if message_id is None:
        try:
            msg = await app.bot.send_message(chat_id=chat_id, text=text)
            message_id = msg.message_id
        except TelegramError as e:
            log.warning("failed to send initial progress message: %s", e)
            _BATCH_EXTRAS.pop(batch_id, None)
            return
    else:
        # Caller already sent the message (e.g. /search Confirm's reply). Edit
        # it in place so it becomes the live progress message. Any failure
        # here is non-fatal — the tracker will keep trying on its next poll.
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text
            )
        except TelegramError as e:
            log.debug("initial edit_message_text failed (non-fatal): %s", e)

    conn = db.connect()
    try:
        db.set_progress_batch_message(conn, batch_id, int(message_id))
    finally:
        conn.close()

    ensure_tracker_running(app)


async def _poll_loop(app: Application) -> None:
    log.info("progress tracker poll loop started")
    pending_deletes: Dict[str, float] = {}  # batch_id -> terminal_since_ts
    # Skip redundant edit_message_text calls when the rendered text hasn't
    # changed since the last successful edit — avoids hammering the Bot API
    # with no-op edits (Telegram answers those with 400 "message is not
    # modified", which is most of the log spam we were seeing).
    last_sent_text: Dict[str, str] = {}  # batch_id -> last text actually sent

    while True:
        try:
            conn = db.connect()
            try:
                batches = db.get_active_progress_batches(conn)
                for b in batches:
                    batch_id = b["batch_id"]
                    chat_id = b["chat_id"]
                    message_id = b["message_id"]
                    job_ids = [int(x) for x in (b["job_ids"] or "").split(",") if x.strip()]
                    if not job_ids or message_id is None:
                        continue

                    rows = db.get_progress_for_jobs(conn, job_ids)
                    rows_by_id = {r["job_id"]: r for r in rows}
                    extras = _BATCH_EXTRAS.get(batch_id, {})
                    header_extra = extras.get("header", "")
                    token_extra = extras.get("token_line", "")
                    text, all_terminal = _render_batch_text(
                        job_ids, rows_by_id,
                        header=header_extra, token_line=token_extra,
                    )

                    # Safety net: force-close batches stuck open way too long
                    # (e.g. a job crashed before ever writing a terminal phase).
                    age = time.time() - int(b.get("created_at") or time.time())
                    if not all_terminal and age > _STALE_BATCH_MAX_AGE_SEC:
                        log.warning(
                            "batch %s stale after %.0fs with no terminal state; force-closing",
                            batch_id, age,
                        )
                        for jid in job_ids:
                            row = rows_by_id.get(jid)
                            if row is None or row.get("phase") not in _TERMINAL:
                                db.upsert_job_progress(
                                    conn, jid, db.PHASE_FAILED, detail="stale — no update received"
                                )
                        rows = db.get_progress_for_jobs(conn, job_ids)
                        rows_by_id = {r["job_id"]: r for r in rows}
                        text, all_terminal = _render_batch_text(
                            job_ids, rows_by_id,
                            header=header_extra, token_line=token_extra,
                        )

                    if last_sent_text.get(batch_id) != text:
                        try:
                            await app.bot.edit_message_text(
                                chat_id=chat_id, message_id=message_id, text=text
                            )
                            last_sent_text[batch_id] = text
                        except BadRequest as e:
                            if "not modified" in str(e).lower():
                                # Someone else already put this exact text up
                                # (or a race); remember it so we stop retrying.
                                last_sent_text[batch_id] = text
                            else:
                                log.debug("edit_message_text failed: %s", e)
                        except TelegramError as e:
                            log.debug("edit_message_text telegram error: %s", e)

                    if all_terminal:
                        first_seen = pending_deletes.get(batch_id)
                        if first_seen is None:
                            pending_deletes[batch_id] = time.time()
                        elif time.time() - first_seen >= _AUTO_DELETE_DELAY_SEC:
                            try:
                                await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
                            except TelegramError as e:
                                log.debug("delete_message failed (non-fatal): %s", e)
                            db.complete_progress_batch(conn, batch_id)
                            db.cleanup_progress(conn, job_ids)
                            db.delete_progress_batch(conn, batch_id)
                            pending_deletes.pop(batch_id, None)
                            last_sent_text.pop(batch_id, None)
                            _BATCH_EXTRAS.pop(batch_id, None)
                    else:
                        pending_deletes.pop(batch_id, None)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 — keep the loop alive forever
            log.exception("progress tracker loop error: %s", e)

        await asyncio.sleep(_POLL_INTERVAL_SEC)


def ensure_tracker_running(app: Application) -> None:
    """Idempotently start the single background polling task on this app."""
    existing = app.bot_data.get(_TRACKER_KEY)
    if existing is not None and not existing.done():
        return
    task = asyncio.get_event_loop().create_task(_poll_loop(app))
    app.bot_data[_TRACKER_KEY] = task
