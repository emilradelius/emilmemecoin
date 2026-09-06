"""Find your Telegram chat id.

1. Create a bot with @BotFather and put the token in ``.env``.
2. Send any message to your new bot.
3. Run: ``python -m memebot.tools.whoami``
"""

from __future__ import annotations

import asyncio
import os
import sys

from ..alerts.telegram import resolve_chat_id
from ..config import Config


async def _main() -> int:
    Config.load()  # loads .env
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env first.")
        return 1
    chats = await resolve_chat_id(token)
    if not chats:
        print("No messages found. Send your bot a message, then run this again.")
        return 1
    print("Found these chats:\n")
    for c in chats:
        print(f"  chat_id={c['chat_id']}  ({c['type']}) {c['name'] or ''}")
    print("\nPut the chat_id into TELEGRAM_CHAT_ID in your .env")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
