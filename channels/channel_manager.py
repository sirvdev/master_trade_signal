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

            reach = await self._check_reachable(channels)

            if self.notifier:
                ch_list = "\n".join(
                    f"  {reach.get(ch.id, ('?', ''))[0]} {ch.name} ({ch.id})"
                    + (f" — {reach[ch.id][1]}" if reach.get(ch.id, ("", ""))[1] else "")
                    for ch in channels)
                bad = [ch.name for ch in channels
                       if reach.get(ch.id, ("", ""))[0] == "❌"]
                try:
                    await self.notifier.send(
                        f"🟢 <b>Signal Bot Active</b>\n"
                        f"Account: @{me.username}\n"
                        f"Channels:\n{ch_list}"
                        + (f"\n\n⚠️ <b>{len(bad)} channel(s) cannot be read.</b> "
                           f"They will look silent all day and will never trade: "
                           f"{', '.join(bad)}. Check the account is still a member "
                           f"and the id is right." if bad else ""))
                except Exception:
                    pass

            await self._client.run_until_disconnected()

    async def _check_reachable(self, channels) -> dict:
        """Prove we can actually READ each channel before trusting the silence.

        Registering a handler always "succeeds": events.NewMessage(chats=<id>)
        accepts an id it cannot resolve and then simply never fires. So a
        channel that was left, renamed, or entered with a wrong id looks
        exactly like a channel having a quiet day — for as long as it takes
        someone to notice, which on 2026-08-24 was a full session.

        This resolves each entity and reads its newest message. It costs one
        round trip per channel at startup and turns an invisible failure into
        a line you cannot miss. Failure here never stops the run: an
        unreachable channel must not take the other fifteen down with it.
        """
        from datetime import datetime, timezone
        out = {}
        for ch in channels:
            try:
                target = int(ch.id)
            except (TypeError, ValueError):
                target = ch.id
            try:
                ent = await self._client.get_entity(target)
                newest = None
                async for m in self._client.iter_messages(ent, limit=1):
                    newest = m.date
                if newest is None:
                    out[ch.id] = ("⚠️", "readable but empty")
                    logger.warning("[MANAGER] %s (%s): readable, but it has no "
                                   "messages at all", ch.name, ch.id)
                    continue
                age = (datetime.now(timezone.utc) - newest).total_seconds()
                human = (f"{age / 86400:.0f}d" if age >= 86400 else
                         f"{age / 3600:.0f}h" if age >= 3600 else
                         f"{age / 60:.0f}m")
                # Not an error: plenty of channels post twice a week. It is
                # here so "no rows in the scorecard" has an explanation next
                # to it instead of being a mystery.
                mark = "✅" if age < 48 * 3600 else "💤"
                out[ch.id] = (mark, f"last post {human} ago")
                logger.info("[MANAGER] %s %s (%s): last post %s ago",
                            mark, ch.name, ch.id, human)
            except Exception as e:
                out[ch.id] = ("❌", f"unreadable: {type(e).__name__}")
                logger.error(
                    "[MANAGER] ❌ %s (%s) CANNOT BE READ: %s: %s — the handler "
                    "is registered but will never fire. This channel will look "
                    "silent and will never trade. Check the account is still a "
                    "member and that the id is correct.",
                    ch.name, ch.id, type(e).__name__, e)
        return out

    async def stop(self):
        self._running = False
        if self._client:
            await self._client.disconnect()
        logger.info("[MANAGER] Stopped")