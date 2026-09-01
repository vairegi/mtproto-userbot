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
from typing import Any, Dict

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
        self._channel_lock = asyncio.Lock()
        # v12.44: process-lifetime memory of gids already delivered or
        # permanently failed. The producer drops these BEFORE the queue so
        # slots never spend claim+cache-read time on known-finished work.
        self._known_done: set = set()
        self._known_failed: set = set()
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
        if self.dash:
            try:
                self.dash.slot_state(idx, state, gid, title, pages, step)
            except Exception:
                pass

    def _d_event(self, idx, kind, gid):
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
                    and peer_cache.has(slot=idx, kind="bot2_user", session_fingerprint=fp)
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
            self._bot2_per_slot[idx] = await _get_entity("bot2_user", self.s.bot2_username)
            log.info("🤖 slot %d resolved downloader bot: @%s",
                     idx, self.s.bot2_username)
        except Exception as e:
            raise RuntimeError(
                f"slot {idx}: cannot resolve BOT2_USERNAME=@{self.s.bot2_username} — {e}. "
                f"Have this userbot DM @{self.s.bot2_username} once, then restart."
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

    async def start(self) -> None:
        for i, sess in enumerate(self.s.sessions, 1):
            c = TelegramClient(StringSession(sess), self.s.api_id, self.s.api_hash)
            await c.connect()
            if not await c.is_user_authorized():
                raise RuntimeError(f"STRING_SESSION slot {i} is not authorized")
            me = await c.get_me()
            uname = getattr(me, "username", None) or "(no username)"
            log.info("🤖 slot %d authorized as @%s (id=%s)", i, uname, me.id)
            if self.dash:
                self.dash.set_account(i, uname)
            self.clients.append(c)
            await self._warm_and_resolve(i, c)

    async def stop(self) -> None:
        self._stop.set()
        for c in self.clients:
            try:
                await c.disconnect()
            except Exception:
                pass

    async def _build_queue_order(self) -> list[str]:
        self._d_scan(phase="reading recent pages…")
        recent_ids = await self.turso.list_recent_search_ids()
        self._d_scan(phase="listing cache…")
        gallery_rows = await self.turso.list_gallery_ids()
        gallery_rows.sort(key=lambda r: r["cached_at"], reverse=True)
        seen: set = set()
        ordered: list[str] = []
        for gid in recent_ids:
            if gid not in seen:
                seen.add(gid); ordered.append(gid)
        for r in gallery_rows:
            gid = r["gid"]
            if gid not in seen:
                seen.add(gid); ordered.append(gid)
        # v12.44: drop gids we already know are delivered / permanently
        # failed. On a warm process this eliminates nearly every
        # "⏭ already in DB channel — skipped" cycle (the 146-PDFs-in-15h
        # problem: most slot time was being spent re-claiming known rows).
        before = len(ordered)
        ordered = [g for g in ordered
                   if g not in self._known_done and g not in self._known_failed]
        filtered = before - len(ordered)
        if filtered:
            log.info("🧹 queue: pre-filtered %d known-done/failed gids "
                     "(%d candidates left)", filtered, len(ordered))
        return ordered

    async def _producer(self) -> None:
        while not self._stop.is_set():
            try:
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
                await asyncio.sleep(10)
            self._d_scan(phase="queue drained — idle")
            log.info("💤 queue drained — sleeping %ds before next scan",
                     self.s.rescan_sleep_s)
            await asyncio.sleep(self.s.rescan_sleep_s)

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
                     self.s.bot2_username)
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
                    self._known_failed.add(gid)
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
                pdf_msg = await self._request_pdf(client, bot2, gid, timeout)
                if pdf_msg is None:
                    # v12.49 (operator spec): timeout is NOT an explicit
                    # "An error occurred" reply — keep the legacy drop path.
                    log.error("⏰ %s — @Gallery_DLBot timed out (%ds) — dropping",
                              gid, int(timeout))
                    self.galleries.drop_claim(gid)
                    self.stats.dropped += 1
                    self._known_failed.add(gid)
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
                    pdf_msg, fb_err = await self._try_fallback_pdf(
                        idx, client, gid, primary_err)
                    if pdf_msg is None:
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
                                gid, self.s.bot2_username,
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
            img_bytes, ext = await self._fetch_cover_bytes(m)
            self.galleries.refresh_claim(gid)

            self._d_state(idx, "posting", gid, title=m["title"],
                          pages=m.get("pages") or 0, step="posting to DB channel")
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

    async def _request_pdf(self, client: TelegramClient, bot2, gid: str, timeout: float):
        log.info("📨 DMing @%s: https://nhentai.net/g/%s/",
                 self.s.bot2_username, gid)
        sent = await client.send_message(bot2, f"https://nhentai.net/g/{gid}/")
        sent_id = int(getattr(sent, "id", 0) or 0)
        deadline = time.monotonic() + timeout
        last_progress_log = 0.0
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
                    return msg
                if latest_incoming is None:
                    latest_incoming = msg
            if latest_incoming is not None:
                text = (getattr(latest_incoming, "raw_text", "") or "").strip()
                if text:
                    if _HARD_ERROR_PATTERNS.search(text):
                        return text[:300]
                    now = time.monotonic()
                    if now - last_progress_log > 30:
                        head = text.replace("\n", " ")[:80]
                        remain = int(deadline - now)
                        log.info("⏳ %s — waiting for PDF (%ds left) · Bot 2: %r",
                                 gid, remain, head)
                        last_progress_log = now
        return None

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
                res = await self._request_pdf(client, fb_ent, gid, timeout)
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

    async def _alert_log_channel(self, text: str) -> None:
        """v12.49: post a double-failure alert to the Bot 2 LOG_CHANNEL via
        the Bot API bot (same transport the dashboard uses). Rate-limited
        to one alert per 60s process-wide; failures are logged, never raise."""
        token = (getattr(self.s, "log_bot_token", "") or "").strip()
        chat = (getattr(self.s, "log_channel_id", "") or "").strip()
        if not token or not chat:
            log.warning("alert not sent (LOG_CHANNEL_ID / log bot token unset): %s",
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
        await self.start()
        tasks = [asyncio.create_task(self._producer())]
        for i, c in enumerate(self.clients, 1):
            tasks.append(asyncio.create_task(self._slot_loop(i, c)))
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
