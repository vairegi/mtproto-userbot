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
# v12.33: single-userbot bootstrap replaced by UserbotPool. build_client is
# still used by userbot.py's one-shot session self-check (start.sh step 1b);
# the resident worker process now owns N Telethon clients via the pool.
from userbot_pool import UserbotPool, set_global as _set_pool_global

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


async def _run_one_job(
    pool: UserbotPool,
    job_id: int,
    url: str,
    url_hash: str,
    *,
    via_search: bool,
    username,
    mpost_enabled: bool,
    submitted_by,
    submitter_is_admin: bool,
    batch: dict,
    fatal: asyncio.Event,
    fatal_reason: list,
) -> None:
    """v12.33b: one queued download, executed as its own asyncio.Task.

    Holds a pool slot for the whole job; applies the outcome (status,
    refund, progress terminal phase, batch counters) exactly as the
    v12.32 serial loop did. Auth/session failures set `fatal` so the
    dispatcher exits with code 4 after draining in-flight tasks.
    Never raises outward — the dispatcher reaps tasks with t.result().
    """
    from userbot_pool import NoUserbotAvailable
    try:
        async with pool.acquire() as slot:
            log.info("job %s dispatched to userbot slot %d", job_id, slot.index)
            outcome = await process_job(
                slot.client, url, url_hash, job_id=job_id,
                via_search=via_search, username=username,
                mpost_enabled=mpost_enabled,
                submitted_by=submitted_by,
            )
    except NoUserbotAvailable as e:
        # Every slot started cooling between the dispatcher's health gate
        # and our acquire. Re-queue; the health gate will hold the
        # dispatcher until a slot recovers.
        log.warning("job %s: %s — requeue", job_id, e)
        conn = db.connect()
        try:
            db.mark_status(conn, job_id, "pending",
                           "all userbots cooling; retrying")
        finally:
            conn.close()
        return
    except (AuthKeyError, UnauthorizedError) as e:
        await _notify_admin(
            f"❌ Userbot session became invalid mid-run: {e!s}\n"
            "Regenerate with scripts/gen_session.py and restart the worker."
        )
        log.error("session invalid mid-run: %s", e)
        fatal_reason.append(str(e))
        fatal.set()
        return
    except Exception as e:  # noqa: BLE001
        log.exception("job %s crashed", job_id)
        conn = db.connect()
        try:
            db.mark_status(conn, job_id, "failed", f"crash: {e!s}"[:500])
            db.upsert_job_progress(conn, job_id, db.PHASE_FAILED,
                                   detail=f"crash: {e!s}"[:200])
            # v11 (Q2b): refund token if this /search job crashed outright.
            if via_search and submitted_by and not submitter_is_admin:
                db.refund_token(conn, int(submitted_by), 1)
                log.info("job %s refunded 1 token to user_id=%s (crash)",
                         job_id, submitted_by)
        finally:
            conn.close()
        batch["failed"].append((url, f"crash: {e!s}"[:200]))
        return

    # Apply outcome
    conn = db.connect()
    try:
        if outcome.status == "done":
            db.mark_status(conn, job_id, "done", None)
            batch["done"] += 1
        elif outcome.status == "partial":
            db.mark_status(conn, job_id, "partial", outcome.detail[:500])
            batch["partial"] += 1
        else:
            db.mark_status(conn, job_id, "failed", outcome.detail[:500])
            batch["failed"].append((url, outcome.detail[:200]))
            # v11 (Q2b): refund on failed (but NOT partial — partial still
            # delivered a PDF so the user got value).
            if via_search and submitted_by and not submitter_is_admin:
                db.refund_token(conn, int(submitted_by), 1)
                log.info("job %s refunded 1 token to user_id=%s (failed)",
                         job_id, submitted_by)
        # Safety net: force job_progress to a terminal phase.
        current = db.get_progress_for_jobs(conn, [job_id])
        phase_now = (current[0]["phase"] if current else None)
        if phase_now not in (db.PHASE_DONE, db.PHASE_FAILED, db.PHASE_PARTIAL):
            terminal_phase = {
                "done": db.PHASE_DONE,
                "partial": db.PHASE_PARTIAL,
            }.get(outcome.status, db.PHASE_FAILED)
            db.upsert_job_progress(conn, job_id, terminal_phase,
                                   detail=outcome.detail[:200])
    finally:
        conn.close()

    log.info("job %s END status=%s detail=%s",
             job_id, outcome.status, outcome.detail)


async def _run_loop() -> int:
    # v12.33: build the N-userbot pool from env. Slot 1 uses the legacy
    # STRING_SESSION (unchanged env), slot 2 uses STRING_SESSION_2 (same
    # API_ID / API_HASH — both userbots belong to the same Telegram dev
    # app per the v12.33 briefing). Any slot with a missing session is
    # silently skipped so a solo-userbot deploy still works.
    try:
        pool = UserbotPool.from_env()
    except Exception as e:  # noqa: BLE001
        await _notify_admin(
            f"❌ UserbotPool build failed: {e!s}\n"
            "Check STRING_SESSION (slot 1) and STRING_SESSION_2 (slot 2) in Render env."
        )
        log.error("UserbotPool.from_env failed: %s", e)
        return 3

    try:
        await pool.start()
    except (AuthKeyError, UnauthorizedError) as e:
        await _notify_admin(
            f"❌ Userbot session invalid at startup: {e!s}\n"
            "Regenerate with scripts/gen_session.py."
        )
        log.error("userbot session not authorised — exiting: %s", e)
        return 3
    except Exception as e:  # noqa: BLE001
        await _notify_admin(f"❌ UserbotPool start failed: {e!s}")
        log.error("pool.start failed: %s", e)
        return 3

    _set_pool_global(pool)
    log.info("v12.33: userbot pool ready with %d slot(s)", len(pool.slots))

    # Reset stuck jobs (§8)
    conn = db.connect()
    try:
        n = db.reset_stuck_processing(conn)
        if n:
            log.warning("Reset %d stuck 'processing' jobs → 'pending'", n)
    finally:
        conn.close()

    # v12.33b batch bookkeeping: shared dict mutated by in-flight job
    # tasks. Single event loop ⇒ plain dict is safe (no locks needed).
    batch: dict = {"done": 0, "partial": 0, "failed": []}

    # v12.33b: concurrent dispatch state. The first v12.33 loop awaited
    # each process_job in-line, so only ONE job was ever in flight and
    # pool.acquire() always tied at in_flight=0 — every job landed on
    # slot 1 (confirmed in prod logs: jobs 2836-2845 all slot 1). Now
    # the dispatcher spawns one asyncio.Task per job (bounded by pool
    # size) and keeps pulling while there's capacity.
    max_concurrent = max(1, len(pool.slots))
    in_flight: set = set()          # set[asyncio.Task]
    fatal = asyncio.Event()         # set by a task on AuthKey/Unauthorized
    fatal_reason: list = []

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

    log.info("worker started, entering main loop (concurrent dispatch, max=%d)",
             max_concurrent)
    while not _stop.is_set() and not fatal.is_set():
        # ---- 1) Reap finished job tasks --------------------------------
        for t in [t for t in in_flight if t.done()]:
            in_flight.discard(t)
            try:
                t.result()
            except Exception as e:  # noqa: BLE001
                # Should not happen — _run_one_job catches everything —
                # but never let a reaped task kill the dispatcher.
                log.error("job task raised unexpectedly: %s", e)

        # ---- 2) Pause gate ---------------------------------------------
        if await _is_paused():
            await asyncio.sleep(5)
            continue

        # ---- 3) Capacity gate ------------------------------------------
        if len(in_flight) >= max_concurrent:
            await asyncio.sleep(0.5)
            continue

        # ---- 4) Health gate: don't pull a job we can't dispatch --------
        if not pool.has_healthy_slot():
            await asyncio.sleep(5)
            continue

        # ---- 5) Pull next pending job ----------------------------------
        conn = db.connect()
        try:
            row = db.next_pending(conn)
        finally:
            conn.close()

        if row is None:
            # Queue drained AND nothing in flight → emit batch summary.
            if not in_flight and (batch["done"] or batch["partial"]
                                  or batch["failed"]):
                lines = [
                    "Batch summary:",
                    f"  ✅ done: {batch['done']}",
                    f"  ⚠️  partial: {batch['partial']}",
                    f"  ❌ failed: {len(batch['failed'])}",
                ]
                if batch["failed"]:
                    lines.append("  failed reasons:")
                    for u, r in batch["failed"][:25]:
                        lines.append(f"   • {u}  —  {r}")
                await _notify_admin("\n".join(lines))
                batch["done"] = batch["partial"] = 0
                batch["failed"] = []
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

        # Mark processing SYNCHRONOUSLY so next_pending can't hand the
        # same row to the next loop iteration.
        conn = db.connect()
        try:
            db.mark_processing(conn, job_id)
        finally:
            conn.close()

        log.info(
            "job %s START %s (via_search=%s user=%s admin=%s mpost=%s)",
            job_id, url, via_search, submitted_by, submitter_is_admin, mpost_enabled,
        )

        # ---- 6) Spawn the job as its own task; loop keeps pulling ------
        task = asyncio.create_task(
            _run_one_job(
                pool, job_id, url, url_hash,
                via_search=via_search, username=username,
                mpost_enabled=mpost_enabled, submitted_by=submitted_by,
                submitter_is_admin=submitter_is_admin,
                batch=batch, fatal=fatal, fatal_reason=fatal_reason,
            ),
            name=f"job-{job_id}",
        )
        in_flight.add(task)

        # v12.33b: inter-job delay REMOVED when the pool has >1 slot
        # (user decision 2026-08-21 — throughput is the point of the
        # pool). In 1-slot rollback mode the delay still paces the queue
        # exactly like v12.32.
        if max_concurrent == 1:
            await _random_delay()

    # ---- Shutdown: drain in-flight jobs before disconnecting -----------
    if in_flight:
        log.info("waiting for %d in-flight job(s) to finish before exit",
                 len(in_flight))
        await asyncio.gather(*in_flight, return_exceptions=True)

    if fatal.is_set():
        log.error("fatal session error (%s) — exiting 4",
                  fatal_reason[0] if fatal_reason else "?")
        await pool.stop()
        return 4

    log.info("worker stopping")
    # v12.33: stop the whole pool, not just slot 1's client.
    await pool.stop()
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
