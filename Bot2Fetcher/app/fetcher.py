"""
fetcher.py — the Bot2Fetcher pipeline.

v12.40j — per-slot peer resolution.

BUG in v12.40i: only slot 1's client resolved the DB channel and
@Gallery_DLBot. Slot 2 was handed slot 1's Entity object, but Telethon's
peer table is per-session — a peer slot 1 knows can be
PeerIdInvalidError on slot 2 until slot 2 has seen it in a dialog. Every
slot 2 job crashed with:
    PeerIdInvalidError: An invalid Peer was used

Fix: each slot now
  1. iter_dialogs(limit=200) once on startup — warms its own peer cache
  2. get_input_entity(...) on ITS OWN client — cached as
     self._channel_per_slot[idx] / self._bot2_per_slot[idx]
Every subsequent send_message / send_file / forward_messages uses the
input-entity that belongs to the slot doing the call. No cross-session
peer sharing anywhere.
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
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()
        # Per-slot resolved input entities (Telethon peer table is per session)
        self._channel_per_slot: dict[int, Any] = {}
        self._bot2_per_slot: dict[int, Any] = {}

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
        """Warm this slot's peer cache with iter_dialogs, then resolve BOTH
        the DB channel and @Gallery_DLBot as input entities on THIS client.
        This is the fix for v12.40i PeerIdInvalidError on slot 2."""
        try:
            n = 0
            async for _ in client.iter_dialogs(limit=200):
                n += 1
            log.info("🧭 slot %d peer cache warmed (%d dialogs)", idx, n)
        except Exception as e:
            log.warning("🧭 slot %d iter_dialogs failed: %s", idx, e)
        ch_key = self.s.db_channel_id
        try:
            ch_key = int(ch_key) if str(ch_key).lstrip("-").isdigit() else ch_key
            self._channel_per_slot[idx] = await client.get_input_entity(ch_key)
            log.info("📺 slot %d resolved DB channel: %s", idx, self.s.db_channel_id)
        except Exception as e:
            raise RuntimeError(
                f"slot {idx}: cannot resolve DB_CHANNEL_ID={self.s.db_channel_id!r} — {e}. "
                f"Make sure this userbot account is a MEMBER of the channel "
                f"(admin is fine, membership is what registers the peer)."
            ) from e
        try:
            self._bot2_per_slot[idx] = await client.get_input_entity(self.s.bot2_username)
            log.info("🤖 slot %d resolved downloader bot: @%s",
                     idx, self.s.bot2_username)
        except Exception as e:
            raise RuntimeError(
                f"slot {idx}: cannot resolve BOT2_USERNAME=@{self.s.bot2_username} — {e}. "
                f"Have this userbot DM @{self.s.bot2_username} once from the "
                f"Telegram app (any /start), then restart the service."
            ) from e

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
            # Per-slot peer resolution — MUST happen before the slot loop
            # starts sending anything.
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
        channel = self._channel_per_slot[idx]
        bot2 = self._bot2_per_slot[idx]
        try:
            self._d_state(idx, "working", gid, step="loading meta")
            cache_row = await self.turso.get_gallery_row(gid)
            m = meta_mod.meta_from_cache(gid, cache_row)
            if m is None:
                log.warning("⚠️ %s — no cached meta, dropping claim", gid)
                self.galleries.drop_claim(gid)
                self.stats.dropped += 1
                self._d_event(idx, "dropped", gid)
                return True
            timeout = compute_pdf_timeout(m.get("pages") or 0)
            caption = meta_mod.caption_for(m)
            log.info("📄 %s — %d pages, PDF wait budget %ds",
                     gid, m.get("pages") or 0, int(timeout))

            self._d_state(idx, "waiting_pdf", gid, title=m["title"],
                          pages=m.get("pages") or 0, step="waiting @Gallery_DLBot")
            pdf_msg = await self._request_pdf(client, bot2, gid, timeout)
            if pdf_msg is None:
                log.error("⏰ %s — @Gallery_DLBot timed out (%ds) — dropping, no cover posted",
                          gid, int(timeout))
                self.galleries.drop_claim(gid)
                self.stats.dropped += 1
                self._d_event(idx, "dropped", gid)
                return True
            if isinstance(pdf_msg, str):
                log.error("🤖❌ %s — Bot 2 hard error, dropping (no cover posted): %s",
                          gid, pdf_msg[:150])
                self.galleries.drop_claim(gid)
                self.stats.dropped += 1
                self._d_event(idx, "dropped", gid)
                return True
            log.info("📥 %s — PDF received from @Gallery_DLBot", gid)

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
                    log.error("❌ %s — cover post failed AFTER PDF was in hand — dropping",
                              gid)
                    self.galleries.drop_claim(gid)
                    self.stats.failed += 1
                    self._d_event(idx, "failed", gid)
                    return True
                log.info("🖼 %s — spoiler cover posted (msg_id=%d)", gid, cover_msg_id)
                pdf_msg_id = await self._post_pdf(client, channel, bot2, pdf_msg, m)
                if not pdf_msg_id:
                    log.error("❌ %s — PDF forward failed after cover; deleting cover",
                              gid)
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
            self.stats.completed += 1
            self.stats.last_finished_at = time.time()
            self._d_event(idx, "completed", gid)
            log.info("✅ %s DONE — cover=%s pdf=%s (total completed: %d)",
                     gid, cover_msg_id, pdf_msg_id, self.stats.completed)
            return True
        finally:
            self.stats.in_flight.pop(gid, None)

    async def _fetch_cover_bytes(self, m: Dict[str, Any]) -> tuple[bytes, str]:
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

    async def run(self) -> None:
        await self.start()
        tasks = [asyncio.create_task(self._producer())]
        for i, c in enumerate(self.clients, 1):
            tasks.append(asyncio.create_task(self._slot_loop(i, c)))
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
