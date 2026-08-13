"""
set_webhook.py — one-shot helper to register BOT 1's Telegram webhook.

Usage (locally, once BOT 1 is deployed and its Render URL is known):

    python scripts/set_webhook.py https://your-scraperbot.onrender.com

Requires BOT1_TOKEN and (optionally) BOT1_WEBHOOK_SECRET in the env.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from app.config import settings


async def main(base_url: str) -> int:
    if not settings.bot_token:
        print("ERROR: BOT1_TOKEN not set in env", file=sys.stderr)
        return 2
    q = f"?s={settings.webhook_secret}" if settings.webhook_secret else ""
    hook_url = f"{base_url.rstrip('/')}/telegram{q}"
    api = f"https://api.telegram.org/bot{settings.bot_token}/setWebhook"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(api, json={
            "url": hook_url,
            "allowed_updates": ["message", "edited_message"],
            "drop_pending_updates": True,
        })
    print(r.status_code, r.text)
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/set_webhook.py <BASE_URL>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
