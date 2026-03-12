"""
channels/channel_listener.py
=============================
Telethon-based listener for a single Telegram channel.
Handles: new messages, edited messages, reply context.
"""

import logging
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.types import Message

from config import AppConfig, ChannelConfig
from core.ai_parser import AIParser
from core.signal_executor import SignalExecutor

logger = logging.getLogger(__name__)


class ChannelListener:
    def __init__(self, channel: ChannelConfig, config: AppConfig,
                 parser: AIParser, executor: SignalExecutor,
                 client: TelegramClient):
        self.channel  = channel
        self.config   = config
        self.parser   = parser
        self.executor = executor
        self.client   = client  # shared TelegramClient from ChannelManager
        self._running = False

    async def register(self):
        """Register event handlers for this channel on the shared client."""
        channel_id = self._parse_channel_id()

        @self.client.on(events.NewMessage(chats=channel_id))
        async def on_new(event: events.NewMessage.Event):
            await self._process(event.message)

        @self.client.on(events.MessageEdited(chats=channel_id))
        async def on_edit(event: events.MessageEdited.Event):
            await self._process_edit(event.message)

        logger.info(f"[LISTENER] Registered handler for channel: {self.channel.name} ({self.channel.id})")

    def _parse_channel_id(self):
        """Convert channel ID string to int or leave as username."""
        try:
            return int(self.channel.id)
        except ValueError:
            return self.channel.id

    async def _process(self, msg: Message):
        if not msg or not msg.text:
            return
        text = msg.text.strip()
        if len(text) < 2:
            return

        ts = msg.date.strftime("%H:%M:%S") if msg.date else "?"
        logger.info(
            f"[{self.channel.name}] [{ts}] msg_id={msg.id}: "
            f"{text[:80].replace(chr(10), ' ')}"
        )

        # Detect reply context
        is_reply    = bool(msg.reply_to_msg_id)
        reply_to_id = int(msg.reply_to_msg_id) if is_reply else None

        try:
            signal = await self.parser.parse(
                text,
                is_reply       = is_reply,
                reply_to_id    = reply_to_id,
                default_symbol = self.channel.symbol,
            )
            signal.is_reply    = is_reply
            signal.reply_to_id = reply_to_id

            logger.info(
                f"[{self.channel.name}] Parsed → "
                f"type={signal.signal_type} dir={signal.direction} "
                f"conf={signal.confidence:.2f}"
            )
            await self.executor.execute(signal, self.channel, message_id=msg.id)
        except Exception as e:
            logger.error(f"[{self.channel.name}] Processing error: {e}", exc_info=True)

    async def _process_edit(self, msg: Message):
        """
        Process edited messages — provider sometimes sends empty template first,
        then edits in the actual prices. Only act if it looks like a real signal.
        """
        if not msg or not msg.text:
            return
        text = msg.text.strip()

        # Only act on edits that look like they contain prices (4+ digit numbers)
        import re
        if not re.search(r'\b\d{4,5}\b', text):
            return

        logger.info(f"[{self.channel.name}] Edited message — re-processing")
        await self._process(msg)