"""
turso_nightly_sync.py — v12.74: nightly Mongo-2 -> Turso sync at 01:00 IST.

With BOT*_TURSO_OFF=1, Turso is frozen out of every bot's hot path (the
500M rows-read/month free-tier budget hit 75% — reads are the metered
killer). This job is the ONLY Turso writer left: once a night it upserts
the last ~26h of Mongo-2 cache rows into Turso so the database stays a
warm, recoverable backup WITHOUT burning read quota.

  * Writes only. Zero Turso reads (blind INSERT OR REPLACE by key).
  * Reads come from Mongo-2 (unmetered): rows updated in the window.
  * Batched (500/batch + short sleep) so the 512 MB instance never spikes;
    a Mongo lease doc makes the run single-flight across instances.
  * Admin alerted BOTH ways (operator requirement): one message to the log
    channel AND one DM to the admin user. Best-effort, never fails the sync.
  * Uses its OWN Turso HTTP writer (not turso_client.execute) because
    turso_client is globally frozen by BOT0_TURSO_OFF — the sync is the
    deliberate exception.

Env (Bot 0 service):
  TURSO_SYNC_ENABLED=1   master switch (default ON)
  TURSO_SYNC_HOUR_IST=1  hour of day in IST to fire (default 1)
  TURSO_SYNC_WINDOW_H=26 freshness window in hours (default 26)
  TURSO_SYNC_BATCH=500   rows per batch (default 500)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger("miniapp.turso_nightly_sync")

_IST_OFFSET_S = 5 * 3600 + 30 * 60          # UTC+5:30


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _enabled() -> bool:
    return os.environ.get("TURSO_SYNC_ENABLED", "1").strip() in ("1", "true", "yes")


def _next_run_epoch(now: float) -> float:
    """Next 01:00 IST expressed as a UTC epoch (no DST — IST is fixed)."""
    hour_ist = _env_int("TURSO_SYNC_HOUR_IST", 1)
    ist_now = now + _IST_OFFSET_S
    day0_ist = int(ist_now // 86400) * 86400
    target_ist = day0_ist + hour_ist * 3600
    if target_ist <= ist_now:
        target_ist += 86400
    return target_ist - _IST_OFFSET_S


# ---------------------------------------------------------------- Turso writer
def _turso_pipeline_url() -> str:
    raw = os.environ.get("TURSO_DATABASE_URL", "").strip()
    if not raw:
        return ""
    if raw.startswith("libsql://"):
        raw = "https://" + raw[len("libsql://"):]
    elif "://" not in raw:
        raw = "https://" + raw
    return raw.rstrip("/") + "/v2/pipeline"


def _turso_exec(sql: str, args: list) -> bool:
    """One statement against Turso's /v2/pipeline. Returns True on 2xx."""
    url = _turso_pipeline_url()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        return False
    body = {"requests": [{"type": "execute",
                          "stmt": {"sql": sql, "args": [
                              {"type": "text", "value": str(a)} if isinstance(a, str)
                              else {"type": "integer", "value": str(int(a))}
                              for a in args]}},
                         {"type": "close"}]}
    try:
        import httpx
        r = httpx.post(url, json=body, timeout=20,
                       headers={"Authorization": f"Bearer {token}",
                                "Content-Type": "application/json"})
        return 200 <= r.status_code < 300
    except Exception as e:  # noqa: BLE001
        log.warning("turso_exec failed: %s", e)
        return False


# ---------------------------------------------------------------- Mongo-2 read
def _mongo2_rows(window_h: int):
    try:
        from common import mongo2_client as _m2
    except Exception:  # noqa: BLE001
        try:
            import mongo2_client as _m2  # noqa: WPS433
        except Exception:  # noqa: BLE001
            return None, "mongo2_client unavailable"
    coll = _m2._get_coll()  # noqa: SLF001
    if coll is None:
        return None, "mongo2 collection unavailable"
    cutoff = time.time() - window_h * 3600
    return coll.find({"updated_at": {"$gte": cutoff}}).sort("updated_at", 1), None


def _acquire_lease() -> bool:
    """Single-flight lease in Mongo (relaybot.sync_lease) — 30 min TTL."""
    try:
        from ..rootdb import load as _lrd
        conn = _lrd().connect()
        col = conn.db["sync_lease"]
        now = time.time()
        res = col.update_one(
            {"_id": "turso_nightly",
             "$or": [{"expires_at": {"$lt": now}}, {"expires_at": {"$exists": False}}]},
            {"$set": {"expires_at": now + 1800, "owner": "bot0", "started_at": now}},
            upsert=True,
        )
        return bool(res.modified_count or res.upserted_id)
    except Exception as e:  # noqa: BLE001
        log.warning("sync lease failed-open (%s) — running anyway", e)
        return True


def _alert(text: str) -> None:
    """Admin alert BOTH ways: log channel + admin DM (Bot API, best-effort)."""
    try:
        from ..config import settings as _st
    except Exception:  # noqa: BLE001
        try:
            from app.config import settings as _st  # noqa: WPS433
        except Exception:  # noqa: BLE001
            log.warning("sync alert: settings unavailable"); return
    token = getattr(_st, "admin_bot_token", "") or os.environ.get("BOT_TOKEN", "")
    if not token:
        log.warning("sync alert: no bot token"); return
    import httpx
    targets = []
    ch = os.environ.get("LOG_CHANNEL_ID", "") or str(getattr(_st, "log_channel_id", "") or "")
    if ch:
        targets.append(ch)
    adm = str(getattr(_st, "admin_user_id", "") or os.environ.get("ADMIN_USER_ID", ""))
    if adm:
        targets.append(adm)
    for t in targets:
        try:
            httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": t, "text": text}, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("sync alert -> %s failed: %s", t, e)


async def _run_once() -> None:
    if not _acquire_lease():
        log.info("🌙 [TURSO SYNC] another instance holds the lease — skipping")
        return
    window_h = _env_int("TURSO_SYNC_WINDOW_H", 26)
    batch_sz = _env_int("TURSO_SYNC_BATCH", 500)
    t0 = time.time()
    cur, err = await asyncio.to_thread(_mongo2_rows, window_h)
    if cur is None:
        msg = f"🌙 [TURSO SYNC] aborted — {err}"
        log.error(msg); _alert(msg); return
    written = failed = 0
    batch = []

    def _flush(rows):
        nonlocal written, failed
        for d in rows:
            ok_ = _turso_exec(
                "INSERT OR REPLACE INTO nhentai_cache (key, payload, expires_at) "
                "VALUES (?, ?, ?)",
                [str(d.get("key")), str(d.get("payload") or ""),
                 int(d.get("expires_at") or 0)])
            if ok_:
                written += 1
            else:
                failed += 1

    for d in cur:
        batch.append(d)
        if len(batch) >= batch_sz:
            await asyncio.to_thread(_flush, batch)
            batch = []
            await asyncio.sleep(0.2)
    if batch:
        await asyncio.to_thread(_flush, batch)
    dur = int(time.time() - t0)
    msg = (f"🌙 [TURSO SYNC] done — wrote {written} rows to Turso "
           f"(failed {failed}, window {window_h}h, {dur}s). Hot path stays Mongo-only.")
    log.info(msg)
    _alert(msg)


async def run_forever() -> None:
    """Sleep until the next 01:00 IST, run, repeat. Never raises."""
    if not _enabled():
        log.info("turso_nightly_sync disabled (TURSO_SYNC_ENABLED=0)")
        return
    log.info("🌙 [TURSO SYNC] scheduled — daily 01:00 IST (writes-only backup)")
    while True:
        try:
            nxt = _next_run_epoch(time.time())
            await asyncio.sleep(max(30, nxt - time.time()))
            await _run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("turso_nightly_sync crashed: %s", e)
            _alert(f"🌙 [TURSO SYNC] CRASHED: {e}")
            await asyncio.sleep(300)
