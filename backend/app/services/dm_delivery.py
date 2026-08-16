"""
dm_delivery.py — BUG 1 fix (hardened).

When a user taps "Queue" on a gallery that is already in the database
channel, we must NOT open a t.me/c/<internal>/<msg> link (which just jumps
them to the channel). Instead, the admin bot copyMessage's the cover post
+ PDF from the database channel directly into the user's DM.

Delivery strategy (in order — try each until one works):
  1. `copyMessage`  — clean copy, no "Forwarded from" tag. Preferred.
  2. `forwardMessage` — fallback if copyMessage refuses (rare permission
     edge case, or a service message that can't be copied).

If Telegram returns "Forbidden: bot can't initiate conversation with a
user", we surface it verbatim so the frontend can tell the user to send
`/start` to the bot first.

The parent bot / worker.py already writes db_cover_msg_id and db_pdf_msg_id
onto the gallery doc when the cover post + PDF land in the DB channel.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

import httpx

from ..config import settings
from . import deletion_scheduler, force_join, share_guard

log = logging.getLogger("miniapp.dm_delivery")

# --- Make parent-bot helpers importable (same trick as queue_bridge) --------
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in [
    os.path.abspath(os.path.join(_HERE, "..", "..", "..")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..")),
    "/opt/render/project/src",
]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import db as _bot_db            # type: ignore
    import gallery_state as _gs     # type: ignore
    HAVE_GS = True
except Exception as e:  # noqa: BLE001
    _bot_db = None
    _gs = None
    HAVE_GS = False
    log.warning("gallery_state/db not importable — DM delivery disabled (%s)", e)


_TG_API = "https://api.telegram.org"
_TIMEOUT = 15.0


def _bot_token() -> str:
    """Resolve the admin bot token from env / settings."""
    return (
        settings.bot_token
        or os.environ.get("BOT_TOKEN", "")
        or os.environ.get("ADMIN_BOT_TOKEN", "")
    )


def _channel_id() -> int:
    """Resolve the database channel id from env / settings."""
    try:
        cid = int(getattr(settings, "database_channel_id", 0) or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid:
        return cid
    for name in ("DATABASE_CHANNEL_ID", "CHANNEL_ID"):
        v = os.environ.get(name)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return 0


# ---------------------------------------------------------------------------
# Low-level Bot API helpers
# ---------------------------------------------------------------------------

def _api_call(
    token: str,
    method: str,
    payload: dict,
    client: httpx.Client,
) -> dict:
    """Call a Bot API method and normalise the response envelope."""
    url = f"{_TG_API}/bot{token}/{method}"
    try:
        r = client.post(url, json=payload, timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": f"http error: {e!s}"}
    try:
        data = r.json() or {}
    except Exception:
        return {"ok": False, "description": f"non-JSON response HTTP {r.status_code}"}
    # Normalise error_code so callers can inspect it uniformly.
    if not data.get("ok") and "error_code" not in data:
        data["error_code"] = r.status_code
    return data


def _copy_or_forward(
    token: str,
    from_chat_id: int,
    chat_id: int,
    message_id: int,
    client: httpx.Client,
) -> dict:
    """Try `copyMessage` first; if it fails for a non-permission reason,
    retry with `forwardMessage`. Returns the last envelope we saw."""
    payload = {
        "chat_id":       int(chat_id),
        "from_chat_id":  int(from_chat_id),
        "message_id":    int(message_id),
        # Feature 2 (Disable sharing): when the admin toggles this on,
        # Telegram blocks the recipient from forwarding / saving the DM.
        **share_guard.payload(),
    }
    r = _api_call(token, "copyMessage", payload, client)
    if r.get("ok"):
        return r

    desc = (r.get("description") or "").lower()
    # If the user hasn't started the bot, forwardMessage will fail the same
    # way — no point retrying, surface the copyMessage error.
    if "bot can't initiate" in desc or "user is deactivated" in desc \
            or "chat not found" in desc or "blocked" in desc:
        return r

    # Otherwise try forwardMessage (drops "Forwarded from" preservation, but
    # covers service messages / media that can't be copied cleanly).
    r2 = _api_call(token, "forwardMessage", payload, client)
    if r2.get("ok"):
        r2["used_forward_fallback"] = True
    return r2 if r2.get("ok") else r


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def deliver_to_dm(gallery_id: str, user_id: int) -> dict:
    """Copy the cover + PDF for `gallery_id` from the DB channel into
    the user's DM. Returns a dict describing what happened. Never raises
    on Telegram-side errors — surfaces them in the dict so the caller can
    decide (fall back to a toast, ask user to /start the bot, etc.).
    """
    if not HAVE_GS:
        return {"ok": False, "delivered": False, "reason": "gallery_state unavailable"}

    gid = str(gallery_id or "").strip().lstrip("#")
    if not gid:
        return {"ok": False, "delivered": False, "reason": "missing gallery_id"}

    token = _bot_token()
    if not token:
        return {"ok": False, "delivered": False, "reason": "BOT_TOKEN not configured"}

    from_chat = _channel_id()
    if not from_chat:
        return {"ok": False, "delivered": False, "reason": "DATABASE_CHANNEL_ID not configured"}

    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return {"ok": False, "delivered": False, "reason": "bad user_id"}

    # --- Feature 3 (Force-join): block delivery until the user has joined
    # every required channel. On block we send them a 'please join' prompt
    # with Join buttons + an 'I've joined' callback (handled by the admin
    # bot, which re-triggers this same function).
    try:
        gate = force_join.check_membership(uid)
    except Exception as e:  # noqa: BLE001
        log.warning("force_join check failed (letting user through): %s", e)
        gate = {"missing": [], "enabled": False}
    if gate.get("missing"):
        force_join.remember_pending(uid, gid)
        force_join.send_join_prompt(uid, gate["missing"], gallery_id=gid)
        return {
            "ok": True,
            "delivered": False,
            "blocked_by_force_join": True,
            "gallery_id": gid,
            "reason": "Please join the required channel(s) — check your DM.",
        }

    # --- Look up the gallery doc --------------------------------------------
    conn = _bot_db.connect()
    try:
        doc = _gs.get(conn, gid) or {}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not doc:
        return {"ok": False, "delivered": False, "reason": "gallery not found",
                "gallery_id": gid}

    cover_msg_id = doc.get("db_cover_msg_id")
    pdf_msg_id   = doc.get("db_pdf_msg_id")

    if not cover_msg_id and not pdf_msg_id:
        return {
            "ok": False,
            "delivered": False,
            "reason": "gallery has no stored DB channel message IDs",
            "gallery_id": gid,
        }

    # --- Fire copyMessage (with forwardMessage fallback) --------------------
    result: dict[str, Any] = {
        "ok": True,
        "delivered": False,
        "gallery_id": gid,
        "cover_copied": False,
        "pdf_copied": False,
    }

    sent_msg_ids: list[int] = []  # collected for the auto-delete scheduler

    with httpx.Client() as client:
        # Cover first — order matters so the PDF replies to the cover in DM.
        if cover_msg_id:
            r = _copy_or_forward(token, from_chat, uid, int(cover_msg_id), client)
            if r.get("ok"):
                result["cover_copied"] = True
                new_mid = int((r.get("result") or {}).get("message_id") or 0)
                if new_mid:
                    sent_msg_ids.append(new_mid)
                if r.get("used_forward_fallback"):
                    result["cover_used_forward"] = True
            else:
                result["cover_error"] = r.get("description") or "unknown"
                result["cover_error_code"] = r.get("error_code")
                log.warning("cover delivery failed gid=%s uid=%s: %s",
                            gid, uid, result["cover_error"])
                # If the user hasn't /start'ed the bot, no point trying the
                # PDF — surface it immediately with a clear reason.
                desc = (r.get("description") or "").lower()
                if "bot can't initiate" in desc:
                    result["ok"] = False
                    result["delivered"] = False
                    result["reason"] = ("Please send /start to the bot in DM "
                                        "first, then try again.")
                    return result

        if pdf_msg_id:
            r = _copy_or_forward(token, from_chat, uid, int(pdf_msg_id), client)
            if r.get("ok"):
                result["pdf_copied"] = True
                new_mid = int((r.get("result") or {}).get("message_id") or 0)
                if new_mid:
                    sent_msg_ids.append(new_mid)
                if r.get("used_forward_fallback"):
                    result["pdf_used_forward"] = True
            else:
                result["pdf_error"] = r.get("description") or "unknown"
                result["pdf_error_code"] = r.get("error_code")
                log.warning("pdf delivery failed gid=%s uid=%s: %s",
                            gid, uid, result["pdf_error"])
                desc = (r.get("description") or "").lower()
                if "bot can't initiate" in desc:
                    result["ok"] = False
                    result["delivered"] = False
                    result["reason"] = ("Please send /start to the bot in DM "
                                        "first, then try again.")
                    return result

    result["delivered"] = result["cover_copied"] or result["pdf_copied"]
    if not result["delivered"]:
        result["ok"] = False
        # Carry a first-class reason so the frontend has something short to toast.
        result["reason"] = (result.get("cover_error")
                            or result.get("pdf_error")
                            or "unknown DM delivery failure")
    else:
        # --- Improvement #4: the DEDUP-DELIVER path was missing the
        # "📨 Sent to your DM" confirmation that the auto-DM path in
        # relay_v2 already sends. Send it here too, from the admin bot,
        # so the user actually gets a Telegram DM (not just an in-app
        # toast). Wrap in a try/except that only logs on failure (never
        # blocks the response).
        #
        # v12.12 (#1): do NOT protect this plain-text confirmation.
        # protect_content=true marks the WHOLE DM chat as protected in
        # Telegram clients, and that chat-level protected state blocks
        # screenshots of everything in the chat — including the mini-app
        # WebView the user opened from it. Media deliveries (cover + PDF)
        # keep their protection via _copy_or_forward(); the confirmation
        # carries no content worth protecting anyway.
        try:
            with httpx.Client() as _client:
                _confirm = _api_call(token, "sendMessage", {
                    "chat_id": int(uid),
                    "text":    "📨 Sent to your DM",
                }, _client)
            if _confirm.get("ok"):
                _mid = int((_confirm.get("result") or {}).get("message_id") or 0)
                if _mid:
                    sent_msg_ids.append(_mid)
            else:
                log.info("dedup-deliver confirmation sendMessage failed: %s",
                         _confirm.get("description"))
        except Exception as _e:  # noqa: BLE001
            log.info("dedup-deliver confirmation raised (non-fatal): %s", _e)

        # Feature 1 (Auto-delete): schedule deletion of the delivered msgs
        # after N hours (no-op unless the admin enabled it). Includes the
        # confirmation message appended above.
        try:
            deletion_scheduler.schedule(uid, sent_msg_ids)
        except Exception as e:  # noqa: BLE001
            log.warning("deletion scheduling failed (non-fatal): %s", e)
        # Force-join pending cleanup: the delivery succeeded, so forget
        # any remembered pending row for this user+gallery.
        try:
            force_join.pop_pending(uid, gid)
        except Exception:
            pass
    return result
