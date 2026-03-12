"""
channels/channel_manager.py
============================
Spins up one ChannelListener per configured channel,
all sharing a single TelegramClient connection.
Handles reconnection on disconnect.
"""

import asyncio
import logging

from telethon import TelegramClient

from channels.channel_listener import ChannelListener
from config import AppConfig
from core.ai_parser import AIParser
from core.signal_executor import SignalExecutor

logger = logging.getLogger(__name__)


class ChannelManager:
    def __init__(self, config: AppConfig, parser: AIParser,
                 executor: SignalExecutor, notifier=None):
        self.config   = config
        self.parser   = parser
        self.executor = executor
        self.notifier = notifier
        self._client: TelegramClient | None = None
        self._running = False

    async def start(self):
        enabled = [ch for ch in self.config.channels if ch.enabled]
        if not enabled:
            logger.warning("[MANAGER] No enabled channels configured")
            return

        self._running = True
        logger.info(f"[MANAGER] Starting {len(enabled)} channel listener(s)")

        while self._running:
            try:
                await self._run(enabled)
            except Exception as e:
                logger.error(f"[MANAGER] Connection error: {e} — reconnecting in 30s")
                await asyncio.sleep(30)

    async def _run(self, channels):
        self._client = TelegramClient(
            self.config.tg_session_name,
            int(self.config.tg_api_id),
            self.config.tg_api_hash,
        )

        # Register all channel listeners on the shared client
        listeners = []
        for ch in channels:
            listener = ChannelListener(
                channel  = ch,
                config   = self.config,
                parser   = self.parser,
                executor = self.executor,
                client   = self._client,
            )
            await listener.register()
            listeners.append(listener)

        async with self._client:
            me = await self._client.get_me()
            logger.info(f"[MANAGER] ✅ Connected as @{me.username}")
            logger.info(f"[MANAGER] Watching: {[ch.name for ch in channels]}")

            if self.notifier:
                ch_list = "\n".join(f"  • {ch.name} ({ch.id})" for ch in channels)
                try:
                    await self.notifier.send(
                        f"🟢 <b>Signal Bot Active</b>\n"
                        f"Account: @{me.username}\n"
                        f"Channels:\n{ch_list}"
                    )
                except Exception:
                    pass

            await self._client.run_until_disconnected()

    async def stop(self):
        self._running = False
        if self._client:
            await self._client.disconnect()
        logger.info("[MANAGER] Stopped")