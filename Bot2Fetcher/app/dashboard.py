"""
dashboard.py — optional live status message in a Telegram log channel.

Posts ONE message via userbot slot 1 and edits it every 60 s. The message
id is persisted in Turso bot2_fetch_state['_dashboard'] so restarts keep
editing the same message instead of spamming new ones.

Disabled entirely when LOG_CHANNEL_ID is unset.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("bot2fetcher.dashboard")


def _fmt(stats: dict) -> str:
    up = stats["uptime_s"]
    h, rem = divmod(up, 3600)
    m, _ = divmod(rem, 60)
    return (
        "🤖 Bot2Fetcher — live\n"
        f"⏱ uptime: {h}h{m:02d}m   🔄 cycles: {stats['cycles']}\n"
        f"📚 scanned: {stats['scanned']}\n"
        f"✅ completed: {stats['completed']}   ❌ failed: {stats['failed']}\n"
        f"⏭ already done: {stats['skipped_done']}   "
        f"🚫 failed-before: {stats['skipped_failed']}   "
        f"🔒 busy: {stats['skipped_busy']}\n"
        f"📥 in flight: {len(stats['in_flight'])}"
        + (f"  ({', '.join(stats['in_flight'].keys())})" if stats["in_flight"] else "")
        + f"\n🕒 updated: {time.strftime('%H:%M:%S UTC', time.gmtime())}"
    )


class Dashboard:
    def __init__(self, settings, turso, stats):
        self.s = settings
        self.turso = turso
        self.stats = stats
        self.msg_id = 0
        self.channel = None

    async def run(self, fetcher) -> None:
        if not self.s.log_channel_id:
            log.info("dashboard disabled (LOG_CHANNEL_ID unset)")
            return
        client = fetcher.clients[0]
        raw = self.s.log_channel_id
        self.channel = await client.get_entity(int(raw) if raw.lstrip("-").isdigit() else raw)
        saved = await self.turso.get_state("_dashboard")
        if saved and saved.get("msg_id"):
            self.msg_id = int(saved["msg_id"])
        while not fetcher._stop.is_set():
            text = _fmt(self.stats.snapshot())
            try:
                if self.msg_id:
                    await client.edit_message(self.channel, self.msg_id, text)
                else:
                    msg = await client.send_message(self.channel, text)
                    self.msg_id = int(msg.id)
                    await self.turso.put_state("_dashboard", {"msg_id": self.msg_id})
            except Exception as e:
                log.warning("dashboard update failed: %s", e)
                self.msg_id = 0  # repost next tick
            await asyncio.sleep(60)
