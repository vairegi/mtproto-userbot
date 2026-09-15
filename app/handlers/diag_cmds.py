"""Diagnostics: /debug /stats. /debug reads mostly from in-memory cache."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..config import settings
from ..services import posting, repo, scheduler as sched, userbot as ub
from ..utils import esc
from .setup_cmds import _reject_non_admin
from ..services import fsub as _fsub


def _ram_lines() -> list:
    """v4.2: memory lines for /debug (process RSS + Render cgroup + host)."""
    import os
    out = []
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS"):
                    out.append(f"bot process RSS: <b>{int(ln.split()[1]) / 1024:.1f} MB</b>")
                    break
    except Exception:
        pass
    try:
        usage = limit = None
        if os.path.exists("/sys/fs/cgroup/memory.current"):
            usage = int(open("/sys/fs/cgroup/memory.current").read().strip())
            raw = open("/sys/fs/cgroup/memory.max").read().strip()
            limit = None if raw == "max" else int(raw)
        elif os.path.exists("/sys/fs/cgroup/memory/memory.usage_in_bytes"):
            usage = int(open("/sys/fs/cgroup/memory/memory.usage_in_bytes").read().strip())
            limit = int(open("/sys/fs/cgroup/memory/memory.limit_in_bytes").read().strip())
            if limit > (1 << 60):
                limit = None
        if usage is not None:
            if limit:
                out.append(f"container RAM: <b>{usage / 1048576:.1f} / {limit / 1048576:.0f} MB</b> ({usage * 100 // limit}%)")
            else:
                out.append(f"container RAM used: <b>{usage / 1048576:.1f} MB</b> (no cgroup limit visible)")
    except Exception:
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k = ln.split(":")[0]
                if k in ("MemTotal", "MemAvailable"):
                    info[k] = int(ln.split()[1])
        if info.get("MemTotal"):
            used = info["MemTotal"] - info.get("MemAvailable", 0)
            out.append(f"host RAM: <b>{used / 1024:.0f} / {info['MemTotal'] / 1024:.0f} MB</b> used ({used * 100 // info['MemTotal']}%)")
    except Exception:
        pass
    return out or ["ram: unavailable on this host"]
from ..services import fsub as _fsub
def _ram_lines() -> list:
    """v4.2: memory usage lines for /debug.
    Process RSS (/proc/self/status) + the Render container's cgroup usage /
    limit (v2 then v1) + host /proc/meminfo as a last-resort fallback.
    Pure /proc|/sys reads — zero DB access, safe on the health path."""
    import os
    out = []
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS"):
                    kb = int(ln.split()[1])
                    out.append(f"bot process RSS: <b>{kb / 1024:.1f} MB</b>")
                    break
    except Exception:
        pass
    try:
        usage = limit = None
        if os.path.exists("/sys/fs/cgroup/memory.current"):  # cgroup v2
            usage = int(open("/sys/fs/cgroup/memory.current").read().strip())
            raw = open("/sys/fs/cgroup/memory.max").read().strip()
            limit = None if raw == "max" else int(raw)
        elif os.path.exists("/sys/fs/cgroup/memory/memory.usage_in_bytes"):  # v1
            usage = int(open("/sys/fs/cgroup/memory/memory.usage_in_bytes").read().strip())
            limit = int(open("/sys/fs/cgroup/memory/memory.limit_in_bytes").read().strip())
            if limit > (1 << 60):  # 'no limit' sentinel on some hosts
                limit = None
        if usage is not None:
            if limit:
                out.append(
                    f"container RAM: <b>{usage / 1048576:.1f} / "
                    f"{limit / 1048576:.0f} MB</b> ({usage * 100 // limit}%)")
            else:
                out.append(f"container RAM used: <b>{usage / 1048576:.1f} MB</b> "
                           f"(no cgroup limit visible)")
    except Exception:
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k = ln.split(":")[0]
                if k in ("MemTotal", "MemAvailable"):
                    info[k] = int(ln.split()[1])
        if info.get("MemTotal"):
            used = info["MemTotal"] - info.get("MemAvailable", 0)
            out.append(f"host RAM: <b>{used / 1024:.0f} / "
                       f"{info['MemTotal'] / 1024:.0f} MB</b> used "
                       f"({used * 100 // info['MemTotal']}%)")
    except Exception:
        pass
    return out or ["ram: unavailable on this host"]

log = logging.getLogger("diag_cmds")
router = Router(name="diag_cmds")


@router.message(Command("debug"))
async def cmd_debug(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    s = ub.backfill_state()
    ms = ub.mass_delete_state()
    cfg = await sched.get_schedule()
    last_err = posting.LAST_PUBLISH_ERROR or "-"
    lines = [
        "<b>🔧 Debug</b>",
        f"db backend: <b>{settings.db_backend}</b>",
        f"spoiler: <b>{'ON' if (await repo.get_setting_bool('spoiler', True)) else 'OFF'}</b>",
        f"protect: <b>{'ON' if (await repo.get_setting_bool('protect_content')) else 'OFF'}</b>",
        f"paused: <b>{'YES' if (await posting._paused()) else 'no'}</b>",
        f"schedule: <code>{cfg or 'off'}</code>",
        f"postcaption: <code>{esc((await repo.get_setting('postcaption_extra')) or '-')}</code>",
        f"filecaption: <code>{esc((await repo.get_setting('filecaption_extra')) or '-')}</code>",
        f"backfill running: {s.running} | current mid: {s.current_mid} / head {s.head_mid}",
        f"backfill counts: 🖼{s.covers_ingested} 📄{s.files_ingested} 🎨{s.stickers_ingested}",
        f"massdlt running: {ms.running} | deleted: {ms.deleted}",
        f"last publish error: <code>{esc(last_err[:160])}</code>",
        f"cache entries: {repo.cache_stats()['entries']}",
        f"fsub member cache (15m): {_fsub.cache_size()} entries",
        "<b>🧠 Memory (Render)</b>",
        *_ram_lines(),
        f"fsub member cache (15m): {_fsub.cache_size()} entries",
        f"telethon available: {ub.telethon_available()}",
        "<b>🧠 Memory (Render)</b>",
        *_ram_lines(),
    ]
    await msg.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    covers = await repo.total_cover_count()
    files = await repo.total_file_count()
    published = await repo.published_cover_count()
    pending = await repo.queued_cover_count()
    utotal = await repo.users_total()
    uactive = await repo.users_active_today()
    unew = await repo.users_new_today()
    uweek = await repo.users_active_week()
    umonth = await repo.users_active_month()
    ftoday = await repo.fetches_today()
    ftotal = await repo.fetches_total()
    await msg.reply(
        f"📊 <b>Stats</b>\n"
        f"🖼 Covers: {covers}\n"
        f"📄 Files: {files}\n"
        f"✅ Published: {published}\n"
        f"⏳ Pending: {pending}\n"
        f"\n"
        f"👥 <b>Users</b>\n"
        f"• Total: {utotal}\n"
        f"• Active today: {uactive}  ·  this week: {uweek}  ·  this month: {umonth}\n"
        f"• New today: {unew}\n"
        f"\n"
        f"📥 <b>File fetches</b>\n"
        f"• Today: {ftoday}\n"
        f"• All time: {ftotal}",
        parse_mode="HTML",
    )
