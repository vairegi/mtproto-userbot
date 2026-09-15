"""Force-subscribe gate: users must join configured channels before Get File.

  /fsub <chat_id> <invite_link>   -> add a required channel
  /fsublist                       -> list with title-as-link
  /fsubremove <chat_id>           -> remove
  /fsub_sync <chat_id>            -> import pending join-requests (userbot)

Storage: settings['fsub_channels'] = [{"chat_id": -100…, "link": "https://t.me/+…",
                                       "title": "…"}]
Membership check: bot.get_chat_member(channel, user) — bot must be admin in
the fsub channel. 'member'/'administrator'/'creator' pass; 'left'/'kicked'
fail; API errors FAIL OPEN with a log warning so a misconfigured channel
never blocks all users.

v3.3.1 (private approval-required channels): a user who REQUESTED to join is
not a member yet — get_chat_member raises 'user not found' or returns 'left'.
Join requests are recorded (chat_join_request events / /fsub_sync) in the
fsub_requests store; a recorded pending request PASSES the gate.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from . import repo

# v4.2: 15-minute membership cache. Only PASSING results are cached — a
# failed check is never cached, so a user who taps Join → 🔄 Retry still
# passes instantly (their previous failure left no cache entry).
_CACHE_TTL = 900  # seconds (15 min)
_member_cache: dict = {}  # (chat_id, user_id) -> monotonic expiry


def cache_size() -> int:
    """Entries currently in the membership cache (shown in /debug)."""
    return len(_member_cache)

log = logging.getLogger("fsub")

_CACHE_TTL = 900  # v4.2: 15-minute membership cache (passes only)
_member_cache: dict = {}


def cache_size() -> int:
    return len(_member_cache)

_PASS_STATUSES = {"member", "administrator", "creator", "owner", "restricted"}
# Telegram phrases that mean "definitively not a member" — for these (and for
# status 'left') a recorded pending join request satisfies the gate.
_NONMEMBER_ERR_KEYS = ("not found", "user_not_found", "user not particip",
                       "not a member", "not a participant",
                       "participant_id_invalid")


async def list_fsub() -> List[dict]:
    return (await repo.get_setting_json("fsub_channels", [])) or []


async def add_fsub(chat_id: int, invite_link: str, title: Optional[str] = None) -> None:
    rows = await list_fsub()
    rows = [r for r in rows if int(r.get("chat_id") or 0) != int(chat_id)]
    rows.append({"chat_id": int(chat_id), "link": invite_link, "title": title or ""})
    await repo.set_setting_json("fsub_channels", rows)


async def remove_fsub(chat_id: int) -> bool:
    rows = await list_fsub()
    new = [r for r in rows if int(r.get("chat_id") or 0) != int(chat_id)]
    if len(new) == len(rows):
        return False
    await repo.set_setting_json("fsub_channels", new)
    return True


async def set_title(chat_id: int, title: str) -> None:
    rows = await list_fsub()
    for r in rows:
        if int(r.get("chat_id") or 0) == int(chat_id):
            r["title"] = title
    await repo.set_setting_json("fsub_channels", rows)


async def _passes_via_request(chat_id: int, user_id: int) -> bool:
    try:
        return await repo.has_fsub_request(int(chat_id), int(user_id))
    except Exception:
        return False


async def unjoined_channels(bot, user_id: int) -> List[dict]:
    """Return the subset of fsub channels the user has NOT joined (and has no
    recorded pending join request for)."""
    missing = []
    for ch in await list_fsub():
        cid = int(ch.get("chat_id") or 0)
        key = (cid, int(user_id))
        # v4.2: verified member within the last 15 min → skip the API call.
        exp = _member_cache.get(key)
        if exp and exp > time.monotonic():
            continue
        try:
            m = await bot.get_chat_member(chat_id=cid, user_id=int(user_id))
            status = getattr(m, "status", "") or ""
            status = getattr(status, "value", status)  # enum -> str
            if str(status) in _PASS_STATUSES:
                _member_cache[key] = time.monotonic() + _CACHE_TTL
                continue
            if str(status) == "left" and await _passes_via_request(cid, user_id):
                # NOT cached: a rejected request must start failing next tap.
                continue
            missing.append(ch)
        except Exception as e:
            err = str(e).lower()
            if any(k in err for k in _NONMEMBER_ERR_KEYS):
                # Definitive non-member (how PRIVATE channels answer for
                # non-members/requesters) — a recorded request still passes.
                if await _passes_via_request(cid, user_id):
                    # NOT cached — same reason as the 'left' branch above.
                    continue
                missing.append(ch)
                continue
            log.warning("fsub check failed chat=%s user=%s: %s (failing open)",
                        cid, user_id, e)
    return missing


async def check_or_gate(bot, user_id: int, code: str) -> bool:
    """True = user may proceed. False = gate message sent (with Retry button)."""
    missing = await unjoined_channels(bot, user_id)
    if not missing:
        return True
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from ..utils import esc
    rows = []
    for ch in missing:
        link = ch.get("link") or "https://t.me/"
        is_request = ("/+" in link) or ("joinchat" in link)
        base = ch.get("title") or "Channel"
        label = (f"📤 Request: {base}" if is_request else f"📢 Join: {base}")
        rows.append([InlineKeyboardButton(text=label, url=link)])
    rows.append([InlineKeyboardButton(text="🔄 Retry", callback_data=f"fsub_retry:{code}")])
    await bot.send_message(
        chat_id=user_id,
        text=("🔒 <b>Join required</b>\n\n"
              "To receive the files, please join the channel(s) below, "
              "then tap <b>🔄 Retry</b>.\n\n"
              "📤 For channels marked <b>Request</b>, send a join request — "
              "as soon as it arrives, 🔄 Retry lets you through "
              "(no need to wait for approval)."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    return False
