"""Telegram delivery and control.

Implemented directly against the Bot API rather than through a framework:
the bot needs ``sendMessage`` and long-polled ``getUpdates`` and nothing else,
and keeping the dependency surface small matters for something you will leave
running unattended on a small VPS for months.

Commands are the runtime control surface - notably ``/mode``, which is how
you switch between alerts-only, paper and live trading without touching the
machine, and ``/panic``, which stops everything immediately.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from ..http import HttpClient

log = logging.getLogger(__name__)

API = "https://api.telegram.org"

CommandHandler = Callable[[list[str]], Awaitable[str]]


class TelegramBot:
    def __init__(self, token: str, chat_id: str | int) -> None:
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self.token = token
        self.chat_id = str(chat_id)
        self.http = HttpClient(rate=2.0, timeout=35.0)
        self._handlers: dict[str, CommandHandler] = {}
        self._offset: int | None = None
        self._running = False

    @property
    def _base(self) -> str:
        return f"{API}/bot{self.token}"

    # --- sending ---------------------------------------------------------
    async def send(self, text: str, *, silent: bool = False,
                   chat_id: str | None = None) -> bool:
        """Send one message. Long messages are split on line boundaries -
        Telegram's hard limit is 4096 characters and a truncated exit alert
        would be worse than a split one."""
        target = chat_id or self.chat_id
        ok = True
        for chunk in self._split(text):
            resp = await self.http.post_json(
                f"{self._base}/sendMessage",
                {
                    "chat_id": target,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "disable_notification": silent,
                },
            )
            if not resp or not resp.get("ok"):
                log.error("telegram send failed: %s", resp)
                ok = False
        return ok

    @staticmethod
    def _split(text: str, limit: int = 4000) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks, current = [], ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > limit:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        return chunks

    # --- commands --------------------------------------------------------
    def command(self, name: str) -> Callable[[CommandHandler], CommandHandler]:
        def deco(fn: CommandHandler) -> CommandHandler:
            self._handlers[name] = fn
            return fn
        return deco

    def register(self, name: str, fn: CommandHandler) -> None:
        self._handlers[name] = fn

    async def set_my_commands(self, descriptions: dict[str, str]) -> None:
        await self.http.post_json(
            f"{self._base}/setMyCommands",
            {"commands": [{"command": k, "description": v}
                          for k, v in descriptions.items()]},
        )

    async def poll_commands(self) -> None:
        """Long-poll for commands until cancelled."""
        self._running = True
        while self._running:
            try:
                params: dict[str, Any] = {"timeout": 30}
                if self._offset is not None:
                    params["offset"] = self._offset
                data = await self.http.get_json(
                    f"{self._base}/getUpdates", params, use_cache=False
                )
                if not data or not data.get("ok"):
                    await asyncio.sleep(5)
                    continue
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    await self._dispatch(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram poll failed; retrying")
                await asyncio.sleep(5)

    async def _dispatch(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or update.get("edited_message") or {}
        text = (msg.get("text") or "").strip()
        chat = str((msg.get("chat") or {}).get("id", ""))
        if not text.startswith("/"):
            return

        # Only the configured chat may control the bot. Without this, anyone
        # who finds the bot's username could flip it into live trading.
        if chat != self.chat_id:
            log.warning("ignoring command from unauthorised chat %s", chat)
            return

        parts = text.split()
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1:]

        handler = self._handlers.get(cmd)
        if handler is None:
            await self.send(f"Unknown command /{cmd}. Try /help")
            return
        try:
            reply = await handler(args)
            if reply:
                await self.send(reply)
        except Exception as exc:
            log.exception("command /%s failed", cmd)
            await self.send(f"⚠️ /{cmd} failed: {type(exc).__name__}: {exc}")

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        self._running = False
        await self.http.close()


async def resolve_chat_id(token: str) -> list[dict[str, Any]]:
    """Helper for first-time setup: message the bot, then run this to find
    your chat id. Used by ``python -m memebot.tools.whoami``."""
    async with HttpClient(rate=2.0) as http:
        data = await http.get_json(f"{API}/bot{token}/getUpdates", use_cache=False)
    if not data or not data.get("ok"):
        return []
    out = []
    for u in data.get("result", []):
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            out.append({
                "chat_id": chat["id"],
                "type": chat.get("type"),
                "name": chat.get("username") or chat.get("title") or chat.get("first_name"),
            })
    return out
