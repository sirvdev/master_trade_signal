"""
core/signal_executor.py
=======================
Executes parsed signals against MT5.

Key rules:
  - Risk X% of equity per channel (configurable per channel, default 10%)
  - ALL TPs are opened (min_lot floor — never skip a TP due to budget)
  - Pre-announcement: open pre_ann_positions × min_lot with no SL/TP (bare trade)
  - Bare trades tagged for BareTradeWatcher to manage (15 min auto-close)
  - Replies matched back to original signal by reply_to_id
"""

import logging
import math
import os
import uuid
from typing import Optional

from bridge.mt5_bridge import MT5FileBridge
from config import ChannelConfig
from core.ai_parser import ParsedSignal
from db.database import Database

logger       = logging.getLogger(__name__)
trades_log   = logging.getLogger("trades")   # writes to trades.log


def _floor_lot(lot: float, step: float = 0.01, min_lot: float = 0.01) -> float:
    if step <= 0:
        return max(min_lot, lot)
    return max(min_lot, math.floor(lot / step) * step)


class SignalExecutor:
    def __init__(self, bridge: MT5FileBridge, db: Database, notifier=None):
        self.bridge        = bridge
        self.db            = db
        self.notifier      = notifier
        self.min_lot       = float(os.getenv("MIN_LOT",       "0.01"))
        self.max_lot       = float(os.getenv("MAX_LOT",       "10.0"))
        self.lot_step      = float(os.getenv("LOT_STEP",      "0.01"))
        self.contract_size = float(os.getenv("CONTRACT_SIZE", "100.0"))
        self.magic         = int(os.getenv("SIGNAL_MAGIC",    "234567"))

    # ── Dispatch ───────────────────────────────────────────────────────────────

    async def execute(self, signal: ParsedSignal, channel: ChannelConfig,
                       message_id: int = 0):
        if channel.halted:
            await self._notify(
                f"⛔ <b>{channel.name}</b> halted (drawdown limit). Signal ignored.")
            return

        t = signal.signal_type
        try:
            if t == "entry":
                await self._handle_entry(signal, channel, message_id)
            elif t == "pre_announcement":
                await self._handle_pre_announcement(signal, channel, message_id)
            elif t == "scouting":
                await self._handle_scouting(signal, channel)
            elif t == "breakeven":
                await self._handle_breakeven(signal, channel)
            elif t == "tp_hit":
                await self._handle_tp_hit(signal, channel)
            elif t in ("close", "close_all"):
                await self._handle_close(signal, channel, message_id, t == "close_all")
            elif t == "sl_correction":
                await self._handle_sl_correction(signal, channel)
        except Exception as e:
            logger.error(f"[EXECUTOR] {t} error: {e}", exc_info=True)
            await self._notify(f"⚠️ Error processing {t}: {e}")

    # ── Entry ──────────────────────────────────────────────────────────────────

    async def _handle_entry(self, signal: ParsedSignal, channel: ChannelConfig,
                              message_id: int):
        symbol    = signal.symbol or channel.symbol
        direction = signal.direction
        sl        = signal.stop_loss
        tps       = [tp for tp in signal.take_profits if tp > 0]

        if not direction:
            await self._notify("⚠️ Entry: no direction — skipped."); return
        if not sl:
            await self._notify("⚠️ Entry: no SL — skipped."); return
        if not tps:
            await self._handle_pre_announcement(signal, channel, message_id); return

        price = await self.bridge.get_price(symbol, direction)
        if not price:
            await self._notify(f"⚠️ Cannot get price for {symbol}"); return

        # Upgrade pending bare trades
        upgraded = await self._upgrade_bare_trades(channel, symbol, direction, sl, tps)

        # Get equity + size lots
        equity = await self.bridge.get_equity()
        if not equity:
            await self._notify("⚠️ Cannot read equity"); return

        sl_dist = abs(price - sl)
        if sl_dist == 0:
            await self._notify("⚠️ SL distance is zero"); return

        risk_amt   = equity * (channel.risk_pct / 100.0)
        lot_per_tp = _floor_lot(
            risk_amt / (sl_dist * self.contract_size * len(tps)),
            self.lot_step, self.min_lot
        )

        # ALL TPs go market — EA only supports ORDER_TYPE_BUY / ORDER_TYPE_SELL
        classified = []
        for i, tp in enumerate(tps, 1):
            if self._tp_passed(direction, price, tp):
                logger.info(f"[EXECUTOR] TP{i} passed — skip"); continue
            classified.append((i, tp))

        if not classified:
            if upgraded:
                await self._notify(
                    f"✅ Upgraded {len(upgraded)} bare position(s) — all TPs passed.")
            else:
                await self._notify(f"⚠️ All TPs passed for {symbol}")
            return

        # Save signal
        signal_id = f"SIG-{message_id}-{uuid.uuid4().hex[:6].upper()}"
        self.db.save_signal(
            signal_id=signal_id, channel_id=channel.id,
            channel_name=channel.name, message_id=message_id,
            reply_to_id=signal.reply_to_id, raw_text=signal.raw_text,
            symbol=symbol, direction=direction,
            entry_type="market",
            entry_price=signal.entry_price, stop_loss=sl,
            take_profits=tps, status="open"
        )

        # Place all orders as market
        placed = []
        for tp_index, tp_price in classified:
            row_id = self.db.save_position(
                signal_id=signal_id, channel_id=channel.id,
                tp_index=tp_index, tp_price=tp_price,
                lot_size=lot_per_tp, stop_loss=sl, order_type="market"
            )
            res = await self.bridge.place_market_order(
                symbol, direction, lot_per_tp, sl, tp_price,
                comment=f"sig_{channel.name[:8]}")

            if res and res.get("ticket"):
                t  = int(res["ticket"])
                ep = res.get("price", price)
                self.db.update_position_opened(row_id, t, ep)
                placed.append((tp_index, t, tp_price))
                logger.info(
                    f"[EXECUTOR] ✅ TP{tp_index} ticket={t} market "
                    f"{direction} {symbol} lot={lot_per_tp:.2f}")
                trades_log.info(
                    f"OPEN signal={signal_id} channel={channel.name} "
                    f"{direction.upper()} {symbol} TP{tp_index}={tp_price} "
                    f"SL={sl} lot={lot_per_tp:.2f} ticket={t} price={ep}"
                )
            else:
                logger.error(f"[EXECUTOR] ❌ TP{tp_index} failed: {res}")

        arrow    = "🟢" if direction == "buy" else "🔴"
        tp_lines = "\n".join(
            f"  TP{i}: <code>{tp:.2f}</code>" for i, _, tp in placed)
        await self._notify(
            f"{arrow} <b>Signal Executed</b> — {channel.name}\n"
            f"<b>{direction.upper()} {symbol}</b>  ×{len(placed)} position(s)\n"
            f"  SL: <code>{sl:.2f}</code>\n{tp_lines}\n"
            f"  Lot each: <code>{lot_per_tp:.2f}</code>  "
            f"Risk: <code>{channel.risk_pct}%</code>\n"
            + (f"  ♻️ Upgraded {len(upgraded)} bare position(s)\n" if upgraded else "")
            + f"Signal: <code>{signal_id}</code>"
        )

    async def _upgrade_bare_trades(self, channel, symbol, direction, sl, tps) -> list:
        upgraded = []
        for bare in self.db.get_bare_signals(channel.id):
            if bare["symbol"] == symbol and bare["direction"] == direction:
                target_tp = tps[min(2, len(tps) - 1)]
                for pos in self.db.get_open_positions(bare["signal_id"]):
                    if pos["ticket"]:
                        ok = await self.bridge.modify_position(
                            pos["ticket"], sl, target_tp)
                        if ok:
                            upgraded.append(pos["ticket"])
                self.db.upgrade_bare_signal(bare["signal_id"], sl, tps)
        return upgraded

    # ── Pre-announcement ───────────────────────────────────────────────────────

    async def _handle_pre_announcement(self, signal: ParsedSignal,
                                        channel: ChannelConfig, message_id: int):
        symbol    = signal.symbol or channel.symbol
        direction = signal.direction
        if not direction:
            return

        emoji     = "📢🟢" if direction == "buy" else "📢🔴"
        signal_id = f"BARE-{message_id}-{uuid.uuid4().hex[:6].upper()}"

        self.db.save_signal(
            signal_id=signal_id, channel_id=channel.id,
            channel_name=channel.name, message_id=message_id,
            reply_to_id=None, raw_text=signal.raw_text,
            symbol=symbol, direction=direction,
            entry_type="market", entry_price=None,
            stop_loss=None, take_profits=[],
            status="pending", is_bare=True
        )

        opened = []
        for i in range(channel.pre_ann_positions):
            row_id = self.db.save_position(
                signal_id=signal_id, channel_id=channel.id,
                tp_index=i + 1, tp_price=0.0,
                lot_size=self.min_lot, stop_loss=0.0, order_type="market"
            )
            res = await self.bridge.place_market_order(
                symbol, direction, self.min_lot, sl=0.0, tp=0.0,
                comment=f"bare_{channel.name[:8]}")
            if res and res.get("ticket"):
                t = int(res["ticket"])
                self.db.update_position_opened(row_id, t, res.get("price", 0.0))
                opened.append(t)
                trades_log.info(
                    f"OPEN_BARE signal={signal_id} channel={channel.name} "
                    f"{direction.upper()} {symbol} lot={self.min_lot} ticket={t}"
                )

        self.db.update_signal_status(signal_id, "open" if opened else "failed")
        await self._notify(
            f"{emoji} <b>Pre-signal opened</b> — {channel.name}\n"
            f"<b>{direction.upper()} {symbol}</b>  ×{len(opened)}\n"
            f"Tickets: {', '.join(f'<code>{t}</code>' for t in opened)}\n"
            f"⏳ Auto-closes in 15 min if no full signal arrives\n"
            f"Signal: <code>{signal_id}</code>"
        )

    # ── Scouting ───────────────────────────────────────────────────────────────

    async def _handle_scouting(self, signal, channel):
        emoji = "👀🟢" if signal.direction == "buy" else "👀🔴"
        await self._notify(
            f"{emoji} <b>Scouting</b> — {channel.name}\n"
            f"Trader watching for {(signal.direction or '?').upper()} — no trade.")

    # ── Breakeven ──────────────────────────────────────────────────────────────

    async def _handle_breakeven(self, signal, channel):
        modified = 0
        for sig_row in self.db.get_open_signals(channel.id, signal.symbol):
            for pos in self.db.get_open_positions(sig_row["signal_id"]):
                if pos["ticket"] and pos["entry_price"]:
                    if await self.bridge.modify_position(
                            pos["ticket"], float(pos["entry_price"]),
                            float(pos["tp_price"] or 0)):
                        modified += 1
        msg = (f"⚖️ <b>Breakeven</b> — {channel.name}\n{modified} position(s) updated."
               if modified else
               f"⚠️ Breakeven: no open positions for {channel.name}")
        await self._notify(msg)

    # ── TP hit ─────────────────────────────────────────────────────────────────

    async def _handle_tp_hit(self, signal, channel):
        await self._notify(
            f"🎯 <b>TP{signal.tp_number or '?'} Hit</b> — {channel.name} "
            f"{signal.symbol}\n<i>MT5 closed automatically.</i>")

    # ── Close ──────────────────────────────────────────────────────────────────

    async def _handle_close(self, signal, channel, message_id, close_all=False):
        sig_rows = []
        if signal.reply_to_id:
            row = self.db.get_signal_by_message(channel.id, signal.reply_to_id)
            if row:
                sig_rows = [row]
        if not sig_rows:
            all_open = self.db.get_open_signals(
                channel.id, signal.symbol if not close_all else None)
            sig_rows = all_open if close_all else all_open[:1]
        if not sig_rows:
            await self._notify(f"⚠️ Close: no open signals for {channel.name}"); return

        closed = 0
        for row in sig_rows:
            for pos in self.db.get_open_positions(row["signal_id"]):
                if pos["ticket"]:
                    if await self.bridge.close_position(pos["ticket"]):
                        self.db.update_position_closed(pos["ticket"], 0.0, "manual")
                        trades_log.info(
                            f"CLOSE_MANUAL channel={channel.name} "
                            f"ticket={pos['ticket']} signal={row['signal_id']}"
                        )
                        closed += 1
            self.db.update_signal_status(row["signal_id"], "closed", "manual")

        await self._notify(
            f"🔴 <b>{'Close All' if close_all else 'Close'}</b> — {channel.name}\n"
            f"Closed {closed} position(s).")

    # ── SL correction ─────────────────────────────────────────────────────────

    async def _handle_sl_correction(self, signal, channel):
        if not signal.new_sl:
            return
        modified = 0
        for row in self.db.get_open_signals(channel.id, signal.symbol):
            for pos in self.db.get_open_positions(row["signal_id"]):
                if pos["ticket"]:
                    if await self.bridge.modify_position(
                            pos["ticket"], signal.new_sl, float(pos["tp_price"] or 0)):
                        modified += 1
        await self._notify(
            f"🛑 <b>SL → <code>{signal.new_sl:.2f}</code></b> — {channel.name}\n"
            f"{modified} position(s) updated.")

    # ── Helper ─────────────────────────────────────────────────────────────────

    def _tp_passed(self, direction: str, price: float, tp: float) -> bool:
        return price >= tp if direction == "buy" else price <= tp

    async def _notify(self, msg: str):
        if self.notifier:
            try:
                await self.notifier.send(msg)
            except Exception as e:
                logger.debug(f"[EXECUTOR] notify error: {e}")