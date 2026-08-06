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
      "url": null, "chat_id": null, "invite_hash": null}, ...]
  - `username` has no leading '@' (normalised on write).
  - `url` overrides the default `https://t.me/<username>` join link.
  - `chat_id` is cached on first successful getChat lookup so private
    (-100…) channels also work.
  - `invite_hash` supports private channels added via a t.me/+... link.
"""
from __future__ import annotations

import logging
import os
import re
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

# Matches Telegram invite links:
#   https://t.me/+ABC123
#   http://telegram.me/+ABC123
#   t.me/joinchat/ABC123
#   telegram.me/joinchat/ABC123
_INVITE_LINK_RE = re.compile(
    r"^(?:https?://)?t(?:elegram)?\.me/(?:joinchat/|\+)([A-Za-z0-9_\-]+)/?$",
    re.IGNORECASE,
)


def _bot_token() -> str:
    return (
        settings.bot_token
        or os.environ.get("BOT_TOKEN", "")
        or os.environ.get("ADMIN_BOT_TOKEN", "")
    )


def _pending_col():
    return _db.db()["miniapp_pending_deliveries"]


def _pending_join_requests_col():
    return _db.db()["miniapp_pending_join_requests"]


# ---------------------------------------------------------------------------
# Settings I/O
# ---------------------------------------------------------------------------
def get_channels() -> List[Dict[str, Any]]:
    v = _db.get_setting("force_join_channels", []) or []
    if not isinstance(v, list):
        return []
    return [c for c in v if isinstance(c, dict)
            and (c.get("username") or c.get("chat_id") or c.get("invite_hash"))]


def set_channels(items: List[Dict[str, Any]]) -> None:
    _db.set_setting("force_join_channels", items or [])


def is_enabled() -> bool:
    """Force-join is active iff at least one channel is configured."""
    return len(get_channels()) > 0


def _normalise_handle(raw: str) -> str:
    """Normalise an admin-supplied channel reference.

    Returns one of:
      * `"invite:<hash>"`   — for `t.me/+…` or `t.me/joinchat/…` links.
      * `"<numeric_id>"`     — for `-100…` style numeric channel IDs.
      * `"<handle>"`         — for `@handle` or plain public handles
                               (leading '@' and http(s)://t.me/ prefix
                               stripped).
    """
    s = str(raw or "").strip()
    if not s:
        return s

    m = _INVITE_LINK_RE.match(s)
    if m:
        return "invite:" + m.group(1)

    # Strip protocol / t.me/ prefix from public links like https://t.me/foo.
    low = s.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            s = s[len(prefix):]
            low = s.lower()
            break
    for prefix in ("t.me/", "telegram.me/"):
        if low.startswith(prefix):
            s = s[len(prefix):]
            break

    if s.startswith("@"):
        s = s[1:]
    return s.strip("/ ")


def _split_channel_and_invite(raw: str) -> tuple:
    """Split an admin-supplied 'channel + optional invite link' input.

    Accepts formats like:
      * '-1002252758260'
      * '-1002252758260 https://t.me/+abcXYZ'
      * '-1002252758260, https://t.me/+abcXYZ'
      * '-1002252758260 | https://t.me/+abcXYZ'
      * '@channelname https://t.me/+abcXYZ'
      * a single 'https://t.me/+abcXYZ'
      * a single 'https://t.me/joinchat/abcXYZ'

    Returns (channel_ref, invite_url) — either half may be empty.
    """
    s = (raw or "").strip()
    if not s:
        return ("", "")
    tokens = [t for t in re.split(r"[\s,|;]+", s) if t]
    if not tokens:
        return ("", "")
    channel_ref = tokens[0]
    invite_url = ""
    for tk in tokens[1:]:
        if _INVITE_LINK_RE.match(tk):
            invite_url = tk
            break
    return (channel_ref, invite_url)


def add_channel(username_or_id: str, title: str = "", url: str = "") -> Dict[str, Any]:
    # Improvement #6: extract an optional second-token invite URL from the
    # raw input so admins can pair a numeric -100… ID (or a public @handle)
    # with a joinable invite link in a single Add tap.
    channel_ref, embedded_url = _split_channel_and_invite(username_or_id)
    if not channel_ref:
        raise ValueError("empty channel handle")
    if not (url or "").strip() and embedded_url:
        url = embedded_url

    handle = _normalise_handle(channel_ref)
    if not handle:
        raise ValueError("empty channel handle")

    invite_hash: Optional[str] = None
    if handle.startswith("invite:"):
        invite_hash = handle[len("invite:"):]
        if not invite_hash:
            raise ValueError("empty invite hash")

    numeric_id: Optional[int] = None
    if invite_hash is None and handle.lstrip("-").isdigit():
        try:
            numeric_id = int(handle)
        except ValueError:
            numeric_id = None

    # Improvement #6: if the admin also supplied an invite URL alongside
    # the numeric ID / @handle, harvest its invite hash so join_url()
    # emits the proper t.me/+… link (not the unjoinable t.me/c/<internal>
    # fallback that private numeric channels would otherwise get).
    if url:
        m = _INVITE_LINK_RE.match(url.strip())
        if m and not invite_hash:
            invite_hash = m.group(1)

    current = get_channels()
    for c in current:
        if invite_hash is not None \
                and (c.get("invite_hash") or "") == invite_hash:
            return {"ok": True, "already": True, "channels": current}
        if numeric_id is not None and int(c.get("chat_id") or 0) == numeric_id:
            return {"ok": True, "already": True, "channels": current}
        if invite_hash is None and numeric_id is None \
                and (c.get("username") or "").lower() == handle.lower():
            return {"ok": True, "already": True, "channels": current}

    # Auto-default url for invite-hash rows so join button always works.
    if invite_hash is not None and not (url or "").strip():
        url = f"https://t.me/+{invite_hash}"

    if invite_hash is not None:
        default_title = f"Private channel (+{invite_hash[:6]}…)"
    elif numeric_id is not None:
        default_title = f"#{numeric_id}"
    else:
        default_title = handle

    # `username` is only meaningful for a public @handle. Numeric-ID or
    # invite-hash rows never carry a public handle.
    username_field = ""
    if numeric_id is None and not handle.startswith("invite:"):
        username_field = handle

    new_row: Dict[str, Any] = {
        "username":    username_field,
        "chat_id":     numeric_id,
        "invite_hash": invite_hash,
        "title":       (title or "").strip() or default_title,
        "url":         (url or "").strip() or None,
        "added_at":    time.time(),
    }

    # Best-effort: try to prefill the real channel title from Bot API.
    try:
        real_title = _fetch_channel_title(new_row)
        if real_title:
            new_row["title"] = real_title
    except Exception as e:  # noqa: BLE001
        log.info("add_channel: _fetch_channel_title failed (non-fatal): %s", e)

    current.append(new_row)
    set_channels(current)
    return {"ok": True, "already": False, "channels": current}


def remove_channel(username_or_id: str) -> Dict[str, Any]:
    # Same split-input tolerance as add_channel(): ignore any trailing
    # invite URL the admin may have kept in the field.
    channel_ref, _ = _split_channel_and_invite(username_or_id)
    handle = _normalise_handle(channel_ref or username_or_id)
    invite_hash: Optional[str] = None
    if handle.startswith("invite:"):
        invite_hash = handle[len("invite:"):]

    numeric = None
    if invite_hash is None and handle.lstrip("-").isdigit():
        try:
            numeric = int(handle)
        except ValueError:
            numeric = None

    kept, removed = [], False
    for c in get_channels():
        if invite_hash is not None \
                and (c.get("invite_hash") or "") == invite_hash:
            removed = True
            continue
        if numeric is not None and int(c.get("chat_id") or 0) == numeric:
            removed = True
            continue
        if invite_hash is None and numeric is None \
                and (c.get("username") or "").lower() == handle.lower():
            removed = True
            continue
        kept.append(c)
    set_channels(kept)
    return {"ok": True, "removed": removed, "channels": kept}


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


def _fetch_channel_title(channel: Dict[str, Any]) -> str:
    """Best-effort Bot API getChat → result.title.

    Works when:
      * `username` is set (public @handle), OR
      * `chat_id` is set (already resolved / numeric -100… channel).

    Returns "" for invite-only rows we can't resolve yet.
    """
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

    r = _sync_call("getChat", {"chat_id": chat_ref})
    if not r.get("ok"):
        return ""
    title = ((r.get("result") or {}).get("title") or "").strip()
    return title


def _resolve_chat_id(channel: Dict[str, Any]) -> Optional[int]:
    """Return the numeric chat_id for a configured channel (cached after
    the first successful lookup). Also refreshes the cached title."""
    cid = channel.get("chat_id")
    if cid:
        try:
            return int(cid)
        except (TypeError, ValueError):
            pass

    handle = channel.get("username") or ""
    if not handle:
        # Invite-hash rows can't be resolved from the hash alone — the
        # admin bot must be an admin in the channel first, which will
        # arrive via ChatMember updates. Fail open in that case (handled
        # upstream in check_membership).
        return None

    r = _sync_call("getChat", {"chat_id": f"@{handle}"})
    if not r.get("ok"):
        log.warning("force_join: getChat @%s failed: %s",
                    handle, r.get("description"))
        return None
    result = r.get("result") or {}
    new_cid = int(result.get("id") or 0) or None
    new_title = (result.get("title") or "").strip()
    if new_cid:
        # Cache back to settings so we don't call getChat every delivery.
        current = get_channels()
        for c in current:
            if (c.get("username") or "").lower() == handle.lower():
                c["chat_id"] = new_cid
                if new_title:
                    c["title"] = new_title
        set_channels(current)
    return new_cid


def _has_pending_join_request(user_id: int, chat_id: int) -> bool:
    """True if the user has tapped a request-to-join invite link for
    `chat_id` and is waiting for admin approval. The row is populated
    by admin_bot.py's ChatJoinRequestHandler."""
    try:
        row = _pending_join_requests_col().find_one(
            {"user_id": int(user_id), "chat_id": int(chat_id)}
        )
    except Exception as e:  # noqa: BLE001
        log.warning("force_join: pending-request lookup failed: %s", e)
        return False
    if not row:
        return False
    status = str(row.get("status") or "").lower()
    # 'pending' = request sent, awaiting approval — treat as member.
    # 'approved' = admin accepted — also fine (getChatMember should agree,
    # but keep this here in case Telegram is slow to propagate).
    return status in ("pending", "approved")


def _is_member(user_id: int, chat_id: int) -> bool:
    r = _sync_call("getChatMember",
                   {"chat_id": int(chat_id), "user_id": int(user_id)})
    if not r.get("ok"):
        desc = str(r.get("description") or "").lower()
        if "not found" in desc:
            # User isn't a member — but they may have a PENDING join
            # request (request-to-join channels). Treat pending as member.
            if _has_pending_join_request(user_id, chat_id):
                return True
            return False
        # Bot not admin / chat not found → can't verify. Safe default:
        # let the user through so a misconfigured channel never locks
        # everyone out.
        log.warning("force_join: getChatMember failed uid=%s chat=%s: %s",
                    user_id, chat_id, desc)
        return True
    status = (r.get("result") or {}).get("status", "")
    if status in _MEMBER_STATUSES:
        return True
    # Non-member status (e.g. "left", "kicked"): fall back to the
    # pending-request table for request-to-join channels.
    if _has_pending_join_request(user_id, chat_id):
        return True
    return False


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
            # Invite-only row we can't resolve yet (bot not admin, or
            # invite_hash with no cached chat_id). Fail OPEN so a
            # correctly-configured admin doesn't lock everyone out
            # during setup.
            continue
        if not _is_member(user_id, cid):
            missing.append(c)
    return {"missing": missing, "enabled": True}


def build_join_keyboard(missing_channels: List[Dict[str, Any]],
                        gallery_id: Optional[str] = None) -> Dict[str, Any]:
    rows: List[List[Dict[str, Any]]] = []
    for c in missing_channels[:5]:
        label = (c.get("title") or "").strip()
        if not label:
            # Try to fetch the real title once before falling back.
            try:
                label = _fetch_channel_title(c) or ""
            except Exception:
                label = ""
        if not label:
            label = c.get("username") or "channel"
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
        + "After joining (or after requesting to join), tap ✅ I've joined."
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
