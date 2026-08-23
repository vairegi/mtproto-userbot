"""
fetcher.py — the Bot2Fetcher pipeline.

v12.40c: producer switched to paged Turso scan (list_gallery_ids +
list_recent_search_ids), payload fetched on-demand per job via
turso.get_gallery_row(gid). No more 15k-row mem_hrana_response blowups.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Dict

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

from . import meta as meta_mod
from .pdf_timing import compute_pdf_timeout

log = logging.getLogger("bot2fetcher.fetcher")

POLL_EVERY_S = 5.0


def channel_link(channel_id: int, msg_id: int) -> str:
    raw = str(channel_id)
    if raw.startswith("-100"):
        raw = raw[4:]
    return f"https://t.me/c/{raw}/{msg_id}"


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
            log.info("slot %d authorized as @%s (%s)", i, uname, me.id)
            self.clients.append(c)
        ch = self.s.db_channel_id
        self._channel = await self.clients[0].get_entity(int(ch) if ch.lstrip("-").isdigit() else ch)
        self._bot2 = await self.clients[0].get_entity(self.s.bot2_username)

    async def stop(self) -> None:
        self._stop.set()
        for c in self.clients:
            try:
                await c.disconnect()
            except Exception:
                pass

    async def _build_queue_order(self) -> list[str]:
        """Newest-first: cached recent-sort pages, then every gallery:* row
        newest-cached-first, deduped."""
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
                log.exception("producer scan failed: %s", e)
                await asyncio.sleep(30)
                continue
            self.stats.scanned = len(ids)
            log.info("scan: %d candidate galleries", len(ids))
            for gid in ids:
                if self._stop.is_set():
                    break
                await self._queue.put(gid)
            self.stats.cycles += 1
            while not self._queue.empty() and not self._stop.is_set():
                await asyncio.sleep(10)
            await asyncio.sleep(self.s.rescan_sleep_s)

    async def _slot_loop(self, idx: int, client: TelegramClient) -> None:
        while not self._stop.is_set():
            try:
                gid = await asyncio.wait_for(self._queue.get(), timeout=15)
            except asyncio.TimeoutError:
                continue
            try:
                await self._do_job(idx, client, gid)
            except FloodWaitError as fw:
                wait = int(getattr(fw, "seconds", 30)) + 5
                log.warning("slot %d FloodWait %ss on %s — sleeping", idx, wait, gid)
                await asyncio.sleep(wait)
                await self._queue.put(gid)
            except Exception as e:
                log.exception("slot %d job %s crashed: %s", idx, gid, e)
                self.stats.failed += 1
            finally:
                self._queue.task_done()
            await asyncio.sleep(random.uniform(self.s.fetch_gap_min, self.s.fetch_gap_max))

    async def _do_job(self, idx: int, client: TelegramClient, gid: str) -> None:
        decision = self.galleries.claim(gid)
        if decision == "done":
            self.stats.skipped_done += 1
            return
        if decision == "failed":
            self.stats.skipped_failed += 1
            return
        if decision == "busy":
            self.stats.skipped_busy += 1
            return
        self.stats.claimed += 1
        self.stats.in_flight[gid] = f"slot{idx}"
        log.info("slot %d claimed %s", idx, gid)
        try:
            cache_row = await self.turso.get_gallery_row(gid)
            m = meta_mod.meta_from_cache(gid, cache_row)
            if m is None:
                m = await meta_mod.meta_from_upstream(gid)
            if m is None or not m.get("cover"):
                self.galleries.mark_failed(gid, status="FAILED_SCRAPE",
                                           error="no meta / cover")
                await self.turso.put_state(gid, {"status": "FAILED_SCRAPE"})
                self.stats.failed += 1
                return
            timeout = compute_pdf_timeout(m.get("pages") or 0)

            async with self._channel_lock:
                cover_msg_id = await self._post_cover(client, m)
                if not cover_msg_id:
                    self.galleries.mark_failed(gid, status="FAILED_OTHER",
                                               error="cover post failed")
                    self.stats.failed += 1
                    return
                self.galleries.refresh_claim(gid)
                pdf_msg = await self._request_pdf(client, gid, timeout)
                if pdf_msg is None:
                    self.galleries.mark_failed(gid, status="FAILED_TIMEOUT",
                                               error=f"no PDF within {int(timeout)}s")
                    await self.turso.put_state(gid, {"status": "FAILED_TIMEOUT"})
                    self.stats.failed += 1
                    return
                if isinstance(pdf_msg, str):
                    self.galleries.mark_failed(gid, status="FAILED_BOT2_ERROR",
                                               error=pdf_msg)
                    await self.turso.put_state(gid, {"status": "FAILED_BOT2_ERROR"})
                    self.stats.failed += 1
                    return
                pdf_msg_id = await self._post_pdf(client, pdf_msg, m)
                if not pdf_msg_id:
                    self.galleries.mark_failed(gid, status="FAILED_OTHER",
                                               error="pdf forward failed")
                    self.stats.failed += 1
                    return

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
            log.info("slot %d COMPLETED %s (cover=%s pdf=%s)",
                     idx, gid, cover_msg_id, pdf_msg_id)
        finally:
            self.stats.in_flight.pop(gid, None)

    async def _post_cover(self, client: TelegramClient, m: Dict[str, Any]) -> int:
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as h:
                r = await h.get(m["cover"], headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200 or len(r.content) < 1000:
                    return 0
                img = r.content
            msg = await client.send_file(
                self._channel, img,
                caption=meta_mod.caption_for(m),
                file_name=f"cover_{m['id']}.jpg",
            )
            return int(getattr(msg, "id", 0) or 0)
        except FloodWaitError:
            raise
        except Exception as e:
            log.warning("cover post %s failed: %s", m.get("id"), e)
            return 0

    async def _request_pdf(self, client: TelegramClient, gid: str, timeout: float):
        sent = await client.send_message(self._bot2, f"https://nhentai.net/g/{gid}/")
        sent_id = int(getattr(sent, "id", 0) or 0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_EVERY_S)
            try:
                msgs = await client.get_messages(self._bot2, limit=10)
            except FloodWaitError:
                raise
            except Exception:
                continue
            for msg in msgs or []:
                if int(getattr(msg, "id", 0) or 0) <= sent_id:
                    continue
                if getattr(msg, "out", False):
                    continue
                doc = getattr(msg, "document", None)
                if doc is not None:
                    return msg
                text = (getattr(msg, "raw_text", "") or "").strip()
                if text:
                    low = text.lower()
                    if any(w in low for w in ("wait", "download", "process", "queue", "⏳")):
                        continue
                    return text[:300]
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
            log.warning("pdf forward failed (%s) — trying download+reupload", e)
        try:
            data = await client.download_media(pdf_msg, bytes)
            if not data:
                return 0
            msg = await client.send_file(
                self._channel, data,
                caption=f"📖 {m['title']}\n🆔 {m['id']}",
                file_name=f"{m['id']}.pdf",
            )
            return int(getattr(msg, "id", 0) or 0)
        except FloodWaitError:
            raise
        except Exception as e:
            log.warning("pdf reupload failed: %s", e)
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
