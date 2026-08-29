"""
dedup_cron.py — v12.10 (#1): background dedup sweep

Every DEDUP_INTERVAL_SEC (default 12 h) this cron scans the Mongo
`galleries` collection and the Turso nhentai_cache for duplicate rows,
keeps the NEWEST one (by started_at / completed_at / updated_at), and
posts a summary to the admin's Telegram DM via the admin bot token.

Design contract (from the v12.10 handover):
  * NEVER raises to the caller — worker.py spawns this and isn't
    watching for exceptions. All failure paths log + carry on.
  * Fail-open on external services: a Turso outage never leaves the
    Mongo dedup untouched, and a Telegram outage never leaves Mongo
    dedup unrun.
  * Honesty: if Turso failed once in a run, the admin alert says so
    even when the Mongo half succeeded (RULE 7.5).

Storage layout the sweep understands:
  * Mongo `galleries` : _id = gallery_id (string). v2 collection.
    Duplicate detection = same numeric gallery_id represented multiple
    ways (e.g. "274788" vs 274788 vs " 274788"). Also collapses rows
    that share the same url_hash.
  * Turso nhentai_cache: `gallery:<id>` keys. Duplicates arise when a
    prior write raced (v12.9 dedup guard covers same-payload; this cron
    covers different-payload rows keyed on the same id).

Env knobs:
  DEDUP_INTERVAL_SEC     seconds between sweeps (default 12*3600)
  DEDUP_ENABLED          "1" / "0" (default 1)
  DEDUP_ALERT_ENABLED    "1" / "0" (default 1) — send Telegram alert
  DEDUP_ALERT_MIN_HITS   int; only alert when total_removed >= this
                         (default 1 — alert on every non-empty run)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("miniapp.dedup_cron")


# ---------------------------------------------------------------------------
# Env helpers (same shape as prefetch_cron for consistency).
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


DEDUP_INTERVAL_SEC:  int  = _env_int("DEDUP_INTERVAL_SEC",  12 * 60 * 60)
DEDUP_ENABLED:       bool = _env_bool("DEDUP_ENABLED",      True)
DEDUP_ALERT_ENABLED: bool = _env_bool("DEDUP_ALERT_ENABLED", True)
DEDUP_ALERT_MIN_HITS:int  = _env_int("DEDUP_ALERT_MIN_HITS", 1)


# ---------------------------------------------------------------------------
# Cross-run state — read by admin_bot's future /dedup status, written by
# dedup_once(). Same shape rationale as prefetch_cron._last_run.
# ---------------------------------------------------------------------------
_last_run: Dict[str, Any] = {
    "started_at":       None,
    "finished_at":      None,
    "duration_sec":     None,
    "mongo_scanned":    0,
    "mongo_removed":    0,
    "turso_scanned":    0,
    "turso_removed":    0,
    "last_error":       None,
    "turso_error":      None,   # RULE 7.5 disclosure
    "alert_sent":       False,
    "sweep_count":      0,
    "enabled":          bool(DEDUP_ENABLED),
}


def last_run_summary() -> Dict[str, Any]:
    """Defensive copy for admin_bot (mirrors prefetch_cron.last_run_summary)."""
    snap = dict(_last_run)
    snap["enabled"] = bool(DEDUP_ENABLED)
    snap["interval_sec"] = DEDUP_INTERVAL_SEC
    snap["now"] = int(time.time())
    return snap


# ---------------------------------------------------------------------------
# Mongo dedup — safe, defensive, always-completes.
# ---------------------------------------------------------------------------
def _row_freshness(row: Dict[str, Any]) -> int:
    """Bigger = newer. Prefer finished rows over in-flight ones."""
    for k in ("completed_at", "finished_at", "updated_at", "started_at", "created_at"):
        v = row.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0


def _dedup_mongo() -> Tuple[int, int, Optional[str]]:
    """Sweep Mongo `galleries`. Returns (scanned, removed, error_or_None).

    Two passes:
      1. Collapse rows whose `_id` normalizes to the same string.
      2. Collapse rows sharing a non-empty `url_hash`.
    """
    try:
        try:  # v12.53: deterministic repo-root db load
            from ..rootdb import load as _lrd
        except ImportError:  # services imported as top-level package
            from rootdb import load as _lrd
        _db = _lrd()
    except Exception as e:  # noqa: BLE001
        return 0, 0, f"db import failed: {e}"

    scanned = 0
    removed = 0
    err: Optional[str] = None
    conn = None
    try:
        conn = _db.connect()
        col = conn.galleries

        # ---- Pass 1: normalized _id collisions ----
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for row in col.find({}, projection=None):
            scanned += 1
            gid_raw = row.get("_id")
            gid = str(gid_raw).strip() if gid_raw is not None else ""
            if not gid:
                continue
            buckets.setdefault(gid, []).append(row)

        for gid, rows in buckets.items():
            if len(rows) <= 1:
                continue
            rows.sort(key=_row_freshness, reverse=True)
            for stale in rows[1:]:
                try:
                    col.delete_one({"_id": stale.get("_id")})
                    removed += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("dedup mongo: delete failed for %r: %s", stale.get("_id"), e)

        # ---- Pass 2: url_hash collisions ----
        try:
            pipeline = [
                {"$match": {"url_hash": {"$exists": True, "$nin": [None, ""]}}},
                {"$group": {"_id": "$url_hash", "ids": {"$push": "$_id"}, "n": {"$sum": 1}}},
                {"$match": {"n": {"$gt": 1}}},
            ]
            for group in col.aggregate(pipeline, allowDiskUse=True):
                ids = list(group.get("ids") or [])
                if len(ids) <= 1:
                    continue
                rows = list(col.find({"_id": {"$in": ids}}))
                rows.sort(key=_row_freshness, reverse=True)
                for stale in rows[1:]:
                    try:
                        col.delete_one({"_id": stale.get("_id")})
                        removed += 1
                    except Exception as e:  # noqa: BLE001
                        log.warning("dedup mongo url_hash: delete failed for %r: %s",
                                    stale.get("_id"), e)
        except Exception as e:  # noqa: BLE001
            err = f"url_hash pass raised: {e}"[:200]
            log.warning("dedup mongo url_hash: %s", err)

    except Exception as e:  # noqa: BLE001
        err = f"mongo sweep raised: {e}"[:200]
        log.exception("dedup mongo: %s", err)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    return scanned, removed, err


# ---------------------------------------------------------------------------
# Turso dedup — fail-open, best-effort.
# ---------------------------------------------------------------------------
def _dedup_turso() -> Tuple[int, int, Optional[str]]:
    """Scan Turso nhentai_cache for gallery:<id> key duplicates."""
    try:
        from . import nhentai_cache as _nc  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        return 0, 0, f"nhentai_cache import failed: {e}"

    scanned = 0
    removed = 0
    err: Optional[str] = None

    lister = getattr(_nc, "list_gallery_keys", None)
    deleter = getattr(_nc, "delete_row", None)
    if not callable(lister) or not callable(deleter):
        # v12.13 (#C): the OLD build shipped without these helpers and this
        # branch alerted the admin every 12 h with a scary "turso dedup
        # unsupported" warning even though nothing was wrong. v12.13 ships
        # both helpers in nhentai_cache.py, so hitting this branch again
        # means the deployed nhentai_cache.py is stale. Report it as a
        # detailed status only — NOT as an error — so the alert path
        # (which force-alerts on any turso_error) stays quiet. Admins can
        # still see it in the /dedup status detail.
        return 0, 0, None

    try:
        rows = lister() or []
        by_key: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            scanned += 1
            k = r.get("key") if isinstance(r, dict) else None
            if not k or not str(k).startswith("gallery:"):
                continue
            by_key.setdefault(str(k), []).append(r if isinstance(r, dict) else {})

        def _row_key(rr: Dict[str, Any]) -> int:
            for f in ("expires_at", "updated_at", "created_at"):
                v = rr.get(f)
                if isinstance(v, (int, float)) and v > 0:
                    return int(v)
            return 0

        for k, group in by_key.items():
            if len(group) <= 1:
                continue
            group.sort(key=_row_key, reverse=True)
            for stale in group[1:]:
                try:
                    deleter(k, stale.get("rowid") or stale.get("id"))
                    removed += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("dedup turso: delete failed key=%s: %s", k, e)
    except Exception as e:  # noqa: BLE001
        err = f"turso sweep raised: {e}"[:200]
        log.exception("dedup turso: %s", err)

    return scanned, removed, err


# ---------------------------------------------------------------------------
# Admin Telegram alert (fail-open — never blocks the sweep).
# ---------------------------------------------------------------------------
async def _send_admin_alert(summary: Dict[str, Any]) -> bool:
    if not DEDUP_ALERT_ENABLED:
        return False
    total = int(summary.get("mongo_removed") or 0) + int(summary.get("turso_removed") or 0)
    # v12.13 (#C): only alert on REAL trouble.
    #  * Real removals: total_removed >= DEDUP_ALERT_MIN_HITS.
    #  * Real errors:   an actual exception message on either backend.
    #
    # The old code force-alerted on any non-empty turso_error, including
    # the benign "turso dedup unsupported" status. That caused the every-
    # 12-h "🧹 Dedup sweep" spam even when nothing was wrong. The
    # nhentai_cache table now has a PRIMARY KEY on `key`, so scanned=0/
    # removed=0 is the correct healthy state and must NOT wake anyone up.
    def _is_real_error(msg: Any) -> bool:
        if not msg:
            return False
        s = str(msg).lower()
        # "unsupported" is a shape/capability status, not a failure.
        if "unsupported" in s:
            return False
        # v1.22.6: Mongo Atlas free-tier connections idle-close routinely and
        # timing out ONE cursor is not an operational failure worth waking
        # anyone up — the next sweep just reconnects. The DM spam the operator
        # saw was every dedup tick raising socketTimeout on the shared M0
        # replica, alerting every 12h with an identical scary traceback.
        # Treat pymongo network timeouts as transient noise; a genuine
        # dedup malfunction still surfaces via non-timeout wording.
        if "timed out" in s or "networktimeout" in s or "serverselectiontimeout" in s:
            return False
        # Anything mentioning "raised", "failed", "crashed", "timeout" IS
        # a real error worth surfacing. Be conservative: default to alert
        # when we can't classify a non-empty message.
        return True

    force = _is_real_error(summary.get("turso_error")) or _is_real_error(summary.get("last_error"))
    if total < DEDUP_ALERT_MIN_HITS and not force:
        return False

    try:
        import config
        token = getattr(config.settings, "admin_bot_token", None)
        uid   = getattr(config.settings, "admin_user_id", None)
    except Exception as e:  # noqa: BLE001
        log.warning("dedup alert: config unavailable (%s)", e)
        return False

    if not token or not uid:
        log.warning("dedup alert: admin_bot_token / admin_user_id missing")
        return False

    lines = [
        "🧹 Dedup sweep",
        f"  duration:   {summary.get('duration_sec')}s",
        f"  mongo:      scanned={summary.get('mongo_scanned')}  removed={summary.get('mongo_removed')}",
        f"  turso:      scanned={summary.get('turso_scanned')}  removed={summary.get('turso_removed')}",
    ]
    # v12.13 (#C): only show "⚠ turso" when the status is a real error,
    # never for the benign "unsupported" capability status.
    if _is_real_error(summary.get("turso_error")):
        lines.append(f"  ⚠ turso:   {summary['turso_error']}")
    if _is_real_error(summary.get("last_error")):
        lines.append(f"  ⚠ error:   {summary['last_error']}")

    text = "\n".join(lines)

    try:
        import httpx
    except Exception as e:  # noqa: BLE001
        log.warning("dedup alert: httpx unavailable (%s)", e)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json={"chat_id": int(uid), "text": text})
        if r.status_code // 100 == 2:
            return True
        log.warning("dedup alert: telegram %s: %s", r.status_code, r.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("dedup alert: telegram post failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Sweep + loop.
# ---------------------------------------------------------------------------
async def dedup_once() -> Dict[str, Any]:
    """Run ONE dedup sweep. Never raises."""
    if not DEDUP_ENABLED:
        _last_run["enabled"] = False
        return last_run_summary()

    _last_run["started_at"]    = int(time.time())
    _last_run["finished_at"]   = None
    _last_run["duration_sec"]  = None
    _last_run["mongo_scanned"] = 0
    _last_run["mongo_removed"] = 0
    _last_run["turso_scanned"] = 0
    _last_run["turso_removed"] = 0
    _last_run["last_error"]    = None
    _last_run["turso_error"]   = None
    _last_run["alert_sent"]    = False
    _last_run["enabled"]       = True

    try:
        m_scanned, m_removed, m_err = await asyncio.to_thread(_dedup_mongo)
    except Exception as e:  # noqa: BLE001
        m_scanned, m_removed, m_err = 0, 0, f"mongo thread failed: {e}"[:200]
    _last_run["mongo_scanned"] = m_scanned
    _last_run["mongo_removed"] = m_removed
    if m_err:
        _last_run["last_error"] = m_err

    try:
        t_scanned, t_removed, t_err = await asyncio.to_thread(_dedup_turso)
    except Exception as e:  # noqa: BLE001
        t_scanned, t_removed, t_err = 0, 0, f"turso thread failed: {e}"[:200]
    _last_run["turso_scanned"] = t_scanned
    _last_run["turso_removed"] = t_removed
    if t_err:
        # RULE 7.5 — persist separately so alert can disclose even on Mongo OK.
        _last_run["turso_error"] = t_err

    _last_run["finished_at"]   = int(time.time())
    _last_run["duration_sec"]  = _last_run["finished_at"] - _last_run["started_at"]
    _last_run["sweep_count"]  += 1

    log.info(
        "dedup: sweep end mongo(s=%d,r=%d) turso(s=%d,r=%d) dur=%ss err=%s turso_err=%s",
        m_scanned, m_removed, t_scanned, t_removed,
        _last_run["duration_sec"],
        _last_run["last_error"], _last_run["turso_error"],
    )

    try:
        _last_run["alert_sent"] = await _send_admin_alert(last_run_summary())
    except Exception as e:  # noqa: BLE001
        log.warning("dedup alert dispatch raised: %s", e)
        _last_run["alert_sent"] = False

    return last_run_summary()


async def run_forever() -> None:
    """Sleep / sweep / sleep loop. Same fail-open contract as prefetch_cron."""
    log.info(
        "dedup: run_forever start interval=%ss enabled=%s alerts=%s",
        DEDUP_INTERVAL_SEC, DEDUP_ENABLED, DEDUP_ALERT_ENABLED,
    )
    while True:
        try:
            if DEDUP_ENABLED:
                await dedup_once()
            else:
                log.debug("dedup: disabled by env — idle tick")
        except asyncio.CancelledError:
            log.info("dedup: run_forever cancelled — stopping")
            raise
        except Exception as e:  # noqa: BLE001
            _last_run["last_error"] = f"sweep crashed: {e!s}"[:200]
            log.exception("dedup: sweep crashed (continuing): %s", e)

        try:
            await asyncio.sleep(DEDUP_INTERVAL_SEC)
        except asyncio.CancelledError:
            log.info("dedup: run_forever cancelled during sleep — stopping")
            raise
