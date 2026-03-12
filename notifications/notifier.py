"""
notifications/notifier.py
=========================
Sends notifications to Telegram via python-telegram-bot.
"""

import logging
import os
from datetime import datetime
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token   = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self._bot: Optional[Bot] = None
        self._enabled = bool(self.token and self.chat_id)

        if not self._enabled:
            logger.warning("[NOTIFIER] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — notifications disabled")

    def _get_bot(self) -> Bot:
        if self._bot is None:
            self._bot = Bot(token=self.token)
        return self._bot

    async def send(self, message: str) -> bool:
        if not self._enabled:
            logger.debug(f"[NOTIFIER] (disabled) {message[:80]}")
            return False
        try:
            await self._get_bot().send_message(
                chat_id    = self.chat_id,
                text       = message,
                parse_mode = ParseMode.HTML,
            )
            return True
        except TelegramError as e:
            logger.error(f"[NOTIFIER] Send failed: {e}")
            return False

    async def notify_startup(self, provider: str, channels: list) -> bool:
        ch_list = ", ".join(ch.name for ch in channels) or "none"
        return await self.send(
            f"🚀 <b>Signal Bot Started</b>\n"
            f"AI: <code>{provider}</code>\n"
            f"Channels: {ch_list}\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
        )

    async def notify_shutdown(self) -> bool:
        return await self.send(
            f"🔴 <b>Signal Bot Stopped</b>\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
        )

    async def notify_error(self, component: str, error: str) -> bool:
        return await self.send(
            f"⚠️ <b>Error — {component}</b>\n<code>{error[:300]}</code>"
        )