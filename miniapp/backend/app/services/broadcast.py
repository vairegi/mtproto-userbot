"""
broadcast.py — Admin broadcast service.

Lets admins send a text message (optionally with an inline URL button) to
every registered mini-app user via the admin bot's Bot API. Runs
sequentially with a small rate limit (~25 msg/s cap per Telegram guidance)
and tracks per-run stats in the `miniapp_broadcasts` collection so the
admin panel can show progress and history.

Design notes
------------
- The broadcast is initiated synchronously (admin taps Send) but delivery
  is fired in a background thread so the HTTP request returns immediately
  with a `run_id`. The frontend polls /api/admin/broadcast/status/<id>
  for live progress.
- On common per-user failures (bot blocked / user deactivated) the row is
  counted as `failed_blocked` and the loop continues. A `banned` user is
  skipped entirely.
- The message is sent via `sendMessage` (no media), with `disable_web_page
  _preview=True` by default so we don't accidentally leak unlisted links
  into every recipient's DM.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from .. import db as _mini_db  # miniapp db (users collection lives here)
from ..config import settings

log = logging.getLogger("miniapp.broadcast")

_TG_API = "https://api.telegram.org"
_HTTP_TIMEOUT = 15.0
_PACING_S = 0.05  # ~20 msg/s to stay well under the 30/s global cap.

# Optional: parent-bot db access for the shared broadcasts collection.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in [
    os.path.abspath(os.path.join(_HERE, "..", "..", "..")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..")),
    "/opt/render/project/src",
]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


def _bot_token() -> str:
    return (
        getattr(settings, "admin_bot_token", "")
        or getattr(settings, "bot_token", "")
        or os.environ.get("ADMIN_BOT_TOKEN", "")
        or os.environ.get("BOT_TOKEN", "")
    )


def _col_runs():
    return _mini_db.db()["miniapp_broadcasts"]


# ---------------------------------------------------------------------------
# Bot API helpers
# ---------------------------------------------------------------------------
def _send_one(token: str, chat_id: int, text: str,
              button_text: str = "", button_url: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "chat_id": int(chat_id),
        "text": text,
        "disable_web_page_preview": True,
    }
    if button_text and button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button_text, "url": button_url}]],
        }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as c:
            r = c.post(f"{_TG_API}/bot{token}/sendMessage", json=payload)
        try:
            return r.json() or {"ok": False,
                                "description": f"HTTP {r.status_code}"}
        except Exception:
            return {"ok": False,
                    "description": f"non-JSON HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": f"http error: {e!s}"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def list_recipients() -> List[int]:
    """Return every user_id the mini-app has ever seen, minus banned users."""
    rows = _mini_db.list_users(limit=100000)
    uids: List[int] = []
    for r in rows:
        try:
            if r.get("banned"):
                continue
            uid = int(r.get("_id"))
            if uid > 0:
                uids.append(uid)
        except Exception:
            continue
    return uids


def start_broadcast(text: str,
                    button_text: str = "",
                    button_url: str = "",
                    initiated_by: Optional[int] = None) -> Dict[str, Any]:
    """Kick off a broadcast; returns the run_id + recipient count."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "empty text"}
    if len(text) > 4000:
        return {"ok": False, "reason": "text too long (>4000 chars)"}
    token = _bot_token()
    if not token:
        return {"ok": False, "reason": "no admin bot token configured"}

    recipients = list_recipients()
    if not recipients:
        return {"ok": False, "reason": "no recipients (no users registered)"}

    run_id = uuid.uuid4().hex[:12]
    doc = {
        "_id": run_id,
        "text": text,
        "button_text": button_text or "",
        "button_url": button_url or "",
        "initiated_by": int(initiated_by) if initiated_by else None,
        "total": len(recipients),
        "sent": 0,
        "failed_blocked": 0,
        "failed_other": 0,
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "last_error": "",
    }
    _col_runs().insert_one(doc)

    t = threading.Thread(
        target=_run,
        args=(run_id, token, text, button_text, button_url, recipients),
        daemon=True,
        name=f"broadcast-{run_id}",
    )
    t.start()

    return {"ok": True, "run_id": run_id, "total": len(recipients)}


def _run(run_id: str, token: str, text: str,
         button_text: str, button_url: str,
         recipients: List[int]) -> None:
    sent = failed_blocked = failed_other = 0
    last_error = ""
    for uid in recipients:
        r = _send_one(token, int(uid), text, button_text, button_url)
        if r.get("ok"):
            sent += 1
        else:
            desc = str(r.get("description") or "").lower()
            if any(k in desc for k in (
                "bot can't initiate", "blocked", "deactivated",
                "chat not found", "user is deactivated",
                "not enough rights", "kicked",
            )):
                failed_blocked += 1
            else:
                failed_other += 1
                last_error = str(r.get("description") or "")[:200]

        if (sent + failed_blocked + failed_other) % 25 == 0:
            _col_runs().update_one(
                {"_id": run_id},
                {"$set": {"sent": sent,
                          "failed_blocked": failed_blocked,
                          "failed_other": failed_other,
                          "last_error": last_error}},
            )
        time.sleep(_PACING_S)

    _col_runs().update_one(
        {"_id": run_id},
        {"$set": {"sent": sent,
                  "failed_blocked": failed_blocked,
                  "failed_other": failed_other,
                  "status": "done",
                  "finished_at": time.time(),
                  "last_error": last_error}},
    )
    log.info("broadcast %s finished: sent=%d blocked=%d other=%d",
             run_id, sent, failed_blocked, failed_other)


def status(run_id: str) -> Optional[Dict[str, Any]]:
    doc = _col_runs().find_one({"_id": str(run_id)})
    if not doc:
        return None
    return {
        "run_id": doc["_id"],
        "status": doc.get("status") or "unknown",
        "total": int(doc.get("total") or 0),
        "sent": int(doc.get("sent") or 0),
        "failed_blocked": int(doc.get("failed_blocked") or 0),
        "failed_other": int(doc.get("failed_other") or 0),
        "started_at": doc.get("started_at"),
        "finished_at": doc.get("finished_at"),
        "last_error": doc.get("last_error") or "",
        "text_preview": (doc.get("text") or "")[:140],
    }


def list_recent(limit: int = 10) -> List[Dict[str, Any]]:
    cur = _col_runs().find({}).sort("started_at", -1).limit(int(limit))
    out: List[Dict[str, Any]] = []
    for d in cur:
        out.append({
            "run_id": d["_id"],
            "status": d.get("status") or "unknown",
            "total": int(d.get("total") or 0),
            "sent": int(d.get("sent") or 0),
            "failed_blocked": int(d.get("failed_blocked") or 0),
            "failed_other": int(d.get("failed_other") or 0),
            "started_at": d.get("started_at"),
            "finished_at": d.get("finished_at"),
            "text_preview": (d.get("text") or "")[:140],
        })
    return out
