"""
userbot_pool.py — v12.33 multi-userbot pool (2 Telethon clients, 1 process).

Rationale (see GUIDE.md §"v12.33 — multi-userbot pool"):

- v12.32 ran ONE Telethon client. A single @Gallery_DLBot fetch stalled the
  whole queue for its full duration (up to ~7 min for a 200-page doujinshi).
- v12.33 runs N clients inside the SAME worker.py asyncio loop. Fetches
  from @Gallery_DLBot run truly in parallel (independent DM histories).
- The DB channel writes (cover + forwarded PDF) are serialised behind ONE
  process-global asyncio.Lock so the channel always reads
  `cover_A, pdf_A, cover_B, pdf_B` — never interleaved.

Public API
----------
    pool = UserbotPool.from_env()        # builds N clients from env vars
    await pool.start()                    # connects all clients
    await pool.stop()                     # disconnects all clients

    async with pool.acquire() as slot:    # least-in-flight dispatch
        # slot.client is a telethon.TelegramClient
        # slot.index  is the 1-based slot number (for logs / alerts)
        ...

    async with pool.channel_write():      # serialises DB-channel writes
        await cover_poster.post_cover(...)
        await client.forward_messages(...)

    await pool.mark_flood(slot, seconds)  # cool the slot + admin alert

Design notes
------------
1. `from_env()` reads BOT 0 env vars using the v12.33 contract:
       Slot 1 (existing, unchanged): API_ID, API_HASH, STRING_SESSION
       Slot 2 (new):                 STRING_SESSION_2
                                     (same API_ID / API_HASH; both userbots
                                      belong to the same Telegram dev app.)
   Any slot whose STRING_SESSION is missing is skipped silently — so a
   solo-userbot deploy still works if slot 2's session is ever unset.
2. `acquire()` picks the slot with the fewest active fetches over the set
   of non-cooling slots. Ties broken by slot index (stable, cheap).
3. `channel_write()` is a flat process-global lock — one DB channel, one
   lock (per user decision in the v12.33 briefing). Held ONLY around the
   cover + PDF forward pair. The Bot 2 send + wait-for-PDF section runs
   UNLOCKED so parallelism actually materialises.
4. Admin alert on FloodWait is fired via the Bot API using the admin bot
   token, same transport worker.py already uses for `_notify_admin`. We
   do NOT import worker.py here to avoid a circular dep.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import List, Optional

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import settings

log = logging.getLogger("userbot_pool")

_ADMIN_ALERT_TIMEOUT = 10.0  # seconds; best-effort HTTP


class NoUserbotAvailable(RuntimeError):
    """Raised by acquire() when every slot is currently cooling.

    The caller (worker.py) should re-enqueue the job with a short delay
    (typically min(cooling_until) - now) and try again.
    """


@dataclass
class _Slot:
    index: int                      # 1-based, for logs / alerts
    client: TelegramClient
    in_flight: int = 0              # concurrent jobs on this client
    cooling_until: float = 0.0      # monotonic time; 0 = healthy
    total_fetches: int = 0          # lifetime, for /checkram-style ops later
    total_floods: int = 0

    @property
    def client_id(self) -> int:
        """Cheap stable per-slot key for external state (e.g. bot2_client's
        per-client `_last_sent_msg_id` dict)."""
        return id(self.client)

    def is_cooling(self, now: Optional[float] = None) -> bool:
        return self.cooling_until > (now if now is not None else time.monotonic())


@dataclass
class UserbotPool:
    """N-Telethon-client pool bound to a single asyncio event loop.

    Do NOT construct across event loops; the `_channel_lock` is bound to
    whichever loop first awaits `channel_write()`.
    """
    slots: List[_Slot]
    _channel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _started: bool = False

    # ----------------------------- Construction ------------------------------

    @classmethod
    def from_env(cls) -> "UserbotPool":
        """Build the pool from BOT 0's env vars per the v12.33 contract.

        - Slot 1: existing API_ID / API_HASH / STRING_SESSION (unchanged).
        - Slot 2: STRING_SESSION_2 (same API_ID / API_HASH).
        - Slot 3+: STRING_SESSION_3, _4, ... same rule (future-proof).

        Any slot with a missing/blank STRING_SESSION is silently skipped.
        """
        api_id = int(settings.api_id)
        api_hash = str(settings.api_hash)

        sessions: List[str] = []
        # Slot 1 — unchanged legacy name so v12.32 env keeps working.
        if settings.session_string:
            sessions.append(settings.session_string)
        # Slot 2..N — indexed suffix.
        idx = 2
        while True:
            key = f"STRING_SESSION_{idx}"
            val = (os.getenv(key) or "").strip()
            if not val:
                break
            sessions.append(val)
            idx += 1

        if not sessions:
            raise RuntimeError(
                "UserbotPool.from_env: no STRING_SESSION found. Set at "
                "least STRING_SESSION (slot 1)."
            )

        slots: List[_Slot] = []
        for i, s in enumerate(sessions, start=1):
            client = TelegramClient(
                StringSession(s),
                api_id,
                api_hash,
                device_model=f"DoujinshiUniverse pool slot {i}",
                system_version="v12.33",
                app_version="v12.33",
            )
            slots.append(_Slot(index=i, client=client))
        log.info("UserbotPool.from_env: built %d slot(s)", len(slots))
        return cls(slots=slots)

    # ----------------------------- Lifecycle ---------------------------------

    async def start(self) -> None:
        """Connect all clients. Idempotent."""
        if self._started:
            return
        for slot in self.slots:
            log.info("pool: starting slot %d", slot.index)
            await slot.client.connect()
            if not await slot.client.is_user_authorized():
                raise RuntimeError(
                    f"UserbotPool slot {slot.index}: session is NOT "
                    f"authorised. Regenerate STRING_SESSION"
                    f"{'' if slot.index == 1 else f'_{slot.index}'}."
                )
        self._started = True
        log.info("pool: all %d slot(s) connected & authorised", len(self.slots))

    async def stop(self) -> None:
        """Disconnect all clients. Idempotent, never raises."""
        for slot in self.slots:
            try:
                await slot.client.disconnect()
            except Exception as e:  # noqa: BLE001
                log.warning("pool: slot %d disconnect failed: %s",
                            slot.index, e)
        self._started = False

    # ----------------------------- Dispatch ----------------------------------

    def _pick(self) -> Optional[_Slot]:
        """Least-in-flight over non-cooling slots. Ties broken by slot index."""
        now = time.monotonic()
        healthy = [s for s in self.slots if not s.is_cooling(now)]
        if not healthy:
            return None
        return min(healthy, key=lambda s: (s.in_flight, s.index))

    @asynccontextmanager
    async def acquire(self):
        """Reserve a healthy slot for one job.

        Yields the `_Slot` (so callers can log slot.index and read
        slot.client). Increments `in_flight` on entry, decrements on exit
        even if the body raises.

        Raises `NoUserbotAvailable` when every slot is cooling.
        """
        slot = self._pick()
        if slot is None:
            now = time.monotonic()
            wait = min(s.cooling_until - now for s in self.slots)
            log.warning("pool: no healthy slots (all cooling ~%.0fs)", wait)
            raise NoUserbotAvailable(f"all slots cooling for {wait:.0f}s")
        slot.in_flight += 1
        slot.total_fetches += 1
        try:
            yield slot
        finally:
            slot.in_flight = max(0, slot.in_flight - 1)

    def has_healthy_slot(self) -> bool:
        """True if at least one slot is not currently cooling.

        Used by worker.py's dispatcher as a cheap pre-pull gate so we
        don't mark a job 'processing' and then immediately have to
        re-queue it because every slot is cooling.
        """
        now = time.monotonic()
        return any(not s.is_cooling(now) for s in self.slots)

    # ---------------------- Serialised DB-channel writes ---------------------

    @asynccontextmanager
    async def channel_write(self):
        """Serialise DB-channel writes across the whole pool.

        Wrap ONLY the (post_cover, forward_pdf) pair in this. Fetches
        against @Gallery_DLBot MUST run outside this lock or v12.33
        gains no throughput over v12.32.
        """
        async with self._channel_lock:
            yield

    # ----------------------------- FloodWait ---------------------------------

    async def mark_flood(self, slot: _Slot, seconds: int,
                         context: str = "") -> None:
        """Mark `slot` cooling for `seconds`, fire admin alert.

        Best-effort — never raises. Idempotent for overlapping FloodWaits
        (later, larger cool-down wins).
        """
        secs = max(1, int(seconds))
        new_until = time.monotonic() + secs
        if new_until > slot.cooling_until:
            slot.cooling_until = new_until
        slot.total_floods += 1
        log.warning("pool: slot %d cooling for %ds (context=%s)",
                    slot.index, secs, context or "-")
        await self._admin_alert(
            f"⚠️ Userbot slot {slot.index} cooling for {secs}s — "
            f"FloodWait from @Gallery_DLBot"
            + (f" (context: {context})" if context else "")
        )

    async def _admin_alert(self, text: str) -> None:
        """Bot-API sendMessage to ADMIN_USER_ID. Never raises."""
        token = getattr(settings, "admin_bot_token", "") or ""
        admin = int(getattr(settings, "admin_user_id", 0) or 0)
        if not token or not admin:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=_ADMIN_ALERT_TIMEOUT) as c:
                await c.post(url, json={"chat_id": admin, "text": text})
        except Exception as e:  # noqa: BLE001
            log.info("pool: admin alert failed (non-fatal): %s", e)

    # ----------------------------- Diagnostics -------------------------------

    def snapshot(self) -> list[dict]:
        """Cheap read-only view for /checkram-style ops. No secrets."""
        now = time.monotonic()
        return [
            {
                "index": s.index,
                "in_flight": s.in_flight,
                "cooling_for": max(0.0, s.cooling_until - now),
                "total_fetches": s.total_fetches,
                "total_floods": s.total_floods,
            }
            for s in self.slots
        ]


# Convenience: the process-global pool. worker.py's _run_loop assigns to
# this after `await pool.start()`; other modules that need to peek at slot
# state (e.g. admin_bot's /checkram) import from here.
POOL: Optional[UserbotPool] = None


def set_global(pool: UserbotPool) -> None:
    """Register the pool as the process-global singleton."""
    global POOL
    POOL = pool


def get_global() -> Optional[UserbotPool]:
    return POOL
