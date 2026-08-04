"""
bot2_client.py — Thin, single-purpose client for @Gallery_DLBot (Bot 2).

Public API
----------
- `Bot2Outcome`  : dataclass returned by every wait.
- `send_link`    : DM the gallery URL to Bot 2 and return the send timestamp.
- `wait_for_pdf` : block until Bot 2 replies with a PDF, an error, or the
                   deadline elapses. Returns Bot2Outcome.

Design rules
------------
1. This module knows NOTHING about the Mongo `galleries` collection or the
   database channel. It just talks to Bot 2. The caller (relay.py) glues
   Bot 2's outcome to the rest of the flow.

2. Text messages from Bot 2 are classified into three buckets:
     - progress  → keep polling (e.g. "Queued", "Processing", "Downloading")
     - error     → return Bot2Outcome(kind="text_reply", error_text=<text>)
     - unknown   → treated as progress (safer than false-fail)
   The error-keyword list is exactly V1's `_BOT2_ERROR_KEYWORDS` so behaviour
   for existing edge cases is preserved.

3. PDF detection is by BOTH mime_type == 'application/pdf' AND filename
   suffix '.pdf'. Screenshot samples show Bot 2 always sends
   `<title>.pdf` documents with the correct mime, but this belt+suspenders
   check protects against future filename oddities.

4. All Telethon calls that could raise FloodWaitError bubble up unchanged
   so the caller's `_with_flood` wrapper can back off.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import Message

log = logging.getLogger("bot2_client")

# ---------------------------------------------------------------------------
# Error-keyword vocabulary (kept identical to V1 relay.py for backward compat)
# ---------------------------------------------------------------------------
_BOT2_ERROR_KEYWORDS: Tuple[str, ...] = (
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

# ---------------------------------------------------------------------------
# Outcome dataclass
# ---------------------------------------------------------------------------

OUTCOME_OK          = "ok"
OUTCOME_TEXT_REPLY  = "text_reply"     # Bot 2 gave up (error text or non-PDF doc)
OUTCOME_TIMEOUT     = "timeout"        # deadline elapsed with neither PDF nor error


@dataclass
class Bot2Outcome:
    """Outcome of a wait_for_pdf() call."""
    kind: str                            # OUTCOME_* above
    pdf_message: Optional[Message] = None
    error_text: str = ""                 # populated when kind == 'text_reply'

    @property
    def ok(self) -> bool:
        return self.kind == OUTCOME_OK


# ---------------------------------------------------------------------------
# _classify_text — pure function, testable without a Telethon client
# ---------------------------------------------------------------------------

def _classify_text(text: str) -> str:
    """Return 'error' | 'progress' for a Bot 2 text message.

    Empty text → 'progress' (nothing to act on yet).
    """
    if not text:
        return "progress"
    low = text.lower()
    for kw in _BOT2_ERROR_KEYWORDS:
        if kw in low:
            return "error"
    return "progress"


def _is_pdf_document(msg: Message) -> bool:
    """True if msg carries a PDF document (mime OR filename)."""
    if not getattr(msg, "document", None):
        return False
    mime = (getattr(msg.document, "mime_type", "") or "").lower()
    if "pdf" in mime:
        return True
    for a in (msg.document.attributes or []):
        fname = getattr(a, "file_name", None)
        if fname and str(fname).lower().endswith(".pdf"):
            return True
    return False


def _extract_filename(msg: Message) -> str:
    """Best-effort filename for a document message; empty string if unknown."""
    if not getattr(msg, "document", None):
        return ""
    for a in (msg.document.attributes or []):
        fname = getattr(a, "file_name", None)
        if fname:
            return str(fname)
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def send_link(
    client: TelegramClient,
    bot2_entity,
    url: str,
) -> float:
    """DM `url` to Bot 2 and return the pre-send epoch timestamp.

    The pre-send timestamp is what wait_for_pdf() should compare against;
    it gives us a small safety margin (~1s) so we never miss the reply due
    to clock skew between our host and Telegram's servers.
    """
    since = time.time()
    await client.send_message(bot2_entity, url)
    return since


async def wait_for_pdf(
    client: TelegramClient,
    bot2_entity,
    since_ts: float,
    timeout_sec: int,
    *,
    poll_interval_sec: float = 2.0,
) -> Bot2Outcome:
    """Poll Bot 2's DM until we see a PDF, an error, or the deadline.

    - Ignores our own outbound messages.
    - Ignores messages older than `since_ts - 1` (clock-skew tolerance).
    - Ignores progress-style text ("Queued", "Downloading", "Converting").
    - Returns OK the moment a PDF document arrives.
    - Returns TEXT_REPLY (with `error_text`) the moment Bot 2 sends either
      an error-looking text OR a non-PDF document (Bot 2 sometimes replies
      with a preview PNG when the gallery is unsupported).
    - Returns TIMEOUT if `timeout_sec` elapses without either.
    """
    deadline = time.monotonic() + max(1, int(timeout_sec))
    seen_ids: set = set()

    while time.monotonic() < deadline:
        try:
            async for msg in client.iter_messages(bot2_entity, limit=10):
                if msg.id in seen_ids:
                    break
                seen_ids.add(msg.id)

                # Older than our send → stop scanning this batch.
                if not msg.date or msg.date.timestamp() < since_ts - 1:
                    break

                # Our own outbound DM → skip.
                if msg.out:
                    continue

                # A document arrived — PDF or something else?
                if getattr(msg, "document", None):
                    if _is_pdf_document(msg):
                        return Bot2Outcome(kind=OUTCOME_OK, pdf_message=msg)
                    fname = _extract_filename(msg)
                    mime = (getattr(msg.document, "mime_type", "") or "").lower()
                    log.info(
                        "bot2 sent non-PDF document (mime=%s name=%s) — treating as failure",
                        mime, fname,
                    )
                    return Bot2Outcome(
                        kind=OUTCOME_TEXT_REPLY,
                        error_text=(
                            f"Bot 2 replied with a non-PDF document "
                            f"(mime={mime or 'unknown'}, name={fname or 'unknown'})"
                        )[:500],
                    )

                # Text-only reply — progress or error?
                text = str(getattr(msg, "message", "") or "")
                if text:
                    cls = _classify_text(text)
                    if cls == "error":
                        log.info("bot2 error text: %s", text[:200])
                        return Bot2Outcome(
                            kind=OUTCOME_TEXT_REPLY,
                            error_text=text[:500],
                        )
                    log.debug("bot2 progress text (still waiting): %s", text[:120])
                    # Fall through — keep polling.

        except FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("bot2 poll error (non-fatal): %s", e)

        await asyncio.sleep(poll_interval_sec)

    return Bot2Outcome(kind=OUTCOME_TIMEOUT, error_text="Bot 2 did not reply before the deadline")


# ---------------------------------------------------------------------------
# Helper — a single call that does send + wait, for callers that don't need
# the timestamps separately.
# ---------------------------------------------------------------------------

async def dm_and_wait(
    client: TelegramClient,
    bot2_entity,
    url: str,
    timeout_sec: int,
) -> Bot2Outcome:
    """Convenience wrapper: DM Bot 2 then wait for its PDF."""
    since = await send_link(client, bot2_entity, url)
    return await wait_for_pdf(client, bot2_entity, since, timeout_sec)
