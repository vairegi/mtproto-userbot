"""
worker.py — Main loop for the userbot worker process.

- Runs startup self-test (§16).
- Resets any 'processing' → 'pending' on startup (§8).
- Consumes queue one job at a time.
- Between jobs: random 20–60s delay (§14 default, configurable).
- Emits a per-batch summary via the Admin Bot when the queue drains
  after having been non-empty (§7).
- If pause flag is set, waits and rechecks every 5s.
- If session becomes invalid at runtime, alerts Ryan and exits non-zero (§7).
"""
from __future__ import annotations

import asyncio
import os
import random
import signal
import sys
from typing import Optional

import httpx
from telethon.errors import AuthKeyError, UnauthorizedError

import db
from config import settings
from logging_setup import setup_logging
# v12.31: relay.py (V1) removed — the legacy Bot 1 (@postedstuffbot) flow
# has been off in production (SELF_COVER_POST_ENABLED defaults to 1) and the
# extra supervised process contributed ~90-130 MB of resident RSS that pushed
# the 512 MB Render free tier into OOM. process_job now always routes to
# relay_v2.
import relay_v2 as _relay_v2
from startup_check import run_checks
from userbot import build_client

# v12.4: background prefetch cron. Import is guarded so a broken
# mini-app tree (e.g. missing libsql-client in a stripped test env)
# never blocks the worker from booting. If the import fails we simply
# skip spawning the sweep — the mini-app fetches on-demand as before.
try:
    from miniapp.backend.app.services import prefetch_cron as _prefetch_cron
except Exception as _e:  # noqa: BLE001
    _prefetch_cron = None
    _prefetch_cron_import_err = _e
else:
    _prefetch_cron_import_err = None

# v12.10 (#1): dedup_cron — same fail-open contract as prefetch_cron.
# A broken dedup module must NEVER stop the worker from booting.
try:
    from miniapp.backend.app.services import dedup_cron as _dedup_cron
except Exception as _e_dd:  # noqa: BLE001
    _dedup_cron = None
    _dedup_cron_import_err = _e_dd
else:
    _dedup_cron_import_err = None

# v12.11 (#1): details_prefetch_cron — background scraper that hydrates
# per-gallery DETAILS (artist, tags, pages, ...) into Turso under the
# same `gallery:<id>` key the detail route reads. Same fail-open contract.
try:
    from miniapp.backend.app.services import details_prefetch_cron as _details_cron
except Exception as _e_dp:  # noqa: BLE001
    _details_cron = None
    _details_cron_import_err = _e_dp
else:
    _details_cron_import_err = None


async def process_job(*args, **kwargs):
    """Thin passthrough to relay_v2.process_job.

    v12.31: the V1/V2 router was removed together with relay.py. Keeping
    the wrapper name `process_job` means the rest of worker.py (log lines,
    tests, call sites) is unchanged. SELF_COVER_POST_ENABLED is now
    advisory only — the code always uses V2.
    """
    return await _relay_v2.process_job(*args, **kwargs)

log = setup_logging("worker")

_stop = asyncio.Event()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop.set)
        except NotImplementedError:
            pass


async def _notify_admin(text: str) -> None:
    if not settings.admin_bot_token or not settings.admin_user_id:
        return
    url = f"https://api.telegram.org/bot{settings.admin_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={"chat_id": settings.admin_user_id, "text": text})
    except Exception as e:  # noqa: BLE001
        log.warning("admin notify failed: %s", e)


async def _random_delay() -> None:
    lo = max(0, int(settings.inter_job_delay_min))
    hi = max(lo, int(settings.inter_job_delay_max))
    d = random.randint(lo, hi) if hi > lo else lo
    log.info("inter-job delay: %ss", d)
    for _ in range(d):
        if _stop.is_set():
            return
        await asyncio.sleep(1)


async def _is_paused() -> bool:
    conn = db.connect()
    try:
        return db.get_flag(conn, "paused", "0") == "1"
    finally:
        conn.close()


async def _run_loop() -> int:
    client = build_client()
    await client.connect()

    if not await client.is_user_authorized():
        await _notify_admin("❌ Userbot session invalid at startup. Regenerate with scripts/gen_session.py.")
        log.error("userbot session not authorised — exiting")
        return 3

    # Reset stuck jobs (§8)
    conn = db.connect()
    try:
        n = db.reset_stuck_processing(conn)
        if n:
            log.warning("Reset %d stuck 'processing' jobs → 'pending'", n)
    finally:
        conn.close()

    # Batch-summary bookkeeping: we consider a "batch" the stretch of consecutive
    # non-empty polls. When the queue drains, we send a summary of the counts
    # accumulated since the batch started.
    batch_done = 0
    batch_partial = 0
    batch_failed: list = []       # list[(url, reason)]
    batch_active = False

    # v12.4: spawn the Turso cache warmer as a background task. It runs
    # in the same event loop as the worker so a bucket-exhaustion event
    # from user traffic is immediately visible to the sweep (no IPC).
    # A crash inside run_forever() is swallowed by prefetch_cron itself
    # — see rule set: the mini-app never goes down because Turso is off.
    if _prefetch_cron is not None:
        try:
            asyncio.create_task(
                _prefetch_cron.run_forever(),
                name="prefetch_cron",
            )
            log.info(
                "prefetch_cron: spawned (interval=%ss, max_pages=%s, enabled=%s)",
                _prefetch_cron.PREFETCH_INTERVAL_SEC,
                _prefetch_cron.PREFETCH_MAX_PAGES,
                _prefetch_cron._enabled(),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("prefetch_cron: spawn failed (%s) — continuing without warmer", e)
    else:
        log.warning(
            "prefetch_cron: not spawned — import failed at boot (%s)",
            _prefetch_cron_import_err,
        )

    # v12.10 (#1): dedup_cron background sweep (every 12 h by default).
    # Spawned right after prefetch_cron so a dedup import failure never
    # affects the warmer. Alerts the admin via Telegram when duplicates
    # were removed OR when a Turso failure needs to be disclosed.
    if _dedup_cron is not None:
        try:
            asyncio.create_task(
                _dedup_cron.run_forever(),
                name="dedup_cron",
            )
            log.info(
                "dedup_cron: spawned (interval=%ss, enabled=%s, alerts=%s)",
                _dedup_cron.DEDUP_INTERVAL_SEC,
                _dedup_cron.DEDUP_ENABLED,
                _dedup_cron.DEDUP_ALERT_ENABLED,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("dedup_cron: spawn failed (%s) — continuing without dedup", e)
    else:
        log.warning(
            "dedup_cron: not spawned — import failed at boot (%s)",
            _dedup_cron_import_err,
        )

    # v12.11 (#1): details_prefetch_cron — walks every cached search page
    # (popular-today first, then date / popular-week / popular) and
    # scrapes DETAILS for each card into Turso under gallery:<id>. The
    # detail endpoint then serves a straight cache hit instead of an
    # upstream fetch. Pauses itself during the day window when a
    # non-admin user is active; admin activity never pauses it.
    if _details_cron is not None:
        try:
            asyncio.create_task(
                _details_cron.run_forever(),
                name="details_prefetch_cron",
            )
            log.info(
                "details_prefetch_cron: spawned (night=%s-%s IST, day_tick=%ss, night_tick=%ss, enabled=%s)",
                _details_cron.NIGHT_START,
                _details_cron.NIGHT_END,
                _details_cron.DAY_TICK_SEC,
                _details_cron.NIGHT_TICK_SEC,
                _details_cron.ENABLED,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("details_prefetch_cron: spawn failed (%s) — continuing without scraper", e)
    else:
        log.warning(
            "details_prefetch_cron: not spawned — import failed at boot (%s)",
            _details_cron_import_err,
        )

    log.info("worker started, entering main loop")
    while not _stop.is_set():
        # Paused? sleep and re-check
        if await _is_paused():
            await asyncio.sleep(5)
            continue

        conn = db.connect()
        try:
            row = db.next_pending(conn)
        finally:
            conn.close()

        if row is None:
            # Queue drained — if we were in a batch, emit summary.
            if batch_active:
                lines = [
                    "Batch summary:",
                    f"  ✅ done: {batch_done}",
                    f"  ⚠️  partial: {batch_partial}",
                    f"  ❌ failed: {len(batch_failed)}",
                ]
                if batch_failed:
                    lines.append("  failed reasons:")
                    for u, r in batch_failed[:25]:
                        lines.append(f"   • {u}  —  {r}")
                await _notify_admin("\n".join(lines))
                batch_done = batch_partial = 0
                batch_failed = []
                batch_active = False
            await asyncio.sleep(3)
            continue

        job_id = int(row["id"])
        url = row["url"]
        url_hash = row["url_hash"]
        # v11: pull /search context from the queue row so we can inject the
        # "@username <url>" caption prefix into Bot 1's DM.
        via_search = bool(row["via_search"]) if "via_search" in row.keys() else False
        username = row["username"] if "username" in row.keys() else None
        submitted_by = row["submitted_by"] if "submitted_by" in row.keys() else None

        # v11 routing rule: Bot 3 /mpost fires ONLY for /search Confirms made
        # by regular (non-admin) users. Admin /search confirms and every URL
        # drop (which are admin-only anyway) skip /mpost entirely.
        submitter_is_admin = False
        if submitted_by:
            conn = db.connect()
            try:
                submitter_is_admin = (
                    int(submitted_by) == int(settings.admin_user_id)
                    or db.get_admin(conn, int(submitted_by)) is not None
                )
            finally:
                conn.close()
        mpost_enabled = bool(via_search and submitted_by and not submitter_is_admin)

        # Mark processing
        conn = db.connect()
        try:
            db.mark_processing(conn, job_id)
        finally:
            conn.close()

        batch_active = True
        log.info(
            "job %s START %s (via_search=%s user=%s admin=%s mpost=%s)",
            job_id, url, via_search, submitted_by, submitter_is_admin, mpost_enabled,
        )

        try:
            outcome = await process_job(
                client, url, url_hash, job_id=job_id,
                via_search=via_search, username=username,
                mpost_enabled=mpost_enabled,
                submitted_by=submitted_by,
            )
        except (AuthKeyError, UnauthorizedError) as e:
            await _notify_admin(
                f"❌ Userbot session became invalid mid-run: {e!s}\n"
                "Regenerate with scripts/gen_session.py and restart the worker."
            )
            log.error("session invalid mid-run: %s", e)
            return 4
        except Exception as e:  # noqa: BLE001
            log.exception("job %s crashed", job_id)
            conn = db.connect()
            try:
                db.mark_status(conn, job_id, "failed", f"crash: {e!s}"[:500])
                db.upsert_job_progress(conn, job_id, db.PHASE_FAILED, detail=f"crash: {e!s}"[:200])
                # v11 (Q2b): refund token if this /search job crashed outright.
                # Only regular users spend tokens, so this is a no-op for admins.
                if via_search and submitted_by and not submitter_is_admin:
                    db.refund_token(conn, int(submitted_by), 1)
                    log.info("job %s refunded 1 token to user_id=%s (crash)", job_id, submitted_by)
            finally:
                conn.close()
            batch_failed.append((url, f"crash: {e!s}"[:200]))
            await _random_delay()
            continue

        # Apply outcome
        conn = db.connect()
        try:
            if outcome.status == "done":
                db.mark_status(conn, job_id, "done", None)
                batch_done += 1
            elif outcome.status == "partial":
                db.mark_status(conn, job_id, "partial", outcome.detail[:500])
                batch_partial += 1
            else:
                db.mark_status(conn, job_id, "failed", outcome.detail[:500])
                batch_failed.append((url, outcome.detail[:200]))
                # v11 (Q2b): refund on failed (but NOT partial — partial still
                # delivered a PDF so the user got value).
                if via_search and submitted_by and not submitter_is_admin:
                    db.refund_token(conn, int(submitted_by), 1)
                    log.info("job %s refunded 1 token to user_id=%s (failed)", job_id, submitted_by)
            # Safety net: force job_progress to a terminal phase.
            current = db.get_progress_for_jobs(conn, [job_id])
            phase_now = (current[0]["phase"] if current else None)
            if phase_now not in (db.PHASE_DONE, db.PHASE_FAILED, db.PHASE_PARTIAL):
                terminal_phase = {
                    "done": db.PHASE_DONE,
                    "partial": db.PHASE_PARTIAL,
                }.get(outcome.status, db.PHASE_FAILED)
                db.upsert_job_progress(conn, job_id, terminal_phase, detail=outcome.detail[:200])
        finally:
            conn.close()

        log.info("job %s END status=%s detail=%s", job_id, outcome.status, outcome.detail)
        await _random_delay()

    log.info("worker stopping")
    await client.disconnect()
    return 0


async def _main() -> int:
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop)

    # Startup self-test (§16)
    code = await run_checks()
    if code != 0:
        log.error("startup self-test failed (code %s) — exiting", code)
        return code

    return await _run_loop()


def main() -> int:
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
