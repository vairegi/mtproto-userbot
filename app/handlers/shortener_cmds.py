"""v4.3: admin commands for the VPLINK shortener verification gate.

  /shortener on|off|status     toggle + inspect the gate
  /shortenerapi <url>          VPLINK api base (…?api=TOKEN&url=)
  /setverifytime <hours>       how long one verify unlocks (default 6, 1-168)
  /shortenermsg <text>         landing-page/overlay heading (clear = default)
  /shortenerbotmsg <text>      bot-DM gate text (clear = default)
  /verifymsg <text>            post-verification success text (clear = default)
  /shortenerbtn <label> | <url>   add a secondary button
  /clearshortenerbtns          remove ALL secondary buttons
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services import repo, shortener as sh
from ..utils import esc
from .setup_cmds import _reject_non_admin

log = logging.getLogger("shortener_cmds")
router = Router(name="shortener_cmds")


def _args(msg: Message) -> str:
    return (msg.text or "").partition(" ")[2].strip()


async def _set_or_clear(msg: Message, key: str, label: str) -> None:
    """Shared handler for the three text-setting commands ('clear' = default)."""
    val = _args(msg)
    if not val:
        cur = (await repo.get_setting(key)) or ""
        await msg.reply(f"Current {label}:\n" + (f"<code>{esc(cur)}</code>" if cur
                        else "<i>(default)</i>"), parse_mode="HTML")
        return
    await repo.set_setting(key, None if val.lower() == "clear" else val)
    await msg.reply(f"✅ {label} {'reset to default' if val.lower() == 'clear' else 'updated'}.")


@router.message(Command("shortener"))
async def cmd_shortener(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    arg = _args(msg).lower()
    if arg == "on":
        await repo.set_setting("shortener_enabled", "1")
        await msg.reply("✅ Shortener gate is now <b>ON</b>.", parse_mode="HTML")
        return
    if arg == "off":
        await repo.set_setting("shortener_enabled", None)
        await msg.reply("❌ Shortener gate is now <b>OFF</b> — files deliver instantly.",
                        parse_mode="HTML")
        return
    # default / "status"
    enabled = await sh.is_enabled()
    api = await sh.get_api_base()
    ttl = await sh.get_ttl_hours()
    btns = await sh.get_secondary_buttons()
    await msg.reply(
        "<b>🔗 Shortener Gate</b>\n"
        f"status: <b>{'ON ✅' if enabled else 'OFF ❌'}</b>\n"
        f"api: <code>{esc(api) if api else '(not set — gate fails open)'}</code>\n"
        f"verify unlock: <b>{ttl:g}h</b>\n"
        f"secondary buttons: <b>{len(btns)}</b>\n"
        f"live tokens: <b>{sh.token_count()}</b> · "
        f"unlocked users (this process): <b>{sh.unlocked_count()}</b>\n\n"
        "<i>/shortener on · /shortener off · /shortenerapi · /setverifytime · "
        "/shortenermsg · /shortenerbotmsg · /verifymsg · /shortenerbtn · "
        "/clearshortenerbtns</i>",
        parse_mode="HTML")


@router.message(Command("shortenerapi"))
async def cmd_shortenerapi(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    val = _args(msg)
    if not val:
        cur = await sh.get_api_base()
        await msg.reply(
            "Current API base:\n" + (f"<code>{esc(cur)}</code>" if cur else "<i>(not set)</i>")
            + "\n\nSet it with:\n<code>/shortenerapi https://vplink.in/api?api=YOUR_TOKEN&url=</code>\n"
              "(end with the empty <code>url=</code> param — the destination is appended automatically)",
            parse_mode="HTML")
        return
    if "url=" not in val or not val.startswith("http"):
        await msg.reply("❌ Expected a full template like "
                        "<code>https://vplink.in/api?api=TOKEN&url=</code>", parse_mode="HTML")
        return
    await repo.set_setting("shortener_api", val)
    await msg.reply("✅ Shortener API saved. Test it with /shortener status.")


@router.message(Command("setverifytime"))
async def cmd_setverifytime(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    raw = _args(msg)
    try:
        hours = float(raw)
        if not (1 <= hours <= 168):
            raise ValueError
    except ValueError:
        await msg.reply("Usage: <code>/setverifytime 6</code> — hours, between 1 and 168.",
                        parse_mode="HTML")
        return
    await repo.set_setting("verify_ttl_hours", str(hours))
    await msg.reply(f"✅ One verification now unlocks for <b>{hours:g}h</b>.",
                    parse_mode="HTML")


@router.message(Command("shortenermsg"))
async def cmd_shortenermsg(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await _set_or_clear(msg, "shortener_msg", "overlay/landing text")


@router.message(Command("shortenerbotmsg"))
async def cmd_shortenerbotmsg(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await _set_or_clear(msg, "shortener_bot_msg", "bot-DM gate text")


@router.message(Command("verifymsg"))
async def cmd_verifymsg(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await _set_or_clear(msg, "verify_msg", "post-verify success text")


@router.message(Command("shortenerbtn"))
async def cmd_shortenerbtn(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    args = _args(msg)
    if args.lower() == "clear" or "|" not in args:
        await msg.reply("Usage: <code>/shortenerbtn Button Label | https://…</code>\n"
                        "(<code>/clearshortenerbtns</code> removes all)",
                        parse_mode="HTML")
        return
    label, url = (p.strip() for p in args.split("|", 1))
    if not label or not url.startswith(("http://", "https://", "tg://")):
        await msg.reply("❌ Need a label and a link: <code>Label | https://…</code>",
                        parse_mode="HTML")
        return
    rows = await sh.get_secondary_buttons()
    rows.append([label[:60], url])
    await repo.set_setting_json("shortener_buttons", rows)
    await msg.reply(f"✅ Secondary button added ({len(rows)} total).")


@router.message(Command("clearshortenerbtns"))
async def cmd_clearshortenerbtns(msg: Message) -> None:
    if await _reject_non_admin(msg):
        return
    await repo.set_setting_json("shortener_buttons", [])
    await msg.reply("✅ All secondary shortener buttons removed.")
