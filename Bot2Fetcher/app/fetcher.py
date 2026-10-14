"""
fetcher.py — the Bot2Fetcher pipeline (v12.49 — dual-downloader chain).

Per-slot peer resolution: each Telethon session warms its own peer
cache via iter_dialogs() + get_input_entity() before the slot loop
starts. All Bot 0 / DB channel / @Gallery_DLBot calls use per-slot
input entities so slot 2 can NEVER PeerIdInvalidError.

v12.49 (fallback chain, operator spec 2026-09-01):
  * When @Gallery_DLBot answers with ANY error text (the
    _HARD_ERROR_PATTERNS match), the SAME link is immediately DM'd to the
    fallback bot (BACKUP_PDF_USERNAME, default pdfdownloadcinbot) with a
    fixed 5-minute wait — no more 3-strike counter on the primary bot.
  * If the fallback also fails (error text or timeout), the gallery is
    parked 12h (mongo_state.set_both_failed), a loud alert goes to the
    Bot 2 LOG_CHANNEL, and the next retry after the park goes STRAIGHT
    to the fallback bot (fallback_pending flag), skipping @Gallery_DLBot.
  * Legacy FAILED_BOT2_ERROR docs (pre-v12.49 permanent skips) become
    reclaimable 12h after their last failure and also route straight to
    the fallback bot.
  * Single-flight guarantee: an in-process asyncio.Lock plus a Mongo
    fallback_lease doc (FallbackLease) make concurrent DMs to the
    fallback bot impossible — across slots AND across instances.
  * BACKUP_PDF_USERNAME unset/empty → the whole chain is inert and
    behavior is byte-identical to v12.48.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import re
import time
from typing import Any, Dict, Optional

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import InputMediaUploadedPhoto

from . import meta as meta_mod
from .cover_normalise import normalise_cover_bytes
from .pdf_timing import compute_pdf_timeout

log = logging.getLogger("bot2fetcher.fetcher")

POLL_EVERY_S = 3.0

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_COVER_HEADERS = {
    "User-Agent": _UA,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Referer": "https://nhentai.net/",
}

# v12.42: the PERMANENT Bot2 failure Ryan reported — Gallery_DLBot replies
# "An error occurred: No images found after download or ZIP extraction."
# for galleries it can never produce a PDF for. Tracked separately so only
# this class of error counts toward the skip-after-3 threshold; transient
# hard errors keep the old drop-and-retry behavior.
_NO_IMAGES_PATTERN = re.compile(r"no images found", re.IGNORECASE)

_HARD_ERROR_PATTERNS = re.compile(
    r"(❌|⛔|🚫|\berror\b|\bfailed\b|not found|"
    r"invalid|unavailable|forbidden|blocked|removed|"
    r"unsupported|can(?:not|'t) (?:find|download|process)|"
    r"^sorry\b|please try again)",
    re.IGNORECASE,
)


def channel_link(channel_id: int, msg_id: int) -> str:
    s = str(abs(int(channel_id)))
    if s.startswith("100"):
        s = s[3:]
    return f"https://t.me/c/{s}/{int(msg_id)}"


class Stats:
    def __init__(self) -> None:
        self.scanned = 0
        self.skipped_done = 0
        self.skipped_failed = 0
        self.skipped_busy = 0
        self.claimed = 0
        self.completed = 0
        self.failed = 0
        self.dropped = 0
        self.in_flight: Dict[str, str] = {}
        self.started_at = time.time()
        self.last_finished_at = 0.0
        self.cycles = 0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "started_at": int(self.started_at),
            "uptime_s": int(time.time() - self.started_at),
            "cycles": self.cycles,
            "scanned": self.scanned,
            "claimed": self.claimed,
            "completed": self.completed,
            "failed": self.failed,
            "dropped": self.dropped,
            "skipped_done": self.skipped_done,
            "skipped_failed": self.skipped_failed,
            "skipped_busy": self.skipped_busy,
            "in_flight": dict(self.in_flight),
            "last_finished_at": int(self.last_finished_at),
        }


class Fetcher:
    def __init__(self, settings, galleries, turso, stats: Stats, dashboard=None):
        self.s = settings
        self.galleries = galleries
        self.turso = turso
        self.stats = stats
        self.dash = dashboard
        self.clients: list[TelegramClient] = []
        # v13.10: Mongo-backed workers with hot-reload. workers_db is wired
        # in main.py; _slot_clients/_slot_tasks track live slots by number;
        # _creds_fp forces a full reconnect when api_id/hash change.
        self.workers_db = None
        self.downloader_username = self.s.bot2_username.strip().lstrip("@")
        token_id = (os.getenv("BOT2_TOKEN") or "").strip().split(":", 1)[0]
        self._admin_bot_id = int(token_id) if token_id.isdigit() else None
        self._slot_runtime: Dict[int, Dict[str, Any]] = {}
        self._slot_account_ids: Dict[int, int] = {}
        self._slot_clients: Dict[int, TelegramClient] = {}
        self._slot_sessions: Dict[int, str] = {}
        self._slot_tasks: Dict[int, asyncio.Task] = {}
        self._creds_fp: str = ""
        self._reconcile_evt = asyncio.Event()
        self._main_loop = None
        self._channel_lock = asyncio.Lock()
        # v12.44: process-lifetime memory of gids already delivered or
        # permanently failed. The producer drops these BEFORE the queue so
        # slots never spend claim+cache-read time on known-finished work.
        self._known_done: set = set()
        self._known_failed: set = set()
        # v12.88: time-boxed cooldown for transient drop_claim failures
        # (primary timeout / unusable meta). Replaces the permanent
        # _known_failed.add() blackhole that stranded user queue rows
        # until the next process restart (live case: gid 679381).
        self._retry_after: dict = {}
        self._retry_cooldown_s = int(
            getattr(self.s, "retry_cooldown_s", 1800) or 1800)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()
        self._channel_per_slot: dict[int, Any] = {}
        self._bot2_per_slot: dict[int, Any] = {}
        # v12.49: fallback downloader state
        self._fallback_per_slot: dict[int, Any] = {}
        self._fallback_lock = asyncio.Lock()          # Layer A: in-process
        self.fallback_lease = None                    # Layer B: Mongo lease
        self._alert_lock = asyncio.Lock()             # alert rate-limiting

    def _d_state(self, idx, state, gid="", title="", pages=0, step=""):
        self._slot_runtime.setdefault(idx, {}).update(
            state=state, gallery_id=str(gid), step=step)
        if self.dash:
            try:
                self.dash.slot_state(idx, state, gid, title, pages, step)
            except Exception as e:
                # v12.85: dashboard state failures were 100% silent —
                # the "slots always show idle" class of bug. Warn once.
                if not getattr(self, "_d_state_warned", False):
                    self._d_state_warned = True
                    log.warning("📊 dashboard slot_state update failed: %s", e)

    def _d_event(self, idx, kind, gid):
        if kind == "completed":
            rt = self._slot_runtime.setdefault(idx, {})
            rt["completed"] = rt.get("completed", 0) + 1
        if self.dash:
            try:
                self.dash.slot_event(idx, kind, gid)
            except Exception:
                pass

    def _d_scan(self, **kw):
        if self.dash:
            try:
                self.dash.set_scan_info(**kw)
            except Exception:
                pass

    async def _warm_and_resolve(self, idx: int, client: TelegramClient) -> None:
        # v12.41: optional PeerCache (injected by main.py as self.peer_cache;
        # None = byte-identical to the pre-v12.41 live-resolution path).
        peer_cache = getattr(self, "peer_cache", None)
        fp = None
        if peer_cache is not None:
            try:
                fp = peer_cache.fp(client)
            except Exception as e:
                log.warning("🧭 slot %d peer-cache fingerprint failed: %s", idx, e)
                peer_cache = None

        async def _get_entity(kind: str, key):
            if peer_cache is not None and fp:
                try:
                    blob = peer_cache.get(slot=idx, kind=kind,
                                          session_fingerprint=fp)
                    if blob is not None:
                        return peer_cache.loads(blob)
                except Exception as e:
                    log.warning("🧭 slot %d %s peer-cache read failed: %s",
                                idx, kind, e)
            ent = await client.get_input_entity(key)   # live resolution
            if peer_cache is not None and fp:
                try:
                    peer_cache.put(slot=idx, kind=kind, entity=ent,
                                   session_fingerprint=fp)
                except Exception as e:
                    log.warning("🧭 slot %d %s peer-cache write failed: %s",
                                idx, kind, e)
            return ent

        ch_key = self.s.db_channel_id
        try:
            ch_key = int(ch_key) if str(ch_key).lstrip("-").isdigit() else ch_key
        except Exception:
            pass

        # Only run the expensive dialog warm-up if EITHER entity is cache-cold.
        need_warm = True
        if peer_cache is not None and fp:
            try:
                need_warm = not (
                    peer_cache.has(slot=idx, kind="db_channel", session_fingerprint=fp)
                    and peer_cache.has(slot=idx, kind=f"pdf_user:{self.downloader_username.casefold()}", session_fingerprint=fp)
                )
            except Exception:
                need_warm = True
        if need_warm:
            try:
                n = 0
                async for _ in client.iter_dialogs(limit=200):
                    n += 1
                log.info("🧭 slot %d peer cache warmed (%d dialogs)", idx, n)
            except Exception as e:
                log.warning("🧭 slot %d iter_dialogs failed: %s", idx, e)
        else:
            log.info("🧭 slot %d peer cache warm-hit — skipping iter_dialogs", idx)

        try:
            self._channel_per_slot[idx] = await _get_entity("db_channel", ch_key)
            log.info("📺 slot %d resolved DB channel: %s", idx, self.s.db_channel_id)
        except Exception as e:
            raise RuntimeError(
                f"slot {idx}: cannot resolve DB_CHANNEL_ID={self.s.db_channel_id!r} — {e}. "
                f"Make sure this userbot is a MEMBER of the channel."
            ) from e
        try:
            self._bot2_per_slot[idx] = await _get_entity(f"pdf_user:{self.downloader_username.casefold()}", self.downloader_username)
            if (self._admin_bot_id is not None and
                    getattr(self._bot2_per_slot[idx], "user_id", None) == self._admin_bot_id):
                self.downloader_username = "Gallery_DLBot"
                self._bot2_per_slot[idx] = await _get_entity(
                    "primary_pdf_user", self.downloader_username)
                log.warning("slot %d: management bot peer rejected; using @Gallery_DLBot", idx)
            log.info("🤖 slot %d resolved downloader bot: @%s",
                     idx, self.downloader_username)
        except Exception as e:
            raise RuntimeError(
                f"slot {idx}: cannot resolve BOT2_USERNAME=@{self.downloader_username} — {e}. "
                f"Have this userbot DM @{self.downloader_username} once, then restart."
            ) from e

        # v12.49: resolve the fallback downloader bot per slot. Optional —
        # when BACKUP_PDF_USERNAME is unset the chain is inert and we skip
        # resolution entirely (never blocks boot, never errors).
        fb = (getattr(self.s, "fallback_username", "") or "").strip().lstrip("@")
        if fb:
            try:
                self._fallback_per_slot[idx] = await _get_entity(
                    "fallback_bot", fb)
                log.info("🛟 slot %d resolved fallback bot: @%s", idx, fb)
            except Exception as e:
                # Non-fatal: the chain degrades to v12.48 behavior for this
                # slot, and the operator sees exactly why in the boot log.
                log.error("🛟 slot %d CANNOT resolve fallback bot @%s — "
                          "fallback chain DISABLED for this slot: %s. "
                          "Have this userbot DM @%s once, then restart.",
                          idx, fb, e, fb)

    async def _validate_downloader(self) -> None:
        """Never send gallery requests to this service's management bot."""
        token = (os.getenv("BOT2_TOKEN") or "").strip()
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as h:
                response = await h.get(f"https://api.telegram.org/bot{token}/getMe")
                data = response.json()
            if not data.get("ok"):
                log.warning("Downloader identity guard: admin getMe failed (code=%s)",
                            data.get("error_code"))
                return
            me = data["result"]
            self._admin_bot_id = int(me["id"])
            if self.downloader_username.casefold() == me["username"].casefold():
                log.error("DOWNLOADER CONFIG ERROR: @%s is the worker-management bot, "
                          "not a PDF downloader. Using @Gallery_DLBot; set "
                          "PDF_DOWNLOADER_USERNAME=Gallery_DLBot in Render.",
                          self.downloader_username)
                self.downloader_username = "Gallery_DLBot"
            log.info("PDF downloader: @%s (separate from worker-management bot)",
                     self.downloader_username)
        except Exception as e:
            # Do not expose a token-bearing HTTP URL in exception text.
            log.warning("Downloader identity guard unavailable (%s); retrying at peer resolution",
                        type(e).__name__)

    def worker_status(self) -> Dict[int, Dict[str, Any]]:
        """Thread-readable, secret-free snapshot for /listworker."""
        out = {}
        for idx, rt in list(self._slot_runtime.items()):
            row = dict(rt)
            task = self._slot_tasks.get(idx)
            row["running"] = bool(task and not task.done())
            out[idx] = row
        return out

    async def start(self) -> None:
        # v13.10: no env sessions — populate from the Mongo worker store.
        # Zero workers = standby (never raise). A revoked session only skips
        # its own slot; it can never take the service down.
        await self._validate_downloader()
        await self.reconcile_workers(initial=True)

    # ---- v13.10 worker reconcile (hot-reload, no restart needed) ----------
    def request_reconcile(self) -> None:
        """Thread-safe nudge from the admin bot after a worker change."""
        try:
            loop = self._main_loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._reconcile_evt.set)
            else:
                self._reconcile_evt.set()
        except Exception:  # noqa: BLE001
            pass

    def _worker_config(self):
        """(creds, [(slot, session)]) from Mongo. Fail-open: (None, []).
        Creds fall back to env API_ID/API_HASH only when Mongo has none."""
        if self.workers_db is None:
            return None, []
        try:
            from . import workers as _wk
            creds = _wk.get_creds(self.workers_db)
            if not creds and self.s.api_id and self.s.api_hash:
                creds = (str(self.s.api_id), str(self.s.api_hash))
            rows = [(w["slot"], w["session"]) for w in _wk.list_workers(self.workers_db)]
            return creds, rows
        except Exception as e:  # noqa: BLE001
            log.warning("worker config read failed (fail-open, standby): %s", e)
            return None, []

    async def _connect_slot(self, idx: int, session: str, api_id: str, api_hash: str) -> None:
        c = None
        self._slot_runtime[idx] = {"state": "connecting", "completed": 0}
        try:
            c = TelegramClient(StringSession(session), int(api_id), api_hash)
            await c.connect()
            if not await c.is_user_authorized():
                raise RuntimeError("session not authorized; re-add this worker")
            me = await c.get_me()
            if int(me.id) in self._slot_account_ids.values():
                raise RuntimeError("duplicate Telegram account already active in another slot")
            uname = getattr(me, "username", None) or "(no username)"
            self._slot_runtime[idx].update(account_id=int(me.id), account=uname)
            log.info("🤖 slot %d authorized as @%s (id=%s)", idx, uname, me.id)
            if self.dash:
                self.dash.set_account(idx, uname)
            await self._warm_and_resolve(idx, c)
            # Publish ONLY fully-resolved clients. Failed setup must be retried.
            self._slot_clients[idx] = c
            self._slot_sessions[idx] = session
            self._slot_account_ids[idx] = int(me.id)
            self.clients = [self._slot_clients[k] for k in sorted(self._slot_clients)]
            self._slot_tasks[idx] = asyncio.create_task(self._slot_loop(idx, c))
            self._slot_runtime[idx].update(state="idle", error="")
        except BaseException as e:
            if c is not None:
                try:
                    await c.disconnect()
                except Exception:
                    pass
            self._channel_per_slot.pop(idx, None)
            self._bot2_per_slot.pop(idx, None)
            self._fallback_per_slot.pop(idx, None)
            self._slot_runtime[idx].update(state="error", error=str(e)[:180])
            raise

    async def _drop_slot(self, idx: int) -> None:
        t = self._slot_tasks.pop(idx, None)
        if t:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        self._slot_sessions.pop(idx, None)
        self._slot_account_ids.pop(idx, None)
        self._slot_runtime.pop(idx, None)
        self._channel_per_slot.pop(idx, None)
        self._bot2_per_slot.pop(idx, None)
        self._fallback_per_slot.pop(idx, None)
        c = self._slot_clients.pop(idx, None)
        if c:
            try:
                await c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.clients = [self._slot_clients[k] for k in sorted(self._slot_clients)]
        if self.dash:
            try:
                self.dash.set_account(idx, "(removed)")
            except Exception:  # noqa: BLE001
                pass
        log.info("🗑 slot %d disconnected (worker removed)", idx)

    async def reconcile_workers(self, initial: bool = False) -> None:
        creds, rows = await asyncio.to_thread(self._worker_config)
        want = {slot: sess for slot, sess in rows}
        if not want:
            if initial:
                log.warning("🛌 Bot 2 STANDBY — no workers configured. "
                            "Add one via /addworker (BOT2_TOKEN admin bot).")
            for idx in list(self._slot_clients):
                await self._drop_slot(idx)
            self._slot_runtime.clear()
            return
        if not creds:
            log.warning("🛌 worker(s) exist but API creds missing — finish "
                        "/addworker or run /changeapiandhash")
            return
        fp = f"{creds[0]}|{creds[1]}"
        if fp != self._creds_fp:
            if self._creds_fp:
                log.info("🔑 API creds changed — reconnecting all slots")
            self._creds_fp = fp
            for idx in list(self._slot_clients):
                await self._drop_slot(idx)
        for idx in list(self._slot_runtime):
            if idx not in want and idx not in self._slot_clients:
                self._slot_runtime.pop(idx, None)
        for idx in list(self._slot_clients):
            task = self._slot_tasks.get(idx)
            if (idx not in want or self._slot_sessions.get(idx) != want[idx]
                    or task is None or task.done()):
                await self._drop_slot(idx)
        for idx in sorted(want):
            if idx not in self._slot_clients:
                try:
                    await asyncio.wait_for(
                        self._connect_slot(idx, want[idx], creds[0], creds[1]),
                        timeout=45.0)
                except Exception as e:
                    if isinstance(e, asyncio.TimeoutError):
                        self._slot_runtime.setdefault(idx, {}).update(
                            state="error", error="connection/setup timeout after 45s")
                    log.warning("slot %d setup failed (%s); remaining slots continue",
                                idx, type(e).__name__)

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._reconcile_evt.clear()
                await self.reconcile_workers()
            except Exception as e:  # noqa: BLE001
                log.warning("worker reconcile failed (retrying): %s", e)
            try:
                await asyncio.wait_for(self._reconcile_evt.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        for t in list(self._slot_tasks.values()):
            t.cancel()
        for c in list(self._slot_clients.values()):
            try:
                await c.disconnect()
            except Exception:
                pass

    async def _build_queue_order(self) -> list[str]:
        self._d_scan(phase="reading recent pages…")
        recent_ids = await self.turso.list_recent_search_ids()
        self._d_scan(phase="listing cache…")
        gallery_rows = await self.turso.list_gallery_ids()
        # v12.80: same mixed-type trap as turso_store — Mongo-2 cached_at can
        # be str on some rows; an unguarded sort would crash the whole scan
        # cycle (outer except -> sleep 30 -> retry forever).
        def _ca_key(r):
            try:
                return float(r.get("cached_at") or 0)
            except (TypeError, ValueError):
                return 0.0
        gallery_rows.sort(key=_ca_key, reverse=True)
        # v12.79: the sweep exists to re-verify FRESH cache rows — walking
        # the entire (forever-growing) history every cycle is what made
        # post-restart scans take 24min for 265 already-done gids.
        max_scan = int(getattr(self.s, "scan_max_galleries", 2000) or 2000)
        if len(gallery_rows) > max_scan:
            log.info("✂️ scan window capped: %d cached gids -> newest %d",
                     len(gallery_rows), max_scan)
            gallery_rows = gallery_rows[:max_scan]
        seen: set = set()
        ordered: list[str] = []
        # v12.73: user-triggered queue rows (mini-app Download on uncached
        # galleries) go FIRST — Bot 0 writes them to Mongo 'queue' with
        # status='pending'; without this the producer only ever saw the
        # Turso cache and user downloads were never picked up.
        user_gids: set = set()
        try:
            for qgid in self.galleries.list_pending_queue():
                if qgid not in seen:
                    seen.add(qgid); ordered.append(qgid)
                user_gids.add(qgid)          # v12.88: exempt from pre-filter
        except Exception:
            pass
        # v12.85: Bot 1 scrape notifications sit right behind user queue
        # rows (ahead of the background sweep) — they are by definition
        # brand-new galleries that need their PDF fetched.
        try:
            for ngid in self.galleries.pop_scrape_notifications():
                if ngid not in seen:
                    seen.add(ngid); ordered.append(ngid)
                user_gids.add(ngid)          # v12.88: exempt from pre-filter
        except Exception:
            pass
        for gid in recent_ids:
            if gid not in seen:
                seen.add(gid); ordered.append(gid)
        for r in gallery_rows:
            gid = r["gid"]
            if gid not in seen:
                seen.add(gid); ordered.append(gid)
        # v12.56: lifetime cached = ALL gallery:* rows in Turso BEFORE the
        # known-done/failed filter — shown on the dashboard alongside the
        # post-filter queue so "cache < In DB channel" stops looking like
        # data loss (the two measure different sets).
        self._last_cache_lifetime = len(gallery_rows)

        # v12.44: drop gids we already know are delivered / permanently
        # failed. On a warm process this eliminates nearly every
        # "⏭ already in DB channel — skipped" cycle (the 146-PDFs-in-15h
        # problem: most slot time was being spent re-claiming known rows).
        # v12.88: purge expired transient-retry cooldowns, and NEVER apply
        # the known-done/known-failed pre-filter to user-queue or
        # scrape-notify gids — an explicit queue row must always reach a
        # slot so claim_ex (the durable Mongo state machine) makes the
        # done/failed/claim decision. The v12.44 in-memory sets blackholed
        # exactly these rows while slots kept working background items.
        now_ts = time.time()
        if self._retry_after:
            self._retry_after = {g: t for g, t in self._retry_after.items()
                                 if t > now_ts}
        before = len(ordered)
        ordered = [g for g in ordered
                   if g in user_gids
                   or (g not in self._known_done
                       and g not in self._known_failed
                       and g not in self._retry_after)]
        filtered = before - len(ordered)
        if filtered:
            log.info("🧹 queue: pre-filtered %d known-done/failed gids "
                     "(%d candidates left)", filtered, len(ordered))
        return ordered

    def _warm_skip_sets(self) -> None:
        """v12.79: one projected, indexed read at boot so a restart doesn't
        re-claim every already-done gid. Observed live: after a redeploy,
        265 '⏭ already in DB channel — skipped' lines took ~24 minutes
        because each skipped gid still ran a Mongo CAS before rejection.

        Loads into the existing in-memory sets:
          _known_done   <- status COMPLETED / PARTIAL  (in DB channel)
          _known_failed <- status FAILED_TIMEOUT / FAILED_SCRAPE /
                           FAILED_OTHER  (no retry path exists)

        FAILED_BOT2_ERROR is deliberately NOT loaded — it has the 12h
        park-then-retry path in claim_ex and MUST stay scannable.
        User-queue rows also bypass these sets entirely, so a user
        re-tapping Download on a previously-failed gallery still works.
        On any error the sets stay empty and fill gradually, exactly like
        pre-v12.79 behavior.
        """
        try:
            coll = self.galleries.coll
            d = f = 0
            for doc in coll.find({"status": {"$in": ["COMPLETED", "PARTIAL"]}},
                                 projection={"_id": 1}):
                self._known_done.add(str(doc["_id"])); d += 1
            for doc in coll.find(
                    {"status": {"$in": ["FAILED_TIMEOUT", "FAILED_SCRAPE",
                                        "FAILED_OTHER"]}},
                    projection={"_id": 1}):
                self._known_failed.add(str(doc["_id"])); f += 1
            log.info("🧹 warmup: loaded %d done + %d permanently-failed "
                     "gids from Mongo (skip-set warm)", d, f)
        except Exception as e:  # noqa: BLE001
            log.warning("warmup skip-set load failed — gradual fill: %s", e)

    async def _producer(self) -> None:
        self._warm_skip_sets()
        while not self._stop.is_set():
            # v13.10: standby when no workers — don't burn RAM/CPU scanning.
            if not self._slot_clients:
                await asyncio.sleep(5)
                continue
            try:
                # v12.84: one cheap update_many per cycle — resets dead
                # PROCESSING claims so their queue rows can finally drain.
                try:
                    self.galleries.reap_zombies()
                    # v12.86: reconcile the mini-app ledger — pending rows
                    # whose gid is already COMPLETED are phantom backlog.
                    self.galleries.reap_queue_ledger()
                    # v12.88: loud alarm for user queue rows stuck pending
                    # with no galleries doc (must never be silent again).
                    self.galleries.log_starving_queue()
                except Exception:
                    pass
                ids = await self._build_queue_order()
            except Exception as e:
                log.exception("🚨 producer scan crashed: %s", e)
                await asyncio.sleep(30)
                continue
            self.stats.scanned = len(ids)
            self._d_scan(phase=f"fetching (cycle {self.stats.cycles + 1})",
                         cache_total=len(ids))
            log.info("📡 scan cycle %d: %d candidate galleries queued",
                     self.stats.cycles + 1, len(ids))
            for gid in ids:
                if self._stop.is_set():
                    break
                await self._queue.put(gid)
            self.stats.cycles += 1
            while not self._queue.empty() and not self._stop.is_set():
                # v12.85: keep the dashboard phase truthful while slots
                # drain the queue (it used to freeze on "listing cache…").
                self._d_scan(phase=f"slots working — {self._queue.qsize()} queued")
                await asyncio.sleep(10)
            self._d_scan(phase="queue drained — idle")
            log.info("💤 queue drained — idle-waiting (user downloads wake "
                     "instantly; full rescan in %ds)", self.s.rescan_sleep_s)
            # v12.78: interruptible idle sleep. Before this, a mini-app
            # Download tapped right after a scan waited out the ENTIRE
            # rescan_sleep_s (default 300s) even with both slots free —
            # observed live: #661490 sat ~5min queued, then finished in 22s.
            # Now the producer peeks at the queue ledger every few seconds
            # (one find_one-sized read — negligible) and wakes the moment a
            # user row appears. The expensive full cache/Turso rescan still
            # runs on the original cadence, so idle read cost stays flat.
            waited = 0.0
            step = 5.0
            while waited < float(self.s.rescan_sleep_s) and not self._stop.is_set():
                await asyncio.sleep(step)
                waited += step
                try:
                    # v12.85: Bot 1 discoveries wake us TOO — pop the
                    # notify doc and hand the gids straight to the slots.
                    fresh = self.galleries.pop_scrape_notifications()
                    if fresh:
                        log.info("⚡ %d fresh scrape id(s) from Bot 1 — "
                                 "queueing instantly", len(fresh))
                        for fg in fresh:
                            await self._queue.put(fg)
                        break
                except Exception:
                    pass
                try:
                    if self.galleries.list_pending_queue(limit=1):
                        log.info("⚡ user queue row detected — waking producer "
                                 "early (%.0fs into idle wait)", waited)
                        break
                except Exception:
                    pass  # a Mongo hiccup must never kill the idle loop

    async def _slot_loop(self, idx: int, client: TelegramClient) -> None:
        log.info("🧵 slot %d worker started", idx)
        self._d_state(idx, "idle")
        while not self._stop.is_set():
            try:
                gid = await asyncio.wait_for(self._queue.get(), timeout=15)
            except asyncio.TimeoutError:
                self._d_state(idx, "idle")
                continue
            did_work = False
            try:
                did_work = await self._do_job(idx, client, gid)
            except FloodWaitError as fw:
                wait = int(getattr(fw, "seconds", 30)) + 5
                log.warning("🚧 slot %d FloodWait %ss on %s — sleeping, will retry",
                            idx, wait, gid)
                self._d_event(idx, "floodwait", gid)
                self._d_state(idx, "resting", gid, step=f"FloodWait {wait}s")
                self.galleries.drop_claim(gid)
                await asyncio.sleep(wait)
                await self._queue.put(gid)
                did_work = False
            except Exception as e:
                log.exception("💥 slot %d job %s crashed: %s", idx, gid, e)
                self.galleries.drop_claim(gid)
                self.stats.failed += 1
                self._d_event(idx, "failed", gid)
                did_work = True
            finally:
                self._queue.task_done()
            if did_work:
                gap = random.uniform(self.s.fetch_gap_min, self.s.fetch_gap_max)
                log.info("⏸ slot %d resting %ds", idx, int(gap))
                self._d_state(idx, "resting", step=f"rest {int(gap)}s")
                await asyncio.sleep(gap)
                self._d_state(idx, "idle")

    async def _do_job(self, idx: int, client: TelegramClient, gid: str) -> bool:
        # v12.49: claim_ex adds the routing hint — "claimed_fallback" means
        # this gid double-failed 12h+ ago (or is a legacy FAILED_BOT2_ERROR
        # permanent skip) and goes STRAIGHT to the fallback bot.
        decision, _prior = self.galleries.claim_ex(gid)
        # v12.73: keep Bot 0's queue ledger in sync for user-triggered rows
        if decision == "done":
            self.galleries.mark_queue_status(gid, "completed")
        elif decision == "failed":
            self.galleries.mark_queue_status(gid, "failed",
                                             error="previously failed/parked")
        if decision == "done":
            self.stats.skipped_done += 1
            self._known_done.add(gid)          # v12.44: never re-queue
            log.info("⏭ %s already in DB channel — skipped", gid)
            return False
        if decision == "failed":
            self.stats.skipped_failed += 1
            self._known_failed.add(gid)        # v12.44: never re-queue
            log.info("🚫 %s previously failed — skipped", gid)
            return False
        if decision == "busy":
            self.stats.skipped_busy += 1
            log.info("🔒 %s claimed by another worker — skipped", gid)
            return False
        route_fallback_direct = (decision == "claimed_fallback")
        if route_fallback_direct:
            log.info("🛟 %s — both-bots park expired; routing STRAIGHT to "
                     "@%s (skipping @%s)", gid,
                     getattr(self.s, "fallback_username", "") or "?",
                     self.downloader_username)
        # v12.72: surface a live "stage" to the mini-app right after claim.
        self.galleries.set_progress(
            gid, stage="fallback_fetching" if route_fallback_direct else "fetching",
            slot=idx)
        # v12.73: mark the queue ledger row as processing (if it came from there)
        self.galleries.mark_queue_status(gid, "processing")
        self.stats.claimed += 1
        self.stats.in_flight[gid] = f"slot{idx}"
        log.info("🎯 slot %d claimed %s — starting job", idx, gid)
        channel = self._channel_per_slot[idx]
        bot2 = self._bot2_per_slot[idx]
        try:
            self._d_state(idx, "working", gid, step="loading meta")
            cache_row = await self.turso.get_gallery_row(gid)
            m = meta_mod.meta_from_cache(gid, cache_row)
            if m is None:
                # v12.45: no Turso cache row — this is a brand-new gallery
                # from a Recent page that BOT 1's details sweeper hasn't
                # cached yet. Ryan's rule: if Bot 2 doesn't find it, fetch
                # it itself. Direct /api/v2/galleries/<id> fetch, then
                # continue the job normally.
                outcome, m = await self._fetch_meta_direct(gid)
                if outcome == "defer":
                    # Transient upstream failure (403/429/5xx/network) —
                    # park the gid for 1h instead of dropping, so a
                    # Cloudflare flap doesn't waste cycles every scan.
                    self.galleries.defer_claim(gid, 3600)
                    self.stats.dropped += 1
                    self._d_event(idx, "dropped", gid)
                    return True
                if outcome == "dead":
                    # 404 — gallery is gone from nhentai; never retry.
                    self.galleries.mark_failed(
                        gid, status="FAILED_SCRAPE",
                        error="nhentai 404 on direct detail fetch")
                    self.stats.failed += 1
                    self._known_failed.add(gid)
                    self._d_event(idx, "failed", gid)
                    return True
                if m is None:
                    log.warning("⚠️ %s — direct fetch gave unusable meta, "
                                "dropping claim", gid)
                    self.galleries.drop_claim(gid)
                    self.stats.dropped += 1
                    # v12.88: cooldown, not a permanent blackhole.
                    self._retry_after[gid] = time.time() + self._retry_cooldown_s
                    self._d_event(idx, "dropped", gid)
                    return True
            timeout = compute_pdf_timeout(m.get("pages") or 0)
            caption = meta_mod.caption_for(m)
            log.info("📄 %s — %d pages, PDF wait budget %ds",
                     gid, m.get("pages") or 0, int(timeout))

            # ---------------- v12.49: dual-downloader chain ----------------
            primary_err = ""
            pdf_msg = None
            if route_fallback_direct and self._fallback_per_slot.get(idx) is not None:
                # Skip the primary entirely: it already failed this gid.
                pdf_msg, fb_err = await self._try_fallback_pdf(
                    idx, client, gid, "park-expired retry (primary skipped)")
                if pdf_msg is None:
                    # Fallback failed AGAIN on the park-expired retry — re-park.
                    log.error("\U0001f6ab %s — park-expired retry: fallback failed "
                              "again (%r) — re-parking 12h", gid, (fb_err or "")[:80])
                    self.galleries.set_both_failed(
                        gid, primary_err="park-expired retry (primary skipped)",
                        fallback_err=fb_err or "", park_s=self.s.both_fail_park_s)
                    await self._alert_log_channel(
                        "\U0001f6ab <b>Double failure (park-expired retry)</b> — "
                        "gallery <code>%s</code>\n"
                        "@%s: <code>%s</code>\n"
                        "Parked another 12h.\nhttps://nhentai.net/g/%s/" % (
                            gid, getattr(self.s, "fallback_username", "") or "?",
                            (fb_err or "timeout")[:150], gid))
                    self.stats.dropped += 1
                    self._d_event(idx, "fallback_error", gid)
                    return True
            elif route_fallback_direct:
                # Fallback bot unresolvable for this slot — keep the park.
                log.error("🛟 %s — fallback bot unavailable on slot %d; "
                          "re-parking 12h", gid, idx)
                self.galleries.set_both_failed(
                    gid, primary_err="park-expired retry but fallback bot "
                    "unresolved", fallback_err="unresolved",
                    park_s=self.s.both_fail_park_s)
                self.stats.dropped += 1
                self._d_event(idx, "dropped", gid)
                return True
            else:
                self._d_state(idx, "waiting_pdf", gid, title=m["title"],
                              pages=m.get("pages") or 0,
                              step="waiting @Gallery_DLBot")
                # v12.72: mini-app progress — primary bot is downloading now.
                self.galleries.set_progress(
                    gid, stage="downloading", total=int(m.get("pages") or 0), slot=idx)
                pdf_msg = await self._request_pdf(client, bot2, gid, timeout, slot=idx)
                if pdf_msg is None:
                    # v12.49 (operator spec): timeout is NOT an explicit
                    # "An error occurred" reply — keep the legacy drop path.
                    log.error("⏰ slot %d gallery %s — @%s timed out (%ds) — dropping",
                              idx, gid, self.downloader_username, int(timeout))
                    self.galleries.drop_claim(gid)
                    self.stats.dropped += 1
                    # v12.88: cooldown, not a permanent blackhole — the gid
                    # re-enters the scan after retry_cooldown_s. Durable
                    # failures keep using mark_failed / set_both_failed.
                    self._retry_after[gid] = time.time() + self._retry_cooldown_s
                    self._d_event(idx, "dropped", gid)
                    return True
                if isinstance(pdf_msg, str):
                    # ANY error text from the primary → immediate fallback.
                    primary_err = pdf_msg
                    pdf_msg = None
                    log.warning("🛟 %s — @Gallery_DLBot error %r — trying "
                                "fallback @%s", gid, primary_err[:100],
                                getattr(self.s, "fallback_username", "") or "?")
                    self._d_state(idx, "waiting_pdf", gid, title=m["title"],
                                  pages=m.get("pages") or 0,
                                  step=f"fallback @{getattr(self.s, 'fallback_username', '')}")
                    # v12.72: primary errored — mini-app sees "handed to backup".
                    self.galleries.set_progress(gid, stage="fallback_fetching",
                                                total=int(m.get("pages") or 0), slot=idx)
                    pdf_msg, fb_err = await self._try_fallback_pdf(
                        idx, client, gid, primary_err)
                    if pdf_msg is None:
                        # v12.73: queue ledger sync (both bots failed)
                        self.galleries.mark_queue_status(
                            gid, "failed", error="both bots failed (parked 12h)")
                        # BOTH bots failed → park 12h + log-channel alert.
                        log.error("🚫 %s — BOTH bots failed "
                                  "(primary=%r | fallback=%r) — parking 12h",
                                  gid, primary_err[:80], (fb_err or "")[:80])
                        self.galleries.set_both_failed(
                            gid, primary_err=primary_err,
                            fallback_err=fb_err or "",
                            park_s=self.s.both_fail_park_s)
                        await self._alert_log_channel(
                            "🚫 <b>Double failure</b> — gallery <code>%s</code>\n"
                            "@%s: <code>%s</code>\n"
                            "@%s: <code>%s</code>\n"
                            "Parked 12h; next retry goes straight to the "
                            "fallback bot.\nhttps://nhentai.net/g/%s/" % (
                                gid, self.downloader_username,
                                (primary_err or "timeout")[:150],
                                getattr(self.s, "fallback_username", "") or "?",
                                (fb_err or "timeout")[:150], gid))
                        self.stats.dropped += 1
                        self._d_event(idx, "fallback_error", gid)
                        return True
            # ---------------- /v12.49 chain — pdf_msg is a Message here ----
            log.info("📥 %s — PDF received", gid)

            self._d_state(idx, "working", gid, title=m["title"],
                          pages=m.get("pages") or 0, step="downloading cover")
            # v12.72: bot has the PDF — compiling phase.
            self.galleries.set_progress(gid, stage="compiling",
                                        total=int(m.get("pages") or 0), slot=idx)
            img_bytes, ext = await self._fetch_cover_bytes(m)
            self.galleries.refresh_claim(gid)

            self._d_state(idx, "posting", gid, title=m["title"],
                          pages=m.get("pages") or 0, step="posting to DB channel")
            # v12.72: about to post cover + forward PDF to database channel.
            self.galleries.set_progress(gid, stage="uploading",
                                        total=int(m.get("pages") or 0), slot=idx)
            async with self._channel_lock:
                cover_msg_id = await self._post_cover(client, channel, m,
                                                     caption, img_bytes, ext)
                if not cover_msg_id:
                    log.error("❌ %s — cover post failed", gid)
                    self.galleries.drop_claim(gid)
                    self.stats.failed += 1
                    self._d_event(idx, "failed", gid)
                    return True
                log.info("🖼 %s — spoiler cover posted (msg_id=%d)", gid, cover_msg_id)
                pdf_msg_id = await self._post_pdf(client, channel, bot2, pdf_msg, m)
                if not pdf_msg_id:
                    log.error("❌ %s — PDF forward failed; deleting cover", gid)
                    try:
                        await client.delete_messages(channel, [cover_msg_id])
                    except Exception as de:
                        log.warning("cover cleanup failed: %s", de)
                    self.galleries.drop_claim(gid)
                    self.stats.failed += 1
                    self._d_event(idx, "failed", gid)
                    return True
                log.info("📤 %s — PDF posted (msg_id=%d)", gid, pdf_msg_id)

            link = channel_link(int(self.s.db_channel_id), cover_msg_id)
            self.galleries.mark_completed(
                gid, title=m["title"], cover_msg_id=cover_msg_id,
                pdf_msg_id=pdf_msg_id, open_link=link, pages=m.get("pages") or 0)
            # v12.73: queue ledger sync (success)
            self.galleries.mark_queue_status(gid, "completed")
            await self.turso.put_state(gid, {
                "status": "COMPLETED", "cover_msg_id": cover_msg_id,
                "pdf_msg_id": pdf_msg_id, "open_link": link,
            })
            # ---- v1.22 BackupDB: best-effort mirror (NEVER fails job) ----
            try:
                await self._mirror_to_backup(client, channel, gid,
                                             cover_msg_id, pdf_msg_id)
            except Exception as be:  # noqa: BLE001
                log.warning("🗄 %s backup mirror failed (non-fatal): %s",
                            gid, be)

            self.stats.completed += 1
            self._known_done.add(gid)          # v12.44: never re-queue
            self.stats.last_finished_at = time.time()
            self._d_event(idx, "completed", gid)
            log.info("✅ %s DONE — cover=%s pdf=%s (total completed: %d)",
                     gid, cover_msg_id, pdf_msg_id, self.stats.completed)
            return True
        finally:
            self.stats.in_flight.pop(gid, None)

    async def _fetch_meta_direct(self, gid: str):
        """v12.45: fetch gallery detail straight from nhentai when Turso has
        no row. Returns (outcome, meta):
          ("ok",    dict)  — meta ready, continue the job
          ("defer", None)  — transient upstream failure; caller parks 1h
          ("dead",  None)  — 404; caller marks failed permanently
          ("ok",    None)  — fetch worked but payload unusable; caller drops
        Deliberately does NOT write the raw payload into the shared
        nhentai_cache table — raw v2 JSON breaks the Mini App detail view
        (BOT 1 normalizes for exactly this reason). BOT 1's sweeper will
        cache it properly later; this fetch only serves THIS job."""
        url = f"https://nhentai.net/api/v2/galleries/{gid}"
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://nhentai.net/",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0,
                                         follow_redirects=True) as h:
                r = await h.get(url, headers=headers)
        except Exception as e:
            log.warning("🌐 %s — direct detail fetch transport error: %s "
                        "(defer 1h)", gid, e)
            return ("defer", None)
        if r.status_code == 404:
            log.error("🌐 %s — nhentai 404 (gallery gone) — marking failed",
                      gid)
            return ("dead", None)
        if r.status_code != 200:
            log.warning("🌐 %s — direct detail fetch HTTP %s (defer 1h)",
                        gid, r.status_code)
            return ("defer", None)
        try:
            raw = r.json()
        except Exception:
            log.warning("🌐 %s — direct detail fetch non-JSON (defer 1h)", gid)
            return ("defer", None)
        if not isinstance(raw, dict) or not raw.get("id"):
            log.warning("🌐 %s — direct detail fetch unusable payload", gid)
            return ("ok", None)
        # Hoist v2 images.cover/thumbnail to top level so meta's
        # _construct_cover_url gets exact extensions (pass 2 of v12.43).
        images = raw.get("images") or {}
        if isinstance(images, dict):
            if "cover" not in raw and images.get("cover"):
                raw["cover"] = images["cover"]
            if "thumbnail" not in raw and images.get("thumbnail"):
                raw["thumbnail"] = images["thumbnail"]
        m = meta_mod.meta_from_cache(gid, {"payload": raw})
        if m is None:
            return ("ok", None)
        log.info("🌐 %s — meta fetched DIRECTLY from nhentai (%d pages, "
                 "no Turso row)", gid, m.get("pages") or 0)
        return ("ok", m)

    async def _fetch_cover_bytes(self, m: Dict[str, Any]) -> tuple[bytes, str]:
        # v12.43: meta may now return a CONSTRUCTED CDN URL whose extension
        # is a guess (cover.jpg when the real file is cover.png). Try the
        # given URL first, then the other common nhentai cover extensions
        # on the same base path — this is the "download it yourself" half
        # of the fix.
        img_bytes: bytes = b""
        ext = ".jpg"
        url0 = m["cover"]
        candidates = [url0]
        tail = url0.rsplit("/", 1)[-1]
        if "." in tail:
            base = url0.rsplit(".", 1)[0]
            for e in (".jpg", ".png", ".webp", ".gif"):
                u = base + e
                if u not in candidates:
                    candidates.append(u)
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                         headers=_COVER_HEADERS) as h:
                for url in candidates:
                    try:
                        r = await h.get(url)
                    except Exception:
                        continue
                    if r.status_code == 200 and len(r.content) >= 200:
                        img_bytes = r.content
                        ext = "." + url.rsplit(".", 1)[-1]
                        if url != url0:
                            log.info("🖼 %s — cover OK via extension "
                                     "fallback: %s", m["id"], url)
                        break
        except Exception as e:
            log.warning("🖼 cover download %s failed: %s", m["id"], e)
        if img_bytes:
            norm, ext = normalise_cover_bytes(img_bytes, source_url=m["cover"])
            if norm:
                img_bytes = norm
        return img_bytes, ext

    async def _post_cover(self, client: TelegramClient, channel, m: Dict[str, Any],
                          caption: str, img_bytes: bytes, ext: str) -> int:
        try:
            if img_bytes:
                buf = io.BytesIO(img_bytes)
                buf.name = f"cover_{m['id']}{ext or '.jpg'}"
                try:
                    uploaded = await client.upload_file(buf, file_name=buf.name)
                    spoiler_media = InputMediaUploadedPhoto(
                        file=uploaded, spoiler=True)
                    sent = await client.send_file(
                        channel, file=spoiler_media,
                        caption=caption, force_document=False)
                except FloodWaitError:
                    raise
                except Exception as e:
                    log.warning("🖼 spoiler upload failed (%s) — plain photo fallback", e)
                    buf.seek(0)
                    sent = await client.send_file(
                        channel, file=buf,
                        caption=caption, force_document=False)
            else:
                log.warning("🖼 %s — no cover bytes, text-only caption", m["id"])
                sent = await client.send_message(channel, caption)
            return int(getattr(sent, "id", 0) or 0)
        except FloodWaitError:
            raise
        except Exception as e:
            log.warning("🖼 cover post %s failed: %s", m.get("id"), e)
            return 0

    async def _request_pdf(self, client: TelegramClient, bot2, gid: str, timeout: float,
                           slot: Optional[int] = None, downloader_name=None):
        name = downloader_name or self.downloader_username
        rt = self._slot_runtime.setdefault(slot, {}) if slot is not None else {}
        rt.update(last_result="waiting", last_reply="", last_downloader=name)
        log.info("📨 slot %s DMing @%s: https://nhentai.net/g/%s/",
                 slot if slot is not None else "?", name, gid)
        sent = await client.send_message(bot2, f"https://nhentai.net/g/{gid}/")
        sent_id = int(getattr(sent, "id", 0) or 0)
        deadline = time.monotonic() + timeout
        last_progress_log = 0.0
        # v12.72: throttled Mongo progress writes (max 1 per 3s per slot).
        import re as _re_p
        _page_re = _re_p.compile(r"(\d{1,4})\s*/\s*(\d{1,4})|page\s+(\d{1,4})(?:\s+of\s+(\d{1,4}))?", _re_p.I)
        last_prog_write = 0.0
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_EVERY_S)
            try:
                msgs = await client.get_messages(bot2, limit=15)
            except FloodWaitError:
                raise
            except Exception:
                continue
            latest_incoming = None
            for msg in msgs or []:
                mid = int(getattr(msg, "id", 0) or 0)
                if mid <= sent_id:
                    continue
                if getattr(msg, "out", False):
                    continue
                doc = getattr(msg, "document", None)
                if doc is not None:
                    rt["last_result"] = "PDF received"
                    return msg
                if latest_incoming is None:
                    latest_incoming = msg
            if latest_incoming is not None:
                text = (getattr(latest_incoming, "raw_text", "") or "").strip()
                if text:
                    rt["last_reply"] = text.replace("\n", " ")[:140]
                    if _HARD_ERROR_PATTERNS.search(text):
                        rt["last_result"] = "downloader error"
                        return text[:300]
                    now = time.monotonic()
                    if now - last_progress_log > 30:
                        head = text.replace("\n", " ")[:80]
                        remain = int(deadline - now)
                        log.info("⏳ %s — waiting for PDF (%ds left) · Bot 2: %r",
                                 gid, remain, head)
                        last_progress_log = now
                    # v12.72: parse "12/29" style page counters from the
                    # primary bot's reply and write throttled progress.
                    if now - last_prog_write > 3.0:
                        try:
                            mtch = _page_re.search(text)
                            if mtch:
                                cur = int(mtch.group(1) or mtch.group(3) or 0)
                                tot = int(mtch.group(2) or mtch.group(4) or 0)
                                if cur > 0:
                                    self.galleries.set_progress(
                                        gid, stage="downloading", slot=slot,
                                        page=cur, total=(tot or None))
                                    last_prog_write = now
                        except Exception:
                            pass

        rt["last_result"] = "timeout"
        log.warning("PDF timeout: slot=%s bot=@%s gallery=%s last_reply=%r. "
                    "If silent, open this bot from that user account and check /start, "
                    "membership and permissions.", slot, name, gid, rt.get("last_reply", ""))

    async def _try_fallback_pdf(self, idx: int, client: TelegramClient,
                                gid: str, primary_err: str):
        """v12.49: single-flight fallback attempt to the backup downloader.

        Returns (Message, "") on a document reply, or (None, reason) on
        error text / timeout / unavailability. Layer A = in-process
        asyncio.Lock (serializes this Render instance's slots); Layer B =
        Mongo fallback_lease (covers multi-instance + restart windows).
        """
        fb_ent = self._fallback_per_slot.get(idx)
        if fb_ent is None:
            return None, "fallback bot unavailable (unresolved or unset)"
        timeout = float(getattr(self.s, "fallback_timeout_s", 300) or 300)
        async with self._fallback_lock:
            if self.fallback_lease is not None:
                ok = self.fallback_lease.acquire(idx, client)
                if not ok:
                    return None, "fallback lease held by another process"
            try:
                self._d_state(idx, "waiting_pdf", gid,
                              step=f"fallback @{getattr(self.s, 'fallback_username', '')}")
                res = await self._request_pdf(
                    client, fb_ent, gid, timeout, slot=idx,
                    downloader_name=getattr(self.s, "fallback_username", "") or "fallback")
            finally:
                if self.fallback_lease is not None:
                    try:
                        self.fallback_lease.release(idx, client)
                    except Exception:
                        pass
        if res is None:
            return None, f"no reply within {int(timeout)}s"
        if isinstance(res, str):
            return None, res
        return res, ""


    def _log_config(self):
        """v13.12: effective (token, chat) for log-channel posts.
        Chat  : Mongo /addlog value (bot2_workers{_id:'log_channel'}) wins;
                env LOG_CHANNEL_ID is only a fallback.
        Token : dedicated dashboard token env (BOT_2_PDF_FECTHER etc.), else
                the BOT2_TOKEN worker admin bot — no separate bot required."""
        token = (getattr(self.s, "log_bot_token", "") or "").strip()
        if not token:
            token = (os.getenv("BOT2_TOKEN") or "").strip()
        chat = ""
        db = getattr(self, "workers_db", None)
        if db is not None:
            try:
                from . import workers as _w
                chat = (_w.get_log_channel(db) or "").strip()
            except Exception:  # noqa: BLE001
                chat = ""
        if not chat:
            chat = (getattr(self.s, "log_channel_id", "") or "").strip()
        return token, chat

    async def _alert_log_channel(self, text: str) -> None:
        """v12.49: post a double-failure alert to the Bot 2 LOG_CHANNEL via
        the Bot API bot (same transport the dashboard uses). Rate-limited
        to one alert per 60s process-wide; failures are logged, never raise."""
        token, chat = self._log_config()
        if not token or not chat:
            log.warning("alert not sent (no log channel — /addlog <channel id> "
                        "in the Bot 2 admin bot, or LOG_CHANNEL_ID env): %s",
                        text[:120])
            return
        async with self._alert_lock:
            last = getattr(self, "_last_alert_at", 0.0)
            now = time.time()
            if now - last < 60:
                log.info("alert rate-limited (suppressed): %s", text[:120])
                return
            try:
                chat_id = int(chat)
            except (TypeError, ValueError):
                chat_id = chat
            try:
                async with httpx.AsyncClient(timeout=15.0) as h:
                    r = await h.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": text,
                              "parse_mode": "HTML",
                              "disable_web_page_preview": True})
                if r.status_code == 200:
                    self._last_alert_at = now
                    log.info("📢 double-failure alert sent to log channel")
                else:
                    log.warning("alert sendMessage HTTP %s: %s",
                                r.status_code, r.text[:200])
            except Exception as e:  # noqa: BLE001
                log.warning("alert send failed (non-fatal): %s", e)

    async def _post_pdf(self, client: TelegramClient, channel, bot2,
                        pdf_msg, m: Dict[str, Any]) -> int:
        try:
            res = await client.forward_messages(
                channel, pdf_msg.id, bot2, drop_author=True)
            msg = res[0] if isinstance(res, list) else res
            mid = int(getattr(msg, "id", 0) or 0)
            if mid:
                return mid
        except FloodWaitError:
            raise
        except Exception as e:
            log.warning("📤 pdf forward failed (%s) — trying download+reupload", e)
        try:
            data = await client.download_media(pdf_msg, bytes)
            if not data:
                return 0
            msg = await client.send_file(
                channel, data,
                caption=f"**{meta_mod._clean_title(m['title'])}**",
                file_name=f"{m['id']}.pdf",
                force_document=True,
            )
            return int(getattr(msg, "id", 0) or 0)
        except FloodWaitError:
            raise
        except Exception as e:
            log.warning("📤 pdf reupload failed: %s", e)
            return 0

    async def _mirror_to_backup(self, client, channel, gid: str,
                                cover_msg_id: int, pdf_msg_id: int) -> None:
        """v1.22 BackupDB live mirror (Bot2Fetcher side).

        Bot2Fetcher deploys with Root Directory = `Bot2Fetcher`, so it
        CANNOT import the repo-root backup_db.py — this is the deliberate,
        minimal twin of backup_db.mirror_pair_to_backup(). Same failure
        policy: log + counter only, never block the user download.
        """
        from pymongo import MongoClient  # lazy: only when mirror runs
        mdb = MongoClient(os.environ["MONGO_URI"])[
            (os.environ.get("MONGO_DB_NAME") or "relaybot").strip()]
        state = mdb["backup_state"].find_one({"_id": "state"}) or {}
        bch_id = int(os.environ.get("BACKUP_DB_CHANNEL_ID", "") or 0) or int(
            state.get("backup_channel_id") or 0)
        if not bch_id:
            return  # no backup channel configured yet — silent no-op
        res = await client.forward_messages(
            bch_id, [int(cover_msg_id), int(pdf_msg_id)], channel,
            drop_author=True)
        msgs = res if isinstance(res, list) else [res]
        bc = int(getattr(msgs[0], "id", 0) or 0) if len(msgs) > 0 else 0
        bp = int(getattr(msgs[1], "id", 0) or 0) if len(msgs) > 1 else 0
        if bc or bp:
            upd = {"backup_channel_id": bch_id,
                   "backup_status": "ok" if bp else "cover_only",
                   "backed_up_at": time.time()}
            if bc:
                upd["backup_cover_msg_id"] = bc
            if bp:
                upd["backup_pdf_msg_id"] = bp
            mdb["galleries"].update_one({"_id": str(gid)}, {"$set": upd})
            mdb["counters"].update_one({"_id": "backupdb"},
                                       {"$inc": {"mirrored_ok": 1}},
                                       upsert=True)
            log.info("🗄 %s mirrored to BackupDB cover=%s pdf=%s", gid, bc, bp)
        else:
            mdb["counters"].update_one({"_id": "backupdb"},
                                       {"$inc": {"mirrored_fail": 1}},
                                       upsert=True)
            log.warning("🗄 %s backup forward returned no ids", gid)

    async def run(self) -> None:
        self._main_loop = asyncio.get_running_loop()
        await self.start()
        # v13.10: slot tasks are owned by reconcile_workers (hot-reload);
        # the producer + the reconcile watcher are the fixed tasks here.
        tasks = [asyncio.create_task(self._producer()),
                 asyncio.create_task(self._reconcile_loop())]
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        for t in list(self._slot_tasks.values()):
            t.cancel()
        await asyncio.gather(*tasks, *list(self._slot_tasks.values()),
                             return_exceptions=True)
