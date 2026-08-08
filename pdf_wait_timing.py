"""
pdf_wait_timing.py — v11.7 (auto-tuning)

Compute an adaptive Bot 2 PDF-wait timeout based on the gallery's page count.

v11.7 auto-tuning
-----------------
The old v11.3 model used a static `seconds_per_page` coefficient (default 1.6).
That's fine on day one but Bot 2's speed drifts over months. v11.7 adds a
self-tuning loop:

  1. `record_bot2_latency(pages, latency_sec)`  — called by relay_v2 whenever
     a PDF successfully arrives. Persists to Mongo collection `bot2_latency`.
  2. `recompute_coefficient()`  — reads the last N=200 samples (or last 30
     days, whichever is smaller), takes the median of latency/pages, and
     caches the result in Mongo settings under `bot2_pdf_sec_per_page_learned`.
     Falls back to the env default if fewer than MIN_SAMPLES=20 points exist.
  3. `compute_pdf_timeout(pages)` reads learned first, env second, static 1.6.

Env-overridable:
  BOT2_PDF_TIMEOUT_FLOOR_SEC   (default 90)
  BOT2_PDF_TIMEOUT_CEIL_SEC    (default 900)
  BOT2_PDF_SEC_PER_PAGE        (default 1.6)   — used as fallback + safety cap
  BOT2_AUTOTUNE                (default "1"  — set "0" to disable learning)
"""
from __future__ import annotations

import logging
import os
import time
from statistics import median
from typing import Optional

log = logging.getLogger("pdf_wait_timing")


def _env_int(key: str, default: int) -> int:
    try:
        v = int(os.getenv(key, "") or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        v = float(os.getenv(key, "") or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _autotune_enabled() -> bool:
    return (os.getenv("BOT2_AUTOTUNE", "1") or "1").strip() not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# v11.7 — auto-tuned coefficient
# ---------------------------------------------------------------------------
_MIN_SAMPLES     = 20
_MAX_SAMPLES     = 200
_MAX_SAMPLE_AGE  = 30 * 24 * 3600  # 30 days


def _get_conn():
    """Return a MongoHandle or None. Never raise — the timeout path must
    NEVER take down the relay if Mongo momentarily drops."""
    try:
        import db
        return db.connect()
    except Exception as e:  # noqa: BLE001
        log.debug("autotune: db.connect() failed: %s", e)
        return None


def record_bot2_latency(pages: Optional[int], latency_sec: float) -> None:
    """Persist one (pages, latency_sec) observation. Best-effort — silent on error."""
    if not _autotune_enabled():
        return
    try:
        n = int(pages) if pages is not None else 0
        lat = float(latency_sec)
    except (TypeError, ValueError):
        return
    if n <= 0 or lat <= 0 or lat > 3600:
        return
    conn = _get_conn()
    if conn is None:
        return
    try:
        conn.database["bot2_latency"].insert_one({
            "pages":   n,
            "latency": lat,
            "ts":      int(time.time()),
        })
    except Exception as e:  # noqa: BLE001
        log.debug("autotune: insert failed: %s", e)


def _fetch_recent_samples() -> list[tuple[int, float]]:
    conn = _get_conn()
    if conn is None:
        return []
    try:
        cutoff = int(time.time()) - _MAX_SAMPLE_AGE
        cur = (conn.database["bot2_latency"]
                   .find({"ts": {"$gte": cutoff}}, {"pages": 1, "latency": 1})
                   .sort("ts", -1)
                   .limit(_MAX_SAMPLES))
        return [(int(d["pages"]), float(d["latency"])) for d in cur]
    except Exception as e:  # noqa: BLE001
        log.debug("autotune: fetch failed: %s", e)
        return []


def recompute_coefficient() -> Optional[float]:
    """Recompute seconds-per-page from recent samples. Returns the new
    coefficient (also cached in Mongo) or None if the sample is too thin."""
    if not _autotune_enabled():
        return None
    samples = _fetch_recent_samples()
    if len(samples) < _MIN_SAMPLES:
        log.debug("autotune: only %d samples (<%d) — keeping env default",
                  len(samples), _MIN_SAMPLES)
        return None
    per = [lat / pg for pg, lat in samples if pg > 0]
    if not per:
        return None
    k = float(median(per))
    env_default = _env_float("BOT2_PDF_SEC_PER_PAGE", 1.6)
    # Clamp so a bad batch of samples can't blow up the timeout.
    k = max(env_default * 0.5, min(env_default * 3.0, k))
    conn = _get_conn()
    if conn is not None:
        try:
            conn.database["settings"].update_one(
                {"_id": "bot2_pdf_sec_per_page_learned"},
                {"$set": {"value": k, "updated_ts": int(time.time()),
                          "n_samples": len(samples)}},
                upsert=True,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("autotune: cache write failed: %s", e)
    log.info("autotune: learned %.3f sec/page from %d samples "
             "(env default was %.3f)", k, len(samples), env_default)
    return k


def _learned_coefficient() -> Optional[float]:
    if not _autotune_enabled():
        return None
    conn = _get_conn()
    if conn is None:
        return None
    try:
        doc = conn.database["settings"].find_one({"_id": "bot2_pdf_sec_per_page_learned"})
        if not doc or "value" not in doc:
            return None
        v = float(doc["value"])
        return v if 0.1 < v < 20.0 else None
    except Exception as e:  # noqa: BLE001
        log.debug("autotune: cache read failed: %s", e)
        return None


def compute_pdf_timeout(
    pages: Optional[int],
    *,
    base_timeout_sec: Optional[int] = None,
) -> int:
    """Return the number of seconds to wait for Bot 2's PDF reply.
    v11.7: consults the learned coefficient first, env second, static default third."""
    floor   = _env_int("BOT2_PDF_TIMEOUT_FLOOR_SEC", 90)
    ceiling = _env_int("BOT2_PDF_TIMEOUT_CEIL_SEC", 900)
    per_pg  = _learned_coefficient() or _env_float("BOT2_PDF_SEC_PER_PAGE", 1.6)

    if base_timeout_sec is not None and base_timeout_sec > floor:
        floor = int(base_timeout_sec)
    if ceiling < floor:
        ceiling = floor + 60

    try:
        n = int(pages) if pages is not None else 0
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return floor

    scaled = int(floor + round(n * per_pg))
    return max(floor, min(ceiling, scaled))


def describe_timeout(pages: Optional[int], timeout: int) -> str:
    try:
        n = int(pages) if pages is not None else 0
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return f"{timeout}s (pages unknown; using floor)"
    tuned = _learned_coefficient() is not None
    tag = "auto-tuned" if tuned else "adaptive"
    return f"{timeout}s ({tag} for {n}-page gallery)"
