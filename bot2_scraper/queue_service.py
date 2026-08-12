"""
queue_service.py — Thin business-logic wrapper around db.py used by the Admin Bot.

Rules (Brief §9):
- Enqueue only URLs that are not already completed (per processed_urls),
  not already pending/processing, and unique within the current batch.
- Return a structured result the Admin Bot turns into a reply.

MIGRATION NOTE (SQLite → MongoDB)
---------------------------------
This module never spoke SQL directly — it only ever called helpers in db.py.
Because the new MongoDB db.py keeps every function name and signature
identical, the enqueue logic below is unchanged in behaviour.

Two things WERE improved for the serverless deployment:

  1. `db.connect()` is now a cheap handle over a shared connection pool, so
     the connect/close pattern is still correct and no longer costly.

  2. Network calls can fail in ways a local file never could (Atlas hiccup,
     cold start, DNS blip). A single dropped URL no longer aborts the whole
     batch — it is reported back to the admin in `rejected` with the reason,
     and the remaining URLs still get queued.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

import db
from url_utils import BatchParseResult, ParsedGalleryURL, parse_batch


@dataclass
class EnqueueResult:
    queued: List[Tuple[int, str]] = field(default_factory=list)          # (job_id, url)
    rejected: List[Tuple[str, str]] = field(default_factory=list)         # (line, reason)
    skipped_duplicates: List[str] = field(default_factory=list)           # normalised URLs
    skipped_already_pending: List[str] = field(default_factory=list)
    skipped_already_done: List[str] = field(default_factory=list)

    def summary_line(self) -> str:
        n = len(self.queued)
        m = len(self.rejected)
        k = (
            len(self.skipped_duplicates)
            + len(self.skipped_already_pending)
            + len(self.skipped_already_done)
        )
        return f"{n} queued, {m} rejected, {k} skipped as duplicates"

    def detail_lines(self) -> List[str]:
        lines: List[str] = []
        if self.rejected:
            lines.append("Rejected:")
            for raw, why in self.rejected:
                lines.append(f"  • {raw} — {why}")
        if self.skipped_already_pending:
            lines.append("Skipped (already pending/processing):")
            for u in self.skipped_already_pending:
                lines.append(f"  • {u}")
        if self.skipped_already_done:
            lines.append("Skipped (already completed):")
            for u in self.skipped_already_done:
                lines.append(f"  • {u}")
        if self.skipped_duplicates:
            lines.append("Skipped (duplicate within this /fetch):")
            for u in self.skipped_duplicates:
                lines.append(f"  • {u}")
        return lines


def enqueue_batch(
    text: str,
    max_links: int,
    via_search: bool = False,
    submitted_by: int | None = None,
    username: str | None = None,
    chat_id: int | None = None,
) -> EnqueueResult:
    """Enqueue every URL parsed from `text`.

    v11 additions:
    - `via_search=True` marks jobs coming from an interactive /search Confirm.
      The relay uses it to inject `@username [link]` into Bot 1's DM caption
      (so the cover post credits the requester).
    - `submitted_by` / `username` record who requested the job (for /alltoken
      and for the caption prefix). They are also what worker.py inspects to
      decide whether to fire the Bot 3 /mpost cross-post:
        via_search AND submitter is NOT an admin  → send /mpost
        anything else                              → skip /mpost
    """
    batch: BatchParseResult = parse_batch(text, max_links=max_links)
    res = EnqueueResult(
        rejected=list(batch.rejected),
        skipped_duplicates=list(batch.duplicates_in_batch),
    )

    try:
        conn = db.connect()
    except Exception as e:  # noqa: BLE001 — MONGO_URI missing / cluster unreachable
        for p in batch.accepted:
            res.rejected.append((p.normalised, f"database unavailable: {e!s}"[:180]))
        return res

    try:
        for p in batch.accepted:  # type: ParsedGalleryURL
            try:
                if db.has_completed(conn, p.url_hash):
                    res.skipped_already_done.append(p.normalised)
                    continue
                if db.has_pending_or_processing(conn, p.url_hash):
                    res.skipped_already_pending.append(p.normalised)
                    continue
                job_id = db.enqueue(
                    conn, p.normalised, p.url_hash,
                    submitted_by=submitted_by,
                    chat_id=chat_id,
                    via_search=via_search,
                    username=username,
                )
                res.queued.append((job_id, p.normalised))
            except Exception as e:  # noqa: BLE001
                # One bad URL must not sink the whole batch.
                res.rejected.append((p.normalised, f"db error: {e!s}"[:180]))
    finally:
        conn.close()
    return res
