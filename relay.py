"""
relay.py — The per-link flow (Brief §6, §6a, §7, §7a, §7b).

For ONE job:
  1) DM URL to Bot 1 (@hentaifoxbot).
  2) DM URL to Bot 2 (@Gallery_DLBot).
  3) Wait for Bot 1's post in the Database Channel (matcher §7b).
  4) In parallel, wait for Bot 2's PDF reply in DM.
  5) Once both ready, native-forward the PDF into the Database Channel.
  6) Record processed_urls the moment the forward succeeds (§6a).

Fallbacks:
  - No PDF from Bot 2 → retry once → still nothing → 'failed: no PDF'.
  - Bot 2 replies with text/error → 'failed: source error' immediately.
  - Bot 1 post not detected in time → try source_api fallback:
        - if metadata returned: post cover+title+tags ourselves, then forward PDF.
        - if not: forward PDF alone, mark 'partial'.
  - FloodWaitError (§7a): sleep e.seconds + 5, retry SAME job, log flood event.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import Message

import db
from config import settings
from logging_setup import setup_logging
from source_api import fetch_metadata, format_fallback_caption
import hf_scraper
from url_utils import validate_and_parse

log = setup_logging("relay")

# Sentinel result codes returned by _run_once
DONE = "done"
PARTIAL = "partial"
FAILED_NO_PDF = "failed: no PDF"
FAILED_SOURCE = "failed: source error"
FAILED_OTHER = "failed: other"


@dataclass
class JobOutcome:
    status: str                # 'done' | 'partial' | 'failed: ...'
    detail: str = ""


# ------------------------- helpers -------------------------

def _slugs_from_url(url: str) -> Tuple[str, ...]:
    p = validate_and_parse(url)
    return p.slug_candidates if p else ()


def _text_of(msg: Message) -> str:
    """Safely extract searchable text from a Telegram message."""
    parts = []
    if getattr(msg, "message", None):
        parts.append(str(msg.message))
    if getattr(msg, "raw_text", None):
        parts.append(str(msg.raw_text))
    return "\n".join(parts).lower()


def _message_matches_slug(msg: Message, slugs: Tuple[str, ...]) -> bool:
    if not slugs:
        # Nothing to compare against — do NOT accept blindly (§7b: three conditions
        # must all hold). Treat absence of slug candidates as "cannot verify".
        return False
    # Search across every text field Telethon exposes: image captions land in
    # different fields depending on the source (msg.message, msg.text,
    # msg.raw_text). Also search inside any URL entities so we catch slugs
    # that only appear as clickable hyperlinks.
    body_parts = []
    for attr in ("message", "text", "raw_text"):
        v = getattr(msg, attr, None)
        if v:
            body_parts.append(str(v))
    for e in (getattr(msg, "entities", None) or []):
        url = getattr(e, "url", None)
        if url:
            body_parts.append(str(url))
    body = "\n".join(body_parts).lower()
    if not body:
        return False
    return any(s.lower() in body for s in slugs if s)


async def _get_bot1_id(client: TelegramClient) -> int:
    ent = await client.get_entity(settings.bot1_username)
    return int(ent.id)


async def _get_bot2_entity(client: TelegramClient):
    return await client.get_entity(settings.bot2_username)


def _cover_link_from_msg(channel_id: int, msg: Optional[Message]) -> Optional[str]:
    """Build a t.me/c/<chan>/<msg_id> deep link from Bot 1's cover post."""
    if msg is None or not getattr(msg, "id", None):
        return None
    chan = str(channel_id)
    if chan.startswith("-100"):
        chan = chan[4:]
    elif chan.startswith("-"):
        chan = chan[1:]
    return f"https://t.me/c/{chan}/{msg.id}"


def _title_from_bot1_msg(msg: Optional[Message], fallback: str) -> str:
    text = _text_of(msg) if msg is not None else ""
    if text:
        first_line = text.strip().splitlines()[0].strip()
        if first_line:
            return first_line[:80]
    return fallback[:80]


async def _send_mpost(client: TelegramClient, cover_link: Optional[str], conn) -> None:
    """Fire-and-forget: DM @Doujinshibot with /mpost <cover_link>. Never waits
    for a reply and never raises — failures are logged only (§ per Ryan)."""
    if not cover_link:
        log.warning("mpost skipped: no cover_link captured (Bot 1 post link missing)")
        return
    username = getattr(settings, "doujinshibot_username", "") or ""
    if not username:
        log.info("mpost skipped: doujinshibot_username not configured")
        return
    try:
        await _with_flood(
            lambda: client.send_message(username, f"/mpost {cover_link}"),
            context="mpost",
            conn=conn,
        )
        log.info("mpost sent to @%s: /mpost %s", username, cover_link)
    except Exception as e:  # noqa: BLE001
        log.warning("mpost send failed for %s (non-fatal): %s", cover_link, e)


# ------------------------- flood-wait wrapper -------------------------

async def _with_flood(coro_factory, *, context: str, conn):
    """Run an awaitable, retrying on FloodWaitError forever (§7a)."""
    while True:
        try:
            return await coro_factory()
        except FloodWaitError as e:
            secs = int(getattr(e, "seconds", 0)) + 5
            log.warning("FloodWait in %s: sleeping %ss", context, secs)
            db.log_flood(conn, secs, context)
            await asyncio.sleep(secs)


# ------------------------- Bot 1 post matcher (§7b) -------------------------

async def _wait_bot1_post(
    client: TelegramClient,
    bot1_id: int,
    channel_id: int,
    since_ts: float,
    slugs: Tuple[str, ...],
    timeout_sec: int,
) -> Optional[Message]:
    """Poll the Database Channel for a matching post from Bot 1.

    All three must hold (§7b):
      - sender_id == bot1_id
      - message date >= since_ts
      - text contains one of the URL-derived slugs
    """
    deadline = time.monotonic() + timeout_sec
    last_seen_id = 0
    while time.monotonic() < deadline:
        try:
            # Iterate the most recent 20 messages, newest first.
            async for msg in client.iter_messages(channel_id, limit=20):
                if msg.id <= last_seen_id:
                    break
                if not msg.date:
                    continue
                if msg.date.timestamp() < since_ts - 1:
                    # Older than our marker — we can stop scanning further back.
                    break
                sender_id = getattr(msg, "sender_id", None) or getattr(msg, "from_id", None)
                # from_id can be a Peer object; normalise via sender_id when possible
                if sender_id is None and getattr(msg, "sender", None):
                    sender_id = getattr(msg.sender, "id", None)
                # Bot 1 posts through an admin identity that Telegram signs as
                # the channel itself, so the sender id we see is the channel's,
                # not the bot's. Accept either. Also accept +/- variants because
                # Telethon sometimes strips the -100 prefix on internal ids.
                allowed_senders = {
                    bot1_id,
                    channel_id,
                    -channel_id if isinstance(channel_id, int) else channel_id,
                    abs(channel_id) if isinstance(channel_id, int) else channel_id,
                }
                if sender_id not in allowed_senders:
                    continue
                if _message_matches_slug(msg, slugs):
                    return msg
            # Track the newest id we saw so subsequent polls do less work
            newest = await client.get_messages(channel_id, limit=1)
            if newest:
                last_seen_id = max(last_seen_id, newest[0].id)
        except FloodWaitError as e:
            # Propagate to outer handler
            raise e
        except Exception as e:  # noqa: BLE001
            log.debug("bot1 poll error (non-fatal): %s", e)
        await asyncio.sleep(2.0)
    return None


# ------------------------- Bot 2 PDF waiter -------------------------

# Words that, when they appear in a Bot 2 text reply, mean "give up, this URL
# will never produce a PDF". Anything else (Queued, Downloading, Converting,
# Processing, etc.) is treated as a progress update — keep waiting for the
# actual PDF file to arrive.
_BOT2_ERROR_KEYWORDS = (
    "error",
    "failed",
    "invalid",
    "not found",
    "unable",
    "cannot",
    "unsupported",
    "rejected",
    "forbidden",
    "denied",
    "banned",
    "blocked",
    "timeout",
    "expired",
    "no images found",
    "no gallery",
    "gallery not",
    "does not exist",
    "doesn't exist",
    "bad url",
    "bad link",
    "not a gallery",
)


def _looks_like_error(text: str) -> bool:
    """True if a Bot 2 text message looks like a hard failure, not progress."""
    if not text:
        return False
    low = text.lower()
    return any(kw in low for kw in _BOT2_ERROR_KEYWORDS)


async def _wait_bot2_pdf(
    client: TelegramClient,
    bot2_entity,
    since_ts: float,
    timeout_sec: int,
) -> Tuple[Optional[Message], str]:
    """Return (pdf_message, note).
       note in {'ok','text_reply','timeout'}.
       - 'ok'         : a PDF arrived from Bot 2.
       - 'text_reply' : Bot 2 sent a message whose text contains error keywords
                        (see _BOT2_ERROR_KEYWORDS). Treat as source error per §7.
       - 'timeout'    : deadline reached with neither PDF nor error message.
       Progress messages ("Queued", "Downloading...", "Converting to PDF") are
       IGNORED and we keep polling until the deadline or the PDF appears.
    """
    deadline = time.monotonic() + timeout_sec
    seen_ids: set = set()
    while time.monotonic() < deadline:
        try:
            async for msg in client.iter_messages(bot2_entity, limit=10):
                if msg.id in seen_ids:
                    break
                seen_ids.add(msg.id)
                if not msg.date or msg.date.timestamp() < since_ts - 1:
                    break
                # Only consider inbound messages from Bot 2, not our own outbound DM
                if msg.out:
                    continue
                if msg.document:
                    fname = ""
                    for a in (msg.document.attributes or []):
                        if hasattr(a, "file_name") and getattr(a, "file_name", None):
                            fname = str(a.file_name).lower()
                            break
                    mime = (getattr(msg.document, "mime_type", "") or "").lower()
                    if "pdf" in mime or fname.endswith(".pdf"):
                        return msg, "ok"
                    log.info("bot2 sent non-PDF document (mime=%s name=%s)", mime, fname)
                    return None, "text_reply"
                # Text-only reply from Bot 2: could be progress or an error.
                if msg.message:
                    txt = str(msg.message)
                    if _looks_like_error(txt):
                        log.info("bot2 text reply looks like error: %s", txt[:200])
                        return None, "text_reply"
                    else:
                        log.info("bot2 progress text (ignored, still waiting): %s", txt[:120])
                        # Fall through — keep polling.
        except FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("bot2 poll error (non-fatal): %s", e)
        await asyncio.sleep(2.0)
    return None, "timeout"


# ------------------------- Main entry -------------------------

async def process_job(client: TelegramClient, url: str, url_hash: str, job_id: Optional[int] = None, via_search: bool = False, username: Optional[str] = None, mpost_enabled: bool = False) -> JobOutcome:
    """Execute the full per-link flow once, with §7a flood-wait retries wrapped
    around each Telegram call."""
    conn = db.connect()
    try:
        # Idempotency check (§6a): if URL already in processed_urls, skip.
        if db.has_completed(conn, url_hash):
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_DONE, detail="already processed")
            return JobOutcome(DONE, "already in processed_urls")

        if job_id is not None:
            db.upsert_job_progress(conn, job_id, db.PHASE_PENDING, title=url)

        slugs = _slugs_from_url(url)
        bot1_id = await _with_flood(lambda: _get_bot1_id(client), context="resolve_bot1", conn=conn)
        bot2 = await _with_flood(lambda: _get_bot2_entity(client), context="resolve_bot2", conn=conn)
        channel = await _with_flood(
            lambda: client.get_entity(settings.database_channel_id),
            context="resolve_channel",
            conn=conn,
        )

        # Capture the timestamp BEFORE we DM Bot 1 (§7b condition 2)
        since_ts = time.time()

        # 1) DM Bot 1
        # v11 (bonus E): when the job came from a /search Confirm, prefix the
        # requester's @handle so Bot 1's cover-post caption mentions them.
        # Plain URL drops from admins are unchanged.
        bot1_payload = url
        if via_search and username:
            handle = username if username.startswith("@") else f"@{username}"
            bot1_payload = f"{handle} {url}"
        await _with_flood(
            lambda: client.send_message(settings.bot1_username, bot1_payload),
            context="dm_bot1",
            conn=conn,
        )
        db.touch_bot_ping(conn, "bot1")

        # 2) DM Bot 2
        await _with_flood(
            lambda: client.send_message(bot2, url),
            context="dm_bot2",
            conn=conn,
        )
        db.touch_bot_ping(conn, "bot2")

        if job_id is not None:
            db.upsert_job_progress(conn, job_id, db.PHASE_SENT_BOTS, detail="sent to Bot 1 & Bot 2")
            db.upsert_job_progress(conn, job_id, db.PHASE_WAIT_PDF, detail="waiting for PDF from Bot 2")

        # 3 + 4) Wait for Bot 1 post and Bot 2 PDF concurrently
        bot1_task = asyncio.create_task(
            _wait_bot1_post(
                client, bot1_id, settings.database_channel_id,
                since_ts, slugs, settings.bot1_post_timeout_sec,
            )
        )
        bot2_task = asyncio.create_task(
            _wait_bot2_pdf(client, bot2, since_ts, settings.bot2_pdf_timeout_sec)
        )

        try:
            bot1_msg, (bot2_msg, bot2_note) = await asyncio.gather(bot1_task, bot2_task)
        except FloodWaitError as e:
            # If a flood-wait bubbles here, sleep and retry the SAME job (§7a).
            secs = int(getattr(e, "seconds", 0)) + 5
            log.warning("FloodWait during wait phase: sleeping %ss", secs)
            db.log_flood(conn, secs, "wait_phase")
            await asyncio.sleep(secs)
            return await process_job(client, url, url_hash, job_id=job_id, via_search=via_search, username=username, mpost_enabled=mpost_enabled)  # retry same job

        # ---- Bot 2 handling first (defines whether we have a PDF at all) ----
        if bot2_note == "text_reply":
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_FAILED, detail="Bot 2 error, no PDF")
            return JobOutcome(FAILED_SOURCE, "Bot 2 replied with text/error, no PDF")

        if bot2_msg is None:
            # Retry once (§7)
            log.info("Bot 2 no PDF within timeout; retrying once")
            since_ts2 = time.time()
            await _with_flood(
                lambda: client.send_message(bot2, url),
                context="dm_bot2_retry",
                conn=conn,
            )
            bot2_msg, bot2_note = await _wait_bot2_pdf(
                client, bot2, since_ts2, settings.bot2_pdf_timeout_sec
            )
            if bot2_note == "text_reply":
                if job_id is not None:
                    db.upsert_job_progress(conn, job_id, db.PHASE_FAILED, detail="Bot 2 error on retry")
                return JobOutcome(FAILED_SOURCE, "Bot 2 replied with text/error on retry")
            if bot2_msg is None:
                if job_id is not None:
                    db.upsert_job_progress(conn, job_id, db.PHASE_FAILED, detail="no PDF after retry")
                return JobOutcome(FAILED_NO_PDF, "Bot 2 did not deliver PDF after retry")

        # ---- Bot 1 branch ----
        # If Bot 1's cover post never appeared, try the fallback API. Otherwise
        # proceed with the native forward — it will land under the cover.
        if bot1_msg is None:
            # Tier 1: Ryan's source API (if SOURCE_API_BASE/KEY are configured).
            meta = await fetch_metadata(url)
            # Tier 2: direct hentaifox.com scraper — no API key required, works
            # today. Only attempted for hentaifox URLs (hf_scraper is site-specific).
            if meta is None and "hentaifox.com" in url.lower():
                try:
                    meta = await hf_scraper.fetch_gallery_meta(url)
                    if meta is not None:
                        log.info("fallback metadata via hf_scraper for %s", url)
                except Exception as e:  # noqa: BLE001
                    log.warning("hf_scraper fallback failed: %s", e)
            if meta is not None:
                # Post our own cover+title+tags first
                caption = format_fallback_caption(meta)
                fallback_cover_msg = None
                try:
                    if meta.cover_url:
                        fallback_cover_msg = await _with_flood(
                            lambda: client.send_file(
                                channel, meta.cover_url, caption=caption
                            ),
                            context="fallback_send_cover",
                            conn=conn,
                        )
                    else:
                        fallback_cover_msg = await _with_flood(
                            lambda: client.send_message(channel, caption),
                            context="fallback_send_text",
                            conn=conn,
                        )
                except Exception as e:  # noqa: BLE001
                    log.warning("fallback cover post failed: %s", e)

                if job_id is not None:
                    db.upsert_job_progress(
                        conn, job_id, db.PHASE_FORWARDING,
                        title=_title_from_bot1_msg(None, meta.title if meta else url),
                        detail="forwarding PDF to channel",
                    )

                # Forward the PDF underneath
                await _with_flood(
                    lambda: client.forward_messages(channel, bot2_msg, drop_author=True),
                    context="forward_pdf_after_fallback",
                    conn=conn,
                )
                # Point of no return — record BEFORE returning success (§6a)
                db.record_processed(conn, url, url_hash)

                fallback_cover_link = _cover_link_from_msg(settings.database_channel_id, fallback_cover_msg)
                if fallback_cover_link and job_id is not None:
                    db.set_cover_link(conn, job_id, fallback_cover_link)
                if mpost_enabled:
                    if job_id is not None:
                        db.upsert_job_progress(conn, job_id, db.PHASE_MPOSTING, detail="sending /mpost")
                    await _send_mpost(client, fallback_cover_link, conn)
                if job_id is not None:
                    db.upsert_job_progress(conn, job_id, db.PHASE_DONE, detail="posted ✅")
                return JobOutcome(DONE, "Bot 1 missed; fallback cover posted + PDF forwarded")

            # No fallback available — forward PDF alone, mark partial (§7)
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_FORWARDING, detail="forwarding PDF alone")
            await _with_flood(
                lambda: client.forward_messages(channel, bot2_msg, drop_author=True),
                context="forward_pdf_partial",
                conn=conn,
            )
            db.record_processed(conn, url, url_hash)
            if job_id is not None:
                # No cover post exists at all — nothing to /mpost.
                db.upsert_job_progress(conn, job_id, db.PHASE_PARTIAL, detail="PDF forwarded alone; no cover post")
            return JobOutcome(PARTIAL, "Bot 1 post not detected; PDF forwarded alone")

        # Happy path: both ready → native forward directly under Bot 1's post.
        title_now = _title_from_bot1_msg(bot1_msg, url)
        if job_id is not None:
            db.upsert_job_progress(conn, job_id, db.PHASE_FORWARDING, title=title_now, detail="forwarding PDF to channel")
        await _with_flood(
            lambda: client.forward_messages(channel, bot2_msg, drop_author=True),
            context="forward_pdf",
            conn=conn,
        )
        db.record_processed(conn, url, url_hash)

        cover_link = _cover_link_from_msg(settings.database_channel_id, bot1_msg)
        if cover_link and job_id is not None:
            db.set_cover_link(conn, job_id, cover_link)
        if mpost_enabled:
            if job_id is not None:
                db.upsert_job_progress(conn, job_id, db.PHASE_MPOSTING, title=title_now, detail="sending /mpost")
            await _send_mpost(client, cover_link, conn)
        if job_id is not None:
            db.upsert_job_progress(conn, job_id, db.PHASE_DONE, title=title_now, detail="posted ✅")
        return JobOutcome(DONE, "cover + PDF posted")
    finally:
        conn.close()
