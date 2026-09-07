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
from core.ai_parser import AIParser, build_channel_hint
from core.clock_sync import ClockSkew
from core.signal_adapter import adapt_all, should_fallback
from core.signal_executor import SignalExecutor
from core.signal_parser import Intent, SignalAssembler, SignalParser

logger = logging.getLogger(__name__)


class ChannelListener:
    def __init__(self, channel: ChannelConfig, config: AppConfig,
                 parser: AIParser, executor: SignalExecutor,
                 client: TelegramClient):
        self.channel  = channel
        self.config   = config
        self.parser   = parser          # AI fallback, used only on UNKNOWN
        self.executor = executor
        self.client   = client  # shared TelegramClient from ChannelManager
        self._running = False
        # Deterministic front end. Authoritative for every number; the AI parser
        # is never asked for a price. SignalAssembler is a passthrough unless
        # the channel sets parser.assemble_window_sec > 0.
        self.det = SignalAssembler(SignalParser({
            "id": channel.id, "symbol": channel.symbol,
            "parser": channel.parser or {},
        }))
        # Channel-specific context for the AI fallback. Built once: it is static
        # per channel and there is no reason to rebuild it per message. The AI
        # only ever sees messages the deterministic layer abstained on, so it
        # needs to know this operator's house style to judge them.
        self.ai_hint = build_channel_hint(channel.name, channel.parser or {})
        # Per-channel AI fallback switch. Default true: every channel that
        # worked before this flag existed keeps working exactly as it did.
        self.ai_enabled = bool((channel.parser or {}).get("ai_fallback", True))
        if not self.ai_enabled:
            logger.info(f"[LISTENER] {channel.name}: AI fallback disabled; "
                        f"messages the deterministic parser abstains on are "
                        f"dropped rather than guessed at")
        # Shared across listeners when ChannelManager passes one in; otherwise
        # per-channel. The skew is a property of the machine, not the channel.
        self.clock = ClockSkew()
        self._last_clock_warn: float = 0.0

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

    async def _warn_if_clock_wrong(self, every_sec: int = 600):
        """Escalate a bad clock at most once per interval, per channel.

        This also goes to Telegram, not just the log. A wrong clock silently
        refuses every signal, so the failure mode is "nothing happens", which is
        indistinguishable from a quiet market unless someone is reading the log
        at that moment. Nobody is, at 3am.
        """
        if not self.clock.is_significant:
            return
        import time
        now = time.time()
        if now - self._last_clock_warn < every_sec:
            return
        self._last_clock_warn = now
        diag = self.clock.diagnosis()
        logger.error(f"[CLOCK] {diag}")
        notifier = getattr(self.executor, "notifier", None)
        if notifier:
            try:
                await notifier.send(
                    f"\U0001f552 <b>System clock is wrong</b>\n{diag}\n\n"
                    f"<b>Signals are being refused as stale until this is "
                    f"fixed.</b> Run <code>w32tm /resync /force</code>.")
            except Exception as e:
                logger.debug(f"[CLOCK] notify failed: {e}")

    def _parse_channel_id(self):
        """Convert channel ID string to int or leave as username."""
        try:
            return int(self.channel.id)
        except ValueError:
            return self.channel.id

    async def _price_is_plausible(self, signal) -> tuple[bool, str]:
        """Is this AI-parsed entry even about the instrument we trade?

        The deterministic parser enforces max_entry_deviation; the AI path does
        not go through the parser, so until now nothing checked its numbers at
        all. That matters because some channels are mixed feeds. One of the
        gold channels also posts Indian index options ("BUY DIXON 14500 CE
        ABOVE 391, TARGET 345/430, SL PAID"). The deterministic layer correctly
        abstains on those, which sends them straight to the AI with
        default_symbol=XAUUSDm, and a helpful AI will happily return a XAUUSDm
        buy at 391.

        This is NOT the slippage gate. It is a wide band whose only job is to
        catch numbers from a different instrument, so it is expressed as a
        percentage of the live price rather than in dollars: 5% of gold is
        ~230, and no real gold signal is ever posted 230 away from spot, while
        an option priced at 391 is 91% away. Fails open when there is no quote,
        for the same reason the slippage gate does: refusing every signal
        because the bridge is briefly quiet is worse than the thing it guards.
        """
        if signal.signal_type not in ("entry", "pre_announcement"):
            return True, ""
        band = float((self.channel.parser or {}).get("ai_sanity_band_pct", 5.0))
        if band <= 0:
            return True, ""
        levels = [x for x in (signal.entry_price, signal.entry_low,
                              signal.entry_high, signal.stop_loss)
                  if isinstance(x, (int, float)) and x]
        levels += [x for x in (signal.take_profits or [])
                   if isinstance(x, (int, float)) and x]
        if not levels:
            return True, ""
        price = await self.executor.bridge.get_price(
            self.channel.symbol, (signal.direction or "buy").lower())
        if not price:
            logger.info(f"[{self.channel.name}] no live price: AI sanity "
                        f"band skipped")
            return True, ""
        tol = abs(float(price)) * band / 100.0
        far = [x for x in levels if abs(float(x) - float(price)) > tol]
        if not far:
            return True, ""
        return False, (f"AI-parsed level(s) {sorted(set(far))[:4]} are more "
                       f"than {band:g}% from {self.channel.symbol} at "
                       f"{price:g}; this message is probably about a "
                       f"different instrument")

    async def _process(self, msg: Message):
        if not msg or not msg.text:
            return
        text = msg.text.strip()
        if len(text) < 2:
            return

        # msg.date is UTC. Mark it, so a log line is never read as wall clock.
        ts = msg.date.strftime("%H:%M:%SZ") if msg.date else "?"
        logger.info(
            f"[{self.channel.name}] [{ts}] msg_id={msg.id}: "
            f"{text[:80].replace(chr(10), ' ')}"
        )

        # Detect reply context
        is_reply    = bool(msg.reply_to_msg_id)
        reply_to_id = int(msg.reply_to_msg_id) if is_reply else None

        # msg.date from Telethon is tz-aware UTC, which is what the freshness
        # gate requires. Do NOT substitute a naive local timestamp here: at
        # UTC+3 every message reads as three hours in the future and the gate
        # stops rejecting stale signals.
        msg_dt = msg.date
        sender_id = getattr(msg, "sender_id", None)

        # Calibrate this machine's clock against Telegram's before judging
        # staleness. A wrong clock makes every signal look hours old and the
        # raw age ("message is 25581s old") does not say why.
        self.clock.observe(msg_dt)
        await self._warn_if_clock_wrong()
        now_ref = self.clock.adjusted_now()   # None unless compensation is on

        try:
            signal = None
            res = self.det.feed(text, msg.id, msg_dt, market_price=None,
                                now=now_ref, sender_id=sender_id)

            if res is not None and res.intent == Intent.NEW_SIGNAL \
                    and not res.rejected_reasons and res.signal:
                # Second pass with the live price. This is the only place
                # max_entry_deviation runs, and it is the main slippage
                # defence: it refuses a signal the market has already left.
                price = await self.executor.bridge.get_price(
                    self.channel.symbol, res.signal.direction.lower())
                if price:
                    res = self.det.parser.parse(
                        text, msg.id, msg_dt, market_price=price,
                        now=now_ref, sender_id=sender_id)
                else:
                    res.notes.append("no live price: slippage gate skipped")

            if res is None:
                logger.info(f"[{self.channel.name}] fragment buffered, "
                            f"waiting for stop/target")
                return

            logger.info(
                f"[{self.channel.name}] det → intent={res.intent.value} "
                f"conf={res.confidence:.2f} exec={res.executable}"
                + (f" REFUSED: {'; '.join(res.rejected_reasons)}"
                   if res.rejected_reasons else "")
            )
            # A staleness refusal on a machine with a known-bad clock is almost
            # certainly the clock, not the signal. Say so on the same line so
            # nobody has to correlate it with a warning further up the file.
            if res.rejected_reasons and res.intent == Intent.NEW_SIGNAL:
                try:
                    self.executor.db.log_skipped_signal(
                        self.channel.id, msg.id, res.intent.value,
                        "; ".join(res.rejected_reasons))
                except Exception:
                    pass
            if any("old" in x or "future" in x for x in res.rejected_reasons):
                diag = self.clock.diagnosis()
                if diag:
                    logger.error(f"[{self.channel.name}] the refusal above is "
                                 f"most likely NOT the signal: {diag}")

            if should_fallback(res) and not self.ai_enabled:
                # Deliberately dropped, not silently lost. Some channels post
                # running commentary in a language the AI will happily
                # misread as an order: "Don't buy gold here, price 4335 k upr
                # stable nhi hoti tab tak buy order nhi lena" is an
                # instruction NOT to buy, and a model reading the English
                # fragments returns a buy at 4335. Where the deterministic
                # parser already reads that channel's real signals cleanly,
                # the fallback adds nothing and can only invent trades.
                logger.info(f"[{self.channel.name}] deterministic layer "
                            f"abstained and ai_fallback=false → dropped")
                return

            if should_fallback(res):
                logger.info(f"[{self.channel.name}] deterministic layer "
                            f"abstained → AI fallback")
                signal = await self.parser.parse(
                    text,
                    is_reply       = is_reply,
                    reply_to_id    = reply_to_id,
                    default_symbol = self.channel.symbol,
                    channel_hint   = self.ai_hint,
                )
                signal.is_reply    = is_reply
                signal.reply_to_id = reply_to_id
            else:
                # A single message can carry two instructions ("take profits
                # now and set breakeven"). Run them in order.
                for signal in adapt_all(res, self.channel.symbol,
                                        is_reply=is_reply,
                                        reply_to_id=reply_to_id):
                    logger.info(
                        f"[{self.channel.name}] Parsed → "
                        f"type={signal.signal_type} dir={signal.direction} "
                        f"conf={signal.confidence:.2f}"
                    )
                    await self.executor.execute(signal, self.channel,
                                                message_id=msg.id)
                return

            if signal is None:
                return          # chatter, refused, or no handler. Already logged.

            # ── the AI may not INVENT a blind entry ──────────────────────────
            # A pre-announcement opens a market order with no levels, a 70-pip
            # protective stop and no target. It is the highest-uncertainty
            # action the system takes, and it is the one action whose only
            # sanity gate is vacuous: _price_is_plausible needs levels to
            # check, and a bare call has none, so it fails open every time.
            #
            # The deterministic parser produced all 118 genuine bare calls and
            # correctly abstained on all five of these, which the AI then
            # turned into live trades:
            #   Isabelle FX   "ASIA SESSION = 3 ROUNDS OF SELL! +340 PIPS"
            #   Isabelle FX   "we caught TRIPLE SELL during Asia Session"
            #   Isabelle FX   "NEXT TARGET ZONE ... waiting for price"
            #   Mr William FX "Dont Close Buy Trade Guys"
            #   Sabin Gold    "Breakeven Profits"
            # A results boast, a watchlist, a hold instruction and a breakeven
            # announcement. Blocking this costs zero genuine signals.
            if signal.signal_type == "pre_announcement" and not bool(
                    (self.channel.parser or {}).get("ai_pre_signal", False)):
                why = ("the AI proposed a blind entry from a message the "
                       "deterministic parser refused; a bare call must come "
                       "from the strict path (parser.ai_pre_signal enables it)")
                logger.warning(f"[{self.channel.name}] AI pre-signal REFUSED: {why}")
                try:
                    self.executor.db.log_skipped_signal(
                        self.channel.id, msg.id, "ai_pre_signal", why)
                except Exception:
                    pass
                return

            ok, why = await self._price_is_plausible(signal)
            if not ok:
                logger.warning(f"[{self.channel.name}] AI signal REFUSED: {why}")
                try:
                    self.executor.db.log_skipped_signal(
                        self.channel.id, msg.id, "ai_entry", why)
                except Exception:
                    pass
                return

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
        Process edited messages — only re-process if the edit added trade levels
        that weren't there before. Idempotency in the executor will catch
        duplicates from the same message_id.
        """
        if not msg or not msg.text:
            return
        text = msg.text.strip()

        import re
        has_prices = bool(re.search(r'\b\d{4,5}\b', text))
        has_sl_or_tp = bool(re.search(r'\b(sl|stop\s*loss|tp|target|take\s*profit)\b',
                                       text, re.IGNORECASE))

        # Only re-process if the edit looks like it ADDED a trade plan
        if not (has_prices and has_sl_or_tp):
            return

        logger.info(f"[{self.channel.name}] Edited message {msg.id} — re-processing")
        await self._process(msg)