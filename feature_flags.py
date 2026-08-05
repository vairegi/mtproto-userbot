"""
feature_flags.py — Bot-side readers for the three Mini-App admin features.

The Mini App backend (miniapp/backend/app/db.py) writes these toggles into
the `miniapp_settings` collection (doc `_id: "singleton"`). The worker
(relay_v2 auto-DM) and the admin bot (force-join "I've joined" callback)
run in DIFFERENT processes, so they can't import the mini-app package —
this module is the shared reader both sides use.

Settings keys:
  auto_delete_enabled   bool
  auto_delete_hours     int   (default 24)
  share_disabled        bool
  force_join_channels   list[dict]
      [{username, title, url, chat_id, invite_hash}, ...]

Plus Bot API helpers for the force-join gate (getChat / getChatMember /
sendMessage with inline keyboard) used by relay_v2's auto-DM path.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("feature_flags")

_TG_API = "https://api.telegram.org"
_HTTP_TIMEOUT = 10.0
_MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


# ---------------------------------------------------------------------------
# Settings reads (via the shared bot db.py handle)
# ---------------------------------------------------------------------------
def _settings_doc(conn) -> Dict[str, Any]:
    try:
        return conn.db["miniapp_settings"].find_one({"_id": "singleton"}) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("feature_flags: miniapp_settings read failed: %s", e)
        return {}


def auto_delete_enabled(conn) -> bool:
    return bool(_settings_doc(conn).get("auto_delete_enabled", False))


def auto_delete_hours(conn) -> int:
    try:
        h = int(_settings_doc(conn).get("auto_delete_hours", 24) or 24)
    except (TypeError, ValueError):
        h = 24
    return max(1, h)


def share_disabled(conn) -> bool:
    return bool(_settings_doc(conn).get("share_disabled", False))


def force_join_channels(conn) -> List[Dict[str, Any]]:
    v = _settings_doc(conn).get("force_join_channels", []) or []
    if not isinstance(v, list):
        return []
    return [c for c in v if isinstance(c, dict)
            and (c.get("username") or c.get("chat_id") or c.get("invite_hash"))]


def force_join_enabled(conn) -> bool:
    return len(force_join_channels(conn)) > 0


# ---------------------------------------------------------------------------
# Auto-delete scheduling (insert rows the mini-app's deletion loop consumes)
# ---------------------------------------------------------------------------
def schedule_deletes(conn, chat_id: int, message_ids) -> int:
    if not auto_delete_enabled(conn):
        return 0
    ids = [int(m) for m in (message_ids or []) if m]
    if not ids:
        return 0
    delete_at = time.time() + (auto_delete_hours(conn) * 3600.0)
    docs = [{"chat_id": int(chat_id), "message_id": mid,
             "created_at": time.time(), "delete_at": delete_at}
            for mid in ids]
    try:
        conn.db["miniapp_scheduled_deletes"].insert_many(docs, ordered=False)
        return len(docs)
    except Exception as e:  # noqa: BLE001
        log.warning("schedule_deletes insert failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Force-join gate (Bot API)
# ---------------------------------------------------------------------------
def _admin_token() -> str:
    from config import settings  # local import to avoid cycles
    return (getattr(settings, "admin_bot_token", "") or ""
            or os.environ.get("BOT_TOKEN", "")
            or os.environ.get("ADMIN_BOT_TOKEN", ""))


async def _api_call_async(method: str, payload: dict) -> dict:
    import httpx  # local import: this module is also imported in sync ctx
    token = _admin_token()
    if not token:
        return {"ok": False, "description": "no admin bot token"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
            r = await c.post(f"{_TG_API}/bot{token}/{method}", json=payload)
        data = r.json() or {}
        return data
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": f"http error: {e!s}"}


async def _fetch_title(channel: Dict[str, Any]) -> str:
    """Best-effort async Bot API getChat → result.title. Works when
    `username` or `chat_id` is known; empty string for invite-only rows
    the bot can't resolve yet."""
    chat_ref: Any = None
    if channel.get("chat_id"):
        try:
            chat_ref = int(channel["chat_id"])
        except (TypeError, ValueError):
            chat_ref = None
    if chat_ref is None and channel.get("username"):
        chat_ref = f"@{channel['username']}"
    if chat_ref is None:
        return ""
    r = await _api_call_async("getChat", {"chat_id": chat_ref})
    if not r.get("ok"):
        return ""
    return ((r.get("result") or {}).get("title") or "").strip()


async def _resolve_chat_id(conn, channel: Dict[str, Any]) -> Optional[int]:
    cid = channel.get("chat_id")
    if cid:
        try:
            return int(cid)
        except (TypeError, ValueError):
            pass
    handle = channel.get("username") or ""
    if not handle:
        # Invite-hash-only rows can't be resolved from the hash alone.
        # Caller (check_membership) will fail OPEN in that case.
        return None
    r = await _api_call_async("getChat", {"chat_id": f"@{handle}"})
    if not r.get("ok"):
        log.warning("force_join getChat @%s failed: %s",
                    handle, r.get("description"))
        return None
    result = r.get("result") or {}
    new_cid = int(result.get("id") or 0) or None
    new_title = (result.get("title") or "").strip()
    if new_cid:
        # Cache it back so the mini-app side benefits too.
        try:
            doc = conn.db["miniapp_settings"].find_one(
                {"_id": "singleton"}) or {}
            chans = doc.get("force_join_channels", []) or []
            for c in chans:
                if (c.get("username") or "").lower() == handle.lower():
                    c["chat_id"] = new_cid
                    if new_title:
                        c["title"] = new_title
            conn.db["miniapp_settings"].update_one(
                {"_id": "singleton"},
                {"$set": {"force_join_channels": chans}},
                upsert=True,
            )
        except Exception:
            pass
    return new_cid


def _has_pending_join_request(conn, user_id: int, chat_id: int) -> bool:
    """Sync read of the shared `miniapp_pending_join_requests` collection
    (populated by admin_bot.py's ChatJoinRequestHandler)."""
    try:
        row = conn.db["miniapp_pending_join_requests"].find_one(
            {"user_id": int(user_id), "chat_id": int(chat_id)}
        )
    except Exception as e:  # noqa: BLE001
        log.warning("feature_flags: pending-request lookup failed: %s", e)
        return False
    if not row:
        return False
    status = str(row.get("status") or "").lower()
    return status in ("pending", "approved")


async def _is_member(conn, user_id: int, chat_id: int) -> bool:
    r = await _api_call_async(
        "getChatMember", {"chat_id": int(chat_id), "user_id": int(user_id)})
    if not r.get("ok"):
        desc = str(r.get("description") or "").lower()
        if "not found" in desc:
            # User might have a PENDING request-to-join — treat as member.
            if _has_pending_join_request(conn, user_id, chat_id):
                return True
            return False
        # Bot not admin / chat not found → can't verify. Let them through
        # so a misconfigured channel never locks everyone out.
        log.warning("force_join getChatMember failed uid=%s chat=%s: %s",
                    user_id, chat_id, desc)
        return True
    status = (r.get("result") or {}).get("status", "")
    if status in _MEMBER_STATUSES:
        return True
    # Non-member status: fall back to pending-request table.
    if _has_pending_join_request(conn, user_id, chat_id):
        return True
    return False


async def check_membership(conn, user_id: int) -> List[Dict[str, Any]]:
    """Return the list of configured channels the user has NOT joined
    (empty list = all good or force-join disabled). Invite-only channels
    that couldn't be resolved fail OPEN (skipped, not treated as missing)
    so a mid-setup admin doesn't lock everyone out."""
    missing: List[Dict[str, Any]] = []
    for c in force_join_channels(conn):
        cid = await _resolve_chat_id(conn, c)
        if not cid:
            # Unresolvable invite-only row → fail OPEN.
            continue
        if not await _is_member(conn, int(user_id), cid):
            missing.append(c)
    return missing


def join_url(channel: Dict[str, Any]) -> str:
    # Priority: admin-supplied url → invite_hash → @username → chat_id
    if channel.get("url"):
        return str(channel["url"])
    ih = channel.get("invite_hash")
    if ih:
        return f"https://t.me/+{ih}"
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


async def send_join_prompt(user_id: int, missing: List[Dict[str, Any]],
                           gallery_id: str = "") -> dict:
    """DM the user a 'please join' message with Join buttons + the
    '✅ I've joined' callback (handled by admin_bot's fj:check handler)."""
    rows = []
    for c in missing[:5]:
        label = (c.get("title") or "").strip()
        if not label:
            try:
                label = await _fetch_title(c) or ""
            except Exception:
                label = ""
        if not label:
            label = c.get("username") or "channel"
        rows.append([{"text": f"🔗 Join {label}", "url": join_url(c)}])
    rows.append([{
        "text": "✅ I've joined — deliver my file",
        "callback_data": f"fj:check:{gallery_id or ''}",
    }])
    text = ("🔒 Please join the required channel"
            + ("s" if len(missing) > 1 else "")
            + " below to receive your file.\n\n"
            + "After joining (or after requesting to join), tap ✅ I've joined.")
    return await _api_call_async("sendMessage", {
        "chat_id": int(user_id),
        "text": text,
        "reply_markup": {"inline_keyboard": rows},
    })


# ---------------------------------------------------------------------------
# Pending-delivery memory (shared with the mini-app service; same collection)
# ---------------------------------------------------------------------------
def remember_pending(conn, user_id: int, gallery_id: str) -> None:
    try:
        conn.db["miniapp_pending_deliveries"].update_one(
            {"_id": f"{int(user_id)}:{str(gallery_id)}"},
            {"$set": {"user_id": int(user_id),
                      "gallery_id": str(gallery_id),
                      "created_at": time.time()}},
            upsert=True,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("remember_pending failed: %s", e)


def pop_pending(conn, user_id: int, gallery_id: str) -> bool:
    try:
        r = conn.db["miniapp_pending_deliveries"].delete_one(
            {"_id": f"{int(user_id)}:{str(gallery_id)}"})
        return bool(r.deleted_count)
    except Exception:
        return False
