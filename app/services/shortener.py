"""v4.3: VPLINK shortener verification gate.

Flow: unverified user taps Get File -> bot DMs a gate message with the
hardcoded "🔓 Verify & Unlock" button pointing to an auto-generated VPLINK
short URL that wraps  <BASE_WEBHOOK_URL>/verify?t=<TOKEN> . When the user
finishes the shortener, VPLINK redirects to the Render /verify landing page,
which redirects them to  t.me/<bot>?start=verify_<TOKEN> . The bot validates
the token (one-time, user-bound, 30-min expiry), stamps an in-memory unlock
good for verify_ttl_hours (default 6), then auto-delivers the file they
originally tapped.

Admin commands live in handlers/shortener_cmds.py:
  /shortener on|off|status · /shortenerapi <url> · /setverifytime <hours>
  /shortenermsg <text> · /shortenerbotmsg <text> · /verifymsg <text>
  /shortenerbtn <label> | <url> · /clearshortenerbtns

Storage: everything lives in the shared `settings` key/value store
(repo.get_setting / set_setting / get_setting_json / set_setting_json) plus
two in-process dicts for tokens + unlock stamps — so the feature works
IDENTICALLY on the Turso and Mongo backends with zero schema changes.
(Token persistence across restarts is intentionally skipped: worst case a
user re-taps Verify — acceptable on the free tier and keeps RAM/DB tiny.)

IMPORTANT: tokens + unlock stamps are PROCESS-LOCAL. On Render free tier the
bot runs a single process, so this is safe; if you ever scale beyond one
instance, move _UNLOCKED to the DB.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
import urllib.parse

from . import repo

log = logging.getLogger("shortener")

# ---- defaults / limits ----
DEFAULT_TTL_HOURS = 6            # /setverifytime can override (1..168)
TOKEN_TTL_SEC = 30 * 60          # one-time token dies after 30 minutes
MAX_SECONDS_PER_VERIFY = 30      # single aiohttp call budget for VPLINK API

# ---- in-process state ----
_TOKENS: dict = {}               # token -> {user_id, code, expires} (in-process;
                                 # 30-min TTL makes restart-loss harmless)
# v4.3.1: verification stamps moved OUT of RAM into the DB (repo.verified_*)
# so unlocks survive restarts/redeploys. No _UNLOCKED dict anymore.


# ---------------------------------------------------------------------------
# Settings helpers (all via the shared settings store)
# ---------------------------------------------------------------------------
async def is_enabled() -> bool:
    return await repo.get_setting_bool("shortener_enabled", False)


async def get_api_base() -> str:
    return ((await repo.get_setting("shortener_api")) or "").strip()


async def get_ttl_hours() -> float:
    raw = (await repo.get_setting("verify_ttl_hours")) or ""
    try:
        h = float(raw)
        return h if 1 <= h <= 168 else DEFAULT_TTL_HOURS
    except (TypeError, ValueError):
        return DEFAULT_TTL_HOURS


async def get_gate_text() -> str:
    """Bot-DM gate message (/shortenerbotmsg)."""
    return (((await repo.get_setting("shortener_bot_msg")) or "").strip()
            or _default_gate_text())


async def get_overlay_text() -> str:
    """Mini-app/overlay text (/shortenermsg). Reused as the landing-page
    heading so the command always has a visible effect in the DM-only flow."""
    return (((await repo.get_setting("shortener_msg")) or "").strip()
            or _default_gate_text())


async def get_verify_text() -> str:
    """Post-verification success text (/verifymsg)."""
    return (((await repo.get_setting("verify_msg")) or "").strip()
            or _default_verify_text())


async def get_secondary_buttons() -> list:
    rows = (await repo.get_setting_json("shortener_buttons", [])) or []
    out = []
    for pair in rows:
        try:
            label, url = str(pair[0])[:60], str(pair[1])
        except Exception:
            continue
        if label and url:
            out.append([label, url])
    return out


def _default_gate_text() -> str:
    return ("🔒 <b>Verification required</b>\n\n"
            "To receive this file, please complete a quick one-time "
            "verification.\n"
            "<blockquote>Tap <b>🔓 Verify & Unlock</b>, finish the short link, "
            "then come back — your file will be sent automatically.</blockquote>")


def _default_verify_text() -> str:
    return "✅ <b>Verified!</b> You're unlocked — your file is on its way."


# ---------------------------------------------------------------------------
# Token + unlock lifecycle (in-process)
# ---------------------------------------------------------------------------
def _sweep() -> None:
    now = time.monotonic()
    for t, rec in [x for x in _TOKENS.items() if x[1]["expires"] <= now]:
        _TOKENS.pop(t, None)



async def is_verified(user_id: int) -> bool:
    """DB-backed: survives restarts. Reads the stored wall-clock expiry."""
    exp = await repo.verified_get(int(user_id))
    return bool(exp and exp > time.time())


async def _mark_verified(user_id: int) -> None:
    await repo.verified_set(int(user_id), time.time() + (await get_ttl_hours()) * 3600)


def new_token(user_id: int, code: str) -> str:
    """Mint a one-time, user-bound, 30-minute token."""
    _sweep()
    tok = secrets.token_urlsafe(18)
    _TOKENS[tok] = {"user_id": int(user_id), "code": code,
                    "expires": time.monotonic() + TOKEN_TTL_SEC}
    return tok


def peek_token(token: str) -> dict | None:
    """Read a token WITHOUT consuming it (used by the /verify landing page)."""
    _sweep()
    return _TOKENS.get(token)


async def consume_token(token: str, user_id: int):
    """Strict redemption: exists, not expired, bound to THIS user, one-time.
    On success marks the user verified and returns the stored cover code."""
    _sweep()
    rec = _TOKENS.pop(token, None)          # pop => one-time, replay-proof
    if not rec:
        return None
    if rec["user_id"] != int(user_id):
        return None
    if rec["expires"] <= time.monotonic():
        return None
    await _mark_verified(user_id)
    return rec.get("code")


def token_count() -> int:
    _sweep()
    return len(_TOKENS)


async def unlocked_count() -> int:
    """Verified users currently unlocked (DB)."""
    return await repo.verified_count()


# ---------------------------------------------------------------------------
# VPLINK shortening (ShrinkMe/vplink.in style:  ?api=TOKEN&url=<dest> )
# ---------------------------------------------------------------------------
async def make_short_url(destination: str) -> str:
    """Wrap `destination` in a VPLINK short link via the configured API base.

    The base is the FULL template with the url param EMPTY, e.g.
      https://vplink.in/api?api=TOKEN&url=
    We append the url-encoded destination ourselves. Response is auto-detected:
    JSON  {"status":"success","shortenedUrl":"..."}  or  plain-text URL
    (the &format=text flavour). Any failure raises — caller falls back."""
    base = await get_api_base()
    if not base:
        raise RuntimeError("shortener API not configured (/shortenerapi)")
    import aiohttp
    url = base + urllib.parse.quote(destination, safe="")
    timeout = aiohttp.ClientTimeout(total=MAX_SECONDS_PER_VERIFY)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(url) as resp:
            body = (await resp.text()).strip()
    # Auto-detect JSON vs plain-text.
    if body.startswith("{"):
        import json
        data = json.loads(body)
        if data.get("status") == "success" and data.get("shortenedUrl"):
            return data["shortenedUrl"]
        raise RuntimeError(f"vplink error: {data.get('message', body[:120])}")
    if body.lower().startswith("http"):
        return body
    raise RuntimeError(f"unexpected vplink response: {body[:120]}")


# ---------------------------------------------------------------------------
# Gate message (hardcoded primary button + optional secondary buttons)
# ---------------------------------------------------------------------------
async def send_gate(bot, user_id: int, code: str) -> bool:
    """DM the verification gate. Returns True if the gate was sent (delivery
    must STOP), False if the shortener is disabled/misconfigured (delivery
    proceeds normally)."""
    if not await is_enabled():
        return False
    if await is_verified(user_id):
        return False
    if not await get_api_base():
        log.warning("shortener ON but /shortenerapi not set — failing OPEN")
        return False

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from .posting import get_bot_username
    from ..config import settings

    tok = new_token(user_id, code)
    base_url = (settings.base_webhook_url or "").rstrip("/")
    destination = f"{base_url}/verify?t={tok}"
    try:
        short = await make_short_url(destination)
    except Exception as e:
        log.exception("vplink shorten failed: %s — failing OPEN", e)
        return False  # never lock users out because the shortener API hiccuped

    username = await get_bot_username(bot)
    rows = [[InlineKeyboardButton(text="🔓 Verify & Unlock", url=short)]]
    for label, url in await get_secondary_buttons():
        rows.append([InlineKeyboardButton(text=label, url=url)])
    if username:
        rows.append([InlineKeyboardButton(
            text="✅ I've Verified — Continue",
            url=f"https://t.me/{username}?start=verify_{tok}")])

    await bot.send_message(chat_id=user_id, text=await get_gate_text(),
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                           parse_mode="HTML")
    return True


# ---------------------------------------------------------------------------
# /verify landing page (aiohttp) — auto-redirect + fallback button
# ---------------------------------------------------------------------------
def landing_html(deep_link: str, heading: str) -> str:
    esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))
    heading = re.sub(r"<[^>]+>", "", heading)  # strip HTML for the page title
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="1;url={esc(deep_link)}">
<title>Verified ✅</title>
<style>
  body{{margin:0;min-height:100vh;display:flex;align-items:center;
       justify-content:center;background:#0f172a;color:#e2e8f0;
       font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
  .card{{text-align:center;padding:40px 28px;max-width:420px}}
  .tick{{font-size:64px;line-height:1}}
  h1{{font-size:22px;margin:16px 0 8px}}
  p{{color:#94a3b8;font-size:14px;margin:0 0 24px}}
  a.btn{{display:inline-block;background:#22c55e;color:#052e16;
        font-weight:700;text-decoration:none;padding:14px 28px;
        border-radius:12px;font-size:16px}}
  a.btn:active{{transform:scale(.97)}}
  .hint{{margin-top:18px;font-size:12px;color:#64748b}}
</style></head>
<body><div class="card">
  <div class="tick">✅</div>
  <h1>{esc(heading)}</h1>
  <p>Verification complete — taking you back to Telegram…</p>
  <a class="btn" href="{esc(deep_link)}">✅ Continue in Telegram</a>
  <div class="hint">If nothing happens, tap the button above.</div>
</div>
<script>setTimeout(function(){{window.location.href={deep_link!r}}},900);</script>
</body></html>"""
