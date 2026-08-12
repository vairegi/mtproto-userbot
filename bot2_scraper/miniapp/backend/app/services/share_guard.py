"""
share_guard.py — "Disable sharing" toggle.

When the admin enables this, every Bot API call that delivers content to a
user (copyMessage / forwardMessage / sendMessage for the confirmation text)
is sent with `protect_content: true`. Telegram then blocks the recipient
from forwarding / saving / copying that message.

Reading the toggle from Mongo costs ~1ms per request so we don't cache.
"""
from __future__ import annotations

from .. import db as _db


def is_enabled() -> bool:
    return bool(_db.get_setting("share_disabled", False))


def payload() -> dict:
    """Return the extra kwargs to merge into every send/copy call."""
    return {"protect_content": True} if is_enabled() else {}
