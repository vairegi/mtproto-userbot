"""
fetcher.py — the Bot2Fetcher pipeline.

v12.40g — PDF-WAIT FIX. Previous version treated @Gallery_DLBot's
progress messages ("Starting upload...", "📤 Uploading PDF...", the
title-echo confirmation, the "‣ Status: Uploading ‣ Progress: […]"
progress bar) as errors and bailed after ~5s, so the PDF never arrived
in the DB channel.

New behaviour of _request_pdf():
  * poll every 3s for messages FROM the bot (out=False) received AFTER
    our DM
  * if any message carries a document -> return it, done
  * if the newest text message matches a HARD ERROR pattern (❌, ⚠️,
    'error:', 'failed', 'not found', 'invalid', 'sorry') -> stop and
    tombstone as FAILED_BOT2_ERROR
  * anything else (title echoes, "Starting upload…", progress bars,
    percent updates, etc.) is IGNORED — keep waiting for the document
    until the adaptive timeout fires
"""
from __future__ import annotations

import asyncio
import io
import logging
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

# Real error signals from @Gallery_DLBot. Case-insensitive substring match.
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
            "skipped_done": self.skipped_done,
            "skipped_failed": self.skipped_failed,
            "skipped_busy": self.skipped_busy,
            "in_flight": dict(self.in_flight),
            "last_finished_at": int(self.last_finished_at),
        }


class Fetcher:
    def __init__(self, settings, galleries, turso, stats: Stats):
        self.s = settings
        self.galleries = galleries
        self.turso = turso
        self.stats = stats
        self.clients: list[TelegramClient] = []
        self._channel_lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()
        self._channel = None
        self._bot2 = None

    async def start(self) -> None:
        for i, sess in enumerate(self.s.sessions, 1):
            c = TelegramClient(StringSession(sess), self.s.api_id, self.s.api_hash)
            await c.connect()
            if not await c.is_user_authorized():
                raise RuntimeError(f"STRING_SESSION slot {i} is not authorized")
            me = await c.get_me()
            uname = getattr(me, "username", None) or "(no username)"
            log.info("🤖 slot %d authorized as @%s (id=%s)", i, uname, me.id)
            self.clients.append(c)
        ch = self.s.db_channel_id
        self._channel = await self.clients[0].get_entity(int(ch) if ch.lstrip("-").isdigit() else ch)
        self._bot2 = await self.clients[0].get_entity(self.s.bot2_username)
        log.info("📺 DB channel: %s | downloader bot: @%s",
                 self.s.db_channel_id, self.s.bot2_username)

    async def stop(self) -> None:
        self._stop.set()
        for c in self.clients:
            try:
                await c.disconnect()
            except Exception:
                pass

    async def _build_queue_order(self) -> list[str]:
        recent_ids = await self.turso.list_recent_search_ids()
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
            log.info("📡 scan cycle %d: %d candidate galleries queued",
                     self.stats.cycles + 1, len(ids))
            for gid in ids:
                if self._stop.is_set():
                    break
                await self._queue.put(gid)
            self.stats.cycles += 1
            while not self._queue.empty() and not self._stop.is_set():
                await asyncio.sleep(10)
            log.info("💤 queue drained — sleeping %ds before next scan",
                     self.s.rescan_sleep_s)
            await asyncio.sleep(self.s.rescan_sleep_s)

    async def _slot_loop(self, idx: int, client: TelegramClient) -> None:
        log.info("🧵 slot %d worker started", idx)
        while not self._stop.is_set():
            try:
                gid = await asyncio.wait_for(self._queue.get(), timeout=15)
            except asyncio.TimeoutError:
                continue
            did_work = False
            try:
                did_work = await self._do_job(idx, client, gid)
            except FloodWaitError as fw:
                wait = int(getattr(fw, "seconds", 30)) + 5
                log.warning("🚧 slot %d FloodWait %ss on %s — sleeping, will retry",
                            idx, wait, gid)
                await asyncio.sleep(wait)
                await self._queue.put(gid)
                did_work = False
            except Exception as e:
                log.exception("💥 slot %d job %s crashed: %s", idx, gid, e)
                self.stats.failed += 1
                did_work = True
            finally:
                self._queue.task_done()
            if did_work:
                gap = random.uniform(self.s.fetch_gap_min, self.s.fetch_gap_max)
                log.info("⏸ slot %d resting %ds", idx, int(gap))
                await asyncio.sleep(gap)

    async def _do_job(self, idx: int, client: TelegramClient, gid: str) -> bool:
        decision = self.galleries.claim(gid)
        if decision == "done":
            self.stats.skipped_done += 1
            log.info("⏭ %s already in DB channel — skipped", gid)
            return False
        if decision == "failed":
            self.stats.skipped_failed += 1
            log.info("🚫 %s previously failed — skipped", gid)
            return False
        if decision == "busy":
            self.stats.skipped_busy += 1
            log.info("🔒 %s claimed by another worker — skipped", gid)
            return False
        self.stats.claimed += 1
        self.stats.in_flight[gid] = f"slot{idx}"
        log.info("🎯 slot %d claimed %s — starting job", idx, gid)
        try:
            cache_row = await self.turso.get_gallery_row(gid)
            m = meta_mod.meta_from_cache(gid, cache_row)
            if m is None:
                log.warning("⚠️ %s — no cached meta, marking FAILED_SCRAPE", gid)
                self.galleries.mark_failed(gid, status="FAILED_SCRAPE",
                                           error="no Turso cache row / no cover")
                await self.turso.put_state(gid, {"status": "FAILED_SCRAPE"})
                self.stats.failed += 1
                return True
            timeout = compute_pdf_timeout(m.get("pages") or 0)
            caption = meta_mod.caption_for(m)
            log.info("📄 %s — %d pages, PDF wait budget %ds",
                     gid, m.get("pages") or 0, int(timeout))

            async with self._channel_lock:
                cover_msg_id = await self._post_cover(client, m, caption)
                if not cover_msg_id:
                    log.error("❌ %s — cover post failed", gid)
                    self.galleries.mark_failed(gid, status="FAILED_OTHER",
                                               error="cover post failed")
                    self.stats.failed += 1
                    return True
                log.info("🖼 %s — spoiler cover posted to DB channel (msg_id=%d)",
                         gid, cover_msg_id)
                self.galleries.refresh_claim(gid)
                pdf_msg = await self._request_pdf(client, gid, timeout)
                if pdf_msg is None:
                    log.error("⏰ %s — @Gallery_DLBot timed out (%ds)", gid, int(timeout))
                    self.galleries.mark_failed(gid, status="FAILED_TIMEOUT",
                                               error=f"no PDF within {int(timeout)}s")
                    await self.turso.put_state(gid, {"status": "FAILED_TIMEOUT"})
                    self.stats.failed += 1
                    return True
                if isinstance(pdf_msg, str):
                    log.error("🤖❌ %s — Bot 2 hard error: %s", gid, pdf_msg[:150])
                    self.galleries.mark_failed(gid, status="FAILED_BOT2_ERROR",
                                               error=pdf_msg)
                    await self.turso.put_state(gid, {"status": "FAILED_BOT2_ERROR"})
                    self.stats.failed += 1
                    return True
                log.info("📥 %s — PDF received from @Gallery_DLBot", gid)
                pdf_msg_id = await self._post_pdf(client, pdf_msg, m)
                if not pdf_msg_id:
                    log.error("❌ %s — PDF forward to DB channel failed", gid)
                    self.galleries.mark_failed(gid, status="FAILED_OTHER",
                                               error="pdf forward failed")
                    self.stats.failed += 1
                    return True
                log.info("📤 %s — PDF posted to DB channel (msg_id=%d)",
                         gid, pdf_msg_id)

            link = channel_link(int(self.s.db_channel_id), cover_msg_id)
            self.galleries.mark_completed(
                gid, title=m["title"], cover_msg_id=cover_msg_id,
                pdf_msg_id=pdf_msg_id, open_link=link, pages=m.get("pages") or 0)
            await self.turso.put_state(gid, {
                "status": "COMPLETED", "cover_msg_id": cover_msg_id,
                "pdf_msg_id": pdf_msg_id, "open_link": link,
            })
            self.stats.completed += 1
            self.stats.last_finished_at = time.time()
            log.info("✅ %s DONE — cover=%s pdf=%s (total completed: %d)",
                     gid, cover_msg_id, pdf_msg_id, self.stats.completed)
            return True
        finally:
            self.stats.in_flight.pop(gid, None)

    async def _post_cover(self, client: TelegramClient, m: Dict[str, Any],
                          caption: str) -> int:
        img_bytes: bytes = b""
        ext = ".jpg"
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                         headers=_COVER_HEADERS) as h:
                r = await h.get(m["cover"])
                if r.status_code == 200 and len(r.content) >= 200:
                    img_bytes = r.content
        except Exception as e:
            log.warning("🖼 cover download %s failed: %s", m["id"], e)

        try:
            if img_bytes:
                norm, ext = normalise_cover_bytes(img_bytes, source_url=m["cover"])
                if norm:
                    img_bytes = norm
                buf = io.BytesIO(img_bytes)
                buf.name = f"cover_{m['id']}{ext or '.jpg'}"
                try:
                    uploaded = await client.upload_file(buf, file_name=buf.name)
                    spoiler_media = InputMediaUploadedPhoto(
                        file=uploaded, spoiler=True)
                    sent = await client.send_file(
                        self._channel, file=spoiler_media,
                        caption=caption, force_document=False)
                except FloodWaitError:
                    raise
                except Exception as e:
                    log.warning("🖼 spoiler upload failed (%s) — plain photo fallback", e)
                    buf.seek(0)
                    sent = await client.send_file(
                        self._channel, file=buf,
                        caption=caption, force_document=False)
            else:
                log.warning("🖼 %s — no cover bytes, text-only caption", m["id"])
                sent = await client.send_message(self._channel, caption)
            return int(getattr(sent, "id", 0) or 0)
        except FloodWaitError:
            raise
        except Exception as e:
            log.warning("🖼 cover post %s failed: %s", m.get("id"), e)
            return 0

    async def _request_pdf(self, client: TelegramClient, gid: str, timeout: float):
        """DM the URL and wait for a document reply.
        Returns:
          Telethon Message  -> the PDF message
          str               -> hard error from Bot 2 (tombstone as BOT2_ERROR)
          None              -> timed out with no document
        Progress/status messages ("Starting upload...", "Uploading PDF...",
        "‣ Status: … ‣ Progress: …", title echoes, etc.) are IGNORED and
        polling continues until the doc arrives or the timeout hits.
        """
        log.info("📨 DMing @%s: https://nhentai.net/g/%s/",
                 self.s.bot2_username, gid)
        sent = await client.send_message(self._bot2, f"https://nhentai.net/g/{gid}/")
        sent_id = int(getattr(sent, "id", 0) or 0)
        deadline = time.monotonic() + timeout
        last_progress_log = 0.0
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_EVERY_S)
            try:
                msgs = await client.get_messages(self._bot2, limit=15)
            except FloodWaitError:
                raise
            except Exception:
                continue

            # scan newest -> oldest, only messages FROM the bot AFTER our DM
            latest_incoming = None
            for msg in msgs or []:
                mid = int(getattr(msg, "id", 0) or 0)
                if mid <= sent_id:
                    continue
                if getattr(msg, "out", False):
                    continue
                # First: check for the document across ALL new messages —
                # not just the newest — in case a text update came in after.
                doc = getattr(msg, "document", None)
                if doc is not None:
                    return msg
                if latest_incoming is None:
                    latest_incoming = msg

            # No document yet. Only inspect the latest incoming text.
            if latest_incoming is not None:
                text = (getattr(latest_incoming, "raw_text", "") or "").strip()
                if text:
                    # Only HARD ERROR patterns abort the wait.
                    if _HARD_ERROR_PATTERNS.search(text):
                        return text[:300]
                    # Otherwise it's progress/status — throttle-log and wait.
                    now = time.monotonic()
                    if now - last_progress_log > 30:
                        head = text.replace("\n", " ")[:80]
                        remain = int(deadline - now)
                        log.info("⏳ %s — waiting for PDF (%ds left) · Bot 2: %r",
                                 gid, remain, head)
                        last_progress_log = now
        return None

    async def _post_pdf(self, client: TelegramClient, pdf_msg, m: Dict[str, Any]) -> int:
        try:
            res = await client.forward_messages(
                self._channel, pdf_msg.id, self._bot2, drop_author=True)
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
                self._channel, data,
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

    async def run(self) -> None:
        await self.start()
        tasks = [asyncio.create_task(self._producer())]
        for i, c in enumerate(self.clients, 1):
            tasks.append(asyncio.create_task(self._slot_loop(i, c)))
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
