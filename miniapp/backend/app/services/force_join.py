"""
force_join.py — Force-subscribe gate.

Admin configures a list of channels that users MUST be members of before
any DM delivery. On every delivery attempt we call Bot API `getChatMember`
for each configured channel; if the user isn't a member of one or more, we
send them a "Please join" prompt with inline Join buttons plus an
"✅ I've joined — deliver my file" callback button, and refuse delivery.

The pending gallery is remembered in `miniapp_pending_deliveries`. When
the user taps "I've joined", the admin bot (see admin_bot.py's `fj:`*
callback) re-checks membership and, on success, triggers delivery of the
remembered gallery.

Setting `force_join_channels` → list[dict]:
    [{"username": "my_channel", "title": "My Channel",
      "url": null, "chat_id": null}, ...]
  - `username` has no leading '@' (normalised on write).
  - `url` overrides the default `https://t.me/<username>` join link.
  - `chat_id` is cached on first successful getChat lookup so private
    (-100…) channels also work.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from .. import db as _db
from ..config import settings

log = logging.getLogger("miniapp.force_join")

_TG_API = "https://api.telegram.org"
_HTTP_TIMEOUT = 10.0
# getChatMember statuses we count as "is a member":
_MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


def _bot_token() -> str:
    return (
        settings.bot_token
        or os.environ.get("BOT_TOKEN", "")
        or os.environ.get("ADMIN_BOT_TOKEN", "")
    )


def _pending_col():
    return _db.db()["miniapp_pending_deliveries"]


# ---------------------------------------------------------------------------
# Settings I/O
# ---------------------------------------------------------------------------
def get_channels() -> List[Dict[str, Any]]:
    v = _db.get_setting("force_join_channels", []) or []
    if not isinstance(v, list):
        return []
    return [c for c in v if isinstance(c, dict)
            and (c.get("username") or c.get("chat_id"))]


def set_channels(items: List[Dict[str, Any]]) -> None:
    _db.set_setting("force_join_channels", items or [])


def is_enabled() -> bool:
    """Force-join is active iff at least one channel is configured."""
    return len(get_channels()) > 0


def _normalise_handle(raw: str) -> str:
    s = str(raw or "").strip()
    if s.startswith("@"):
        s = s[1:]
    return s


def add_channel(username_or_id: str, title: str = "", url: str = "") -> Dict[str, Any]:
    handle = _normalise_handle(username_or_id)
    if not handle:
        raise ValueError("empty channel handle")

    numeric_id: Optional[int] = None
    if handle.lstrip("-").isdigit():
        try:
            numeric_id = int(handle)
        except ValueError:
            numeric_id = None

    current = get_channels()
    for c in current:
        if numeric_id is not None and int(c.get("chat_id") or 0) == numeric_id:
            return {"ok": True, "already": True, "channels": current}
        if (c.get("username") or "").lower() == handle.lower():
            return {"ok": True, "already": True, "channels": current}

    new_row: Dict[str, Any] = {
        "username": handle if numeric_id is None else "",
        "chat_id":  numeric_id,
        "title":    (title or "").strip()
                    or (handle if numeric_id is None else f"#{numeric_id}"),
        "url":      (url or "").strip() or None,
        "added_at": time.time(),
    }
    current.append(new_row)
    set_channels(current)
    return {"ok": True, "already": False, "channels": current}


def remove_channel(username_or_id: str) -> Dict[str, Any]:
    handle = _normalise_handle(username_or_id)
    numeric = None
    if handle.lstrip("-").isdigit():
        try:
            numeric = int(handle)
        except ValueError:
            numeric = None
    kept, removed = [], False
    for c in get_channels():
        if numeric is not None and int(c.get("chat_id") or 0) == numeric:
            removed = True
            continue
        if (c.get("username") or "").lower() == handle.lower():
            removed = True
            continue
        kept.append(c)
    set_channels(kept)
    return {"ok": True, "removed": removed, "channels": kept}


def join_url(channel: Dict[str, Any]) -> str:
    if channel.get("url"):
        return str(channel["url"])
    u = channel.get("username")
    if u:
        return f"https://t.me/{u}"
    cid = channel.get("chat_id")
    if cid:
        s = str(abs(int(cid)))
        if s.startswith("100"):
            s = s[3:]
        return f"https://t.me/c/{s}"
    return "https://t.me/"


# ---------------------------------------------------------------------------
# Bot API glue
# ---------------------------------------------------------------------------
def _sync_call(method: str, payload: dict) -> dict:
    token = _bot_token()
    if not token:
        return {"ok": False, "description": "no bot token"}
    try:
        r = httpx.post(
            f"{_TG_API}/bot{token}/{method}",
            json=payload, timeout=_HTTP_TIMEOUT,
        )
        return r.json() or {"ok": False, "description": f"HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": f"http error: {e!s}"}


def _resolve_chat_id(channel: Dict[str, Any]) -> Optional[int]:
    """Return the numeric chat_id for a configured channel (cached after
    the first successful lookup)."""
    cid = channel.get("chat_id")
    if cid:
        try:
            return int(cid)
        except (TypeError, ValueError):
            pass

    handle = channel.get("username") or ""
    if not handle:
        return None
    r = _sync_call("getChat", {"chat_id": f"@{handle}"})
    if not r.get("ok"):
        log.warning("force_join: getChat @%s failed: %s",
                    handle, r.get("description"))
        return None
    new_cid = int((r.get("result") or {}).get("id") or 0) or None
    if new_cid:
        # Cache back to settings so we don't call getChat every delivery.
        current = get_channels()
        for c in current:
            if (c.get("username") or "").lower() == handle.lower():
                c["chat_id"] = new_cid
        set_channels(current)
    return new_cid


def _is_member(user_id: int, chat_id: int) -> bool:
    r = _sync_call("getChatMember",
                   {"chat_id": int(chat_id), "user_id": int(user_id)})
    if not r.get("ok"):
        desc = str(r.get("description") or "").lower()
        if "not found" in desc:
            return False
        # Bot not admin / chat not found → can't verify. Safe default:
        # let the user through so a misconfigured channel never locks
        # everyone out.
        log.warning("force_join: getChatMember failed uid=%s chat=%s: %s",
                    user_id, chat_id, desc)
        return True
    status = (r.get("result") or {}).get("status", "")
    return status in _MEMBER_STATUSES


def check_membership(user_id: int) -> Dict[str, Any]:
    """Returns {"missing": [...], "enabled": bool}. `missing` is the list of
    configured channels the user hasn't joined (empty if all good, or if
    force-join is disabled)."""
    channels = get_channels()
    if not channels:
        return {"missing": [], "enabled": False}

    missing: List[Dict[str, Any]] = []
    for c in channels:
        cid = _resolve_chat_id(c)
        if not cid:
            continue
        if not _is_member(user_id, cid):
            missing.append(c)
    return {"missing": missing, "enabled": True}


def build_join_keyboard(missing_channels: List[Dict[str, Any]],
                        gallery_id: Optional[str] = None) -> Dict[str, Any]:
    rows: List[List[Dict[str, Any]]] = []
    for c in missing_channels[:5]:
        label = c.get("title") or c.get("username") or "channel"
        rows.append([{"text": f"🔗 Join {label}", "url": join_url(c)}])
    rows.append([{
        "text": "✅ I've joined — deliver my file",
        "callback_data": f"fj:check:{gallery_id or ''}",
    }])
    return {"inline_keyboard": rows}


def send_join_prompt(user_id: int, missing: List[Dict[str, Any]],
                     gallery_id: Optional[str] = None) -> dict:
    text = (
        "🔒 Please join the required channel"
        + ("s" if len(missing) > 1 else "")
        + " below to receive your file.\n\n"
        + "After joining, tap ✅ I've joined."
    )
    return _sync_call("sendMessage", {
        "chat_id": int(user_id),
        "text": text,
        "reply_markup": build_join_keyboard(missing, gallery_id),
    })


# ---------------------------------------------------------------------------
# Pending-delivery memory (used by the admin bot's 'I've joined' callback)
# ---------------------------------------------------------------------------
def remember_pending(user_id: int, gallery_id: str) -> None:
    try:
        _pending_col().update_one(
            {"_id": f"{int(user_id)}:{str(gallery_id)}"},
            {"$set": {
                "user_id":    int(user_id),
                "gallery_id": str(gallery_id),
                "created_at": time.time(),
            }},
            upsert=True,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("force_join.remember_pending failed: %s", e)


def pop_pending(user_id: int, gallery_id: str) -> bool:
    try:
        r = _pending_col().delete_one(
            {"_id": f"{int(user_id)}:{str(gallery_id)}"},
        )
        return bool(r.deleted_count)
    except Exception as e:  # noqa: BLE001
        log.warning("force_join.pop_pending failed: %s", e)
        return False


def list_pending_for(user_id: int) -> List[str]:
    try:
        rows = _pending_col().find({"user_id": int(user_id)}).limit(20)
        return [str(r.get("gallery_id")) for r in rows if r.get("gallery_id")]
    except Exception:
        return []
