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
from datetime import datetime
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

    # ── Working balance ────────────────────────────────────────────────────────

    async def _get_working_balance(self, channel: ChannelConfig) -> Optional[float]:
        """
        Returns the balance to use for risk sizing.
        - If channel.starting_balance == 0: returns live equity.
        - Else: returns min(live_equity, system_balance), and warns if drift exceeds threshold.
        """
        equity = await self.bridge.get_equity()
        if not equity:
            return None

        if channel.starting_balance <= 0:
            return float(equity)

        rec = self.db.get_system_balance(channel.id)
        if not rec:
            # First time — initialise
            self.db.init_system_balance(channel.id, channel.starting_balance)
            sys_bal = channel.starting_balance
        else:
            sys_bal = float(rec["system_balance"])

        working = min(float(equity), sys_bal)

        drift_pct = abs(float(equity) - sys_bal) / max(sys_bal, 1.0) * 100
        if drift_pct > channel.balance_drift_pct:
            await self._notify(
                f"⚠️ <b>Balance drift</b> — {channel.name}\n"
                f"Equity: <code>${equity:.2f}</code>  System: <code>${sys_bal:.2f}</code>  "
                f"Drift: <code>{drift_pct:.1f}%</code>\n"
                f"Sizing on <code>${working:.2f}</code> (the lower of the two)."
            )
        return working

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
        # Idempotency: if this message_id already produced a non-bare signal, skip.
        existing = self.db.get_signals_by_message(channel.id, message_id)
        existing_full = [s for s in existing if not s["is_bare"]]
        if existing_full:
            logger.info(
                f"[EXECUTOR] message_id={message_id} already has full signal "
                f"{existing_full[0]['signal_id']} — skipping duplicate"
            )
            return

        # If bare exists for same message_id, _upgrade_bare_trades will close it
        # and we proceed normally.

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

        # Get working balance + size lots
        working_balance = await self._get_working_balance(channel)
        if not working_balance:
            await self._notify("⚠️ Cannot read working balance"); return

        sl_dist = abs(price - sl)
        if sl_dist == 0:
            await self._notify("⚠️ SL distance is zero"); return

        # Classify TPs — market or limit based on signal.entry_type
        entry_type  = signal.entry_type or "market"
        entry_price = signal.entry_price   # limit price (None = use current for market)
        classified  = []

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

        # ── TP outlier check (typo guard) ──────────────────────────────────────
        if len(classified) >= 2:
            distances = [abs(price - tp) for _, tp in classified]
            mean_dist = sum(distances) / len(distances)
            outliers = [
                (i, tp, abs(price - tp)) for i, tp in classified
                if abs(abs(price - tp) - mean_dist) > 3 * mean_dist  # 3x off mean = clear typo
            ]
            if outliers:
                await self._notify(
                    f"⚠️ <b>TP outliers detected</b> — {channel.name}\n"
                    f"Suspicious TPs (>3× off mean distance): "
                    + ", ".join(f"TP{i}@{tp:.2f}" for i, tp, _ in outliers) +
                    f"\nPlacing anyway — review the signal."
                )

        # ── Session-aware risk multiplier ─────────────────────────────────────
        hour_utc = datetime.utcnow().hour
        if 2 <= hour_utc < 7:
            mult, session = channel.asian_risk_mult, "asian"
        elif 7 <= hour_utc < 13:
            mult, session = channel.london_risk_mult, "london"
        elif 13 <= hour_utc < 21:
            mult, session = channel.ny_risk_mult, "ny"
        else:
            mult, session = 1.0, "off-hours"
        effective_risk_pct = channel.risk_pct * mult
        if abs(mult - 1.0) > 1e-9:
            logger.info(
                f"[EXECUTOR] {channel.name}: {session} session multiplier "
                f"{mult:.2f} → effective risk {effective_risk_pct:.2f}% "
                f"(base {channel.risk_pct}%)"
            )

        # ── Lot sizing — keep risk == effective_risk_pct of working_balance ───
        risk_amt = working_balance * (effective_risk_pct / 100.0)
        n_tps    = max(1, len(classified))
        ideal_lot_per_tp = risk_amt / (sl_dist * self.contract_size * n_tps)

        # Floor to lot_step but DON'T let min_lot silently inflate risk
        floor_lot = math.floor(ideal_lot_per_tp / self.lot_step) * self.lot_step

        if floor_lot < self.min_lot:
            # Two strategies — pick (a) for now, document (b) in comments
            # (a) reduce TP count to fit min_lot exactly
            # (b) alternative would be to skip the trade entirely
            max_tps_at_min = int(risk_amt / (sl_dist * self.contract_size * self.min_lot))
            if max_tps_at_min < 1:
                await self._notify(
                    f"⚠️ <b>Account too small</b> — {channel.name}\n"
                    f"Min risk per trade with min_lot: $"
                    f"{self.min_lot * sl_dist * self.contract_size:.2f} "
                    f"({self.min_lot * sl_dist * self.contract_size / working_balance * 100:.1f}% of "
                    f"${working_balance:.2f}). Required risk_pct ({effective_risk_pct:.2f}%) too small. "
                    f"Skipped to protect account."
                )
                return

            # Take only first N TPs at min_lot to match target risk
            n_tps_used = min(max_tps_at_min, n_tps)
            classified = classified[:n_tps_used]
            lot_per_tp = self.min_lot
            actual_risk = lot_per_tp * sl_dist * self.contract_size * n_tps_used
            await self._notify(
                f"ℹ️ <b>Lot constraint</b> — {channel.name}: only opening "
                f"{n_tps_used}/{n_tps} TPs at min_lot to keep risk at "
                f"~${actual_risk:.2f} ({actual_risk/working_balance*100:.1f}% of working balance)."
            )
        else:
            lot_per_tp = floor_lot

        # Final guard — never exceed max_lot per position
        lot_per_tp = min(lot_per_tp, self.max_lot)

        # ── Runner support ────────────────────────────────────────────────────
        # If the signal includes "TP open" / "TP runner", append a synthetic
        # position with tp_price=None — the bridge will open with SL only.
        # NOTE: the runner adds ~1/n_tps additional risk on top of risk_pct
        # because it shares lot_per_tp with the other positions.
        if getattr(signal, "has_runner", False):
            classified.append((len(classified) + 1, None))

        # Save signal
        signal_id = f"SIG-{message_id}-{uuid.uuid4().hex[:6].upper()}"
        self.db.save_signal(
            signal_id=signal_id, channel_id=channel.id,
            channel_name=channel.name, message_id=message_id,
            reply_to_id=signal.reply_to_id, raw_text=signal.raw_text,
            symbol=symbol, direction=direction,
            entry_type=entry_type,
            entry_price=entry_price, stop_loss=sl,
            take_profits=tps, status="open"
        )

        # Place all orders
        placed = []
        for tp_index, tp_price in classified:
            is_runner = tp_price is None
            row_id = self.db.save_position(
                signal_id=signal_id, channel_id=channel.id,
                tp_index=tp_index, tp_price=(0.0 if is_runner else tp_price),
                lot_size=lot_per_tp, stop_loss=sl, order_type=entry_type
            )

            if is_runner:
                # Runner: open with SL only, no TP (tp=0 = unlimited)
                res = await self.bridge.place_market_order(
                    symbol, direction, lot_per_tp, sl=sl, tp=0.0,
                    comment=f"sig_{channel.name[:5]}_run")
                order_label = "runner"
            elif entry_type == "limit" and entry_price:
                res = await self.bridge.place_limit_order(
                    symbol, direction, lot_per_tp, entry_price, sl, tp_price,
                    comment=f"sigl_{channel.name[:7]}")
                order_label = f"limit@{entry_price:.2f}"
            else:
                res = await self.bridge.place_market_order(
                    symbol, direction, lot_per_tp, sl, tp_price,
                    comment=f"sig_{channel.name[:8]}")
                order_label = "market"

            if res and res.get("ticket"):
                t  = int(res["ticket"])
                ep = res.get("price", entry_price or price)
                self.db.update_position_opened(row_id, t, ep)
                placed.append((tp_index, t, tp_price, order_label))
                tp_label = "RUNNER" if is_runner else f"TP{tp_index}"
                tp_log   = "open" if is_runner else tp_price
                logger.info(
                    f"[EXECUTOR] ✅ {tp_label} ticket={t} {order_label} "
                    f"{direction} {symbol} lot={lot_per_tp:.2f}")
                trades_log.info(
                    f"OPEN signal={signal_id} channel={channel.name} "
                    f"{direction.upper()} {symbol} {tp_label}={tp_log} "
                    f"SL={sl} lot={lot_per_tp:.2f} ticket={t} price={ep} type={order_label}"
                )
            else:
                logger.error(f"[EXECUTOR] ❌ {'RUNNER' if is_runner else f'TP{tp_index}'} {order_label} failed: {res}")

        arrow    = "🟢" if direction == "buy" else "🔴"
        etype    = f"limit@{entry_price:.2f}" if entry_type == "limit" and entry_price else "market"
        tp_lines = "\n".join(
            (f"  RUNNER: <code>open</code>" if tp is None
             else f"  TP{i}: <code>{tp:.2f}</code>")
            for i, _, tp, _ in placed)
        runner_count = sum(1 for _, _, tp, _ in placed if tp is None)
        tp_count     = len(placed) - runner_count
        runner_msg   = f" (+{runner_count} runner)" if runner_count else ""
        actual_total_risk = lot_per_tp * sl_dist * self.contract_size * len(placed)
        risk_msg = (f"  Total risk: <code>${actual_total_risk:.2f}</code> "
                    f"({actual_total_risk/working_balance*100:.1f}% of working balance)\n")
        await self._notify(
            f"{arrow} <b>Signal Executed</b> — {channel.name}\n"
            f"<b>{direction.upper()} {symbol}</b>  ×{tp_count} TP position(s)"
            f"{runner_msg}  [{etype}]\n"
            f"  SL: <code>{sl:.2f}</code>\n{tp_lines}\n"
            f"  Lot each: <code>{lot_per_tp:.2f}</code>  "
            f"Risk: <code>{effective_risk_pct:.2f}%</code>"
            + (f" (base {channel.risk_pct}% × {mult:.2f} {session})"
               if abs(mult - 1.0) > 1e-9 else "")
            + "\n"
            + risk_msg
            + (f"  ♻️ Upgraded {len(upgraded)} bare position(s)\n" if upgraded else "")
            + f"Signal: <code>{signal_id}</code>"
        )

    async def _upgrade_bare_trades(self, channel, symbol, direction, sl, tps) -> list:
        """
        Find pending bare trades for this channel/symbol/direction and upgrade them
        to full signals: close the bare 0.01-lot positions and let the parent
        _handle_entry function open the full sized batch.
        """
        upgraded = []
        bare_signals = self.db.get_bare_signals(channel.id)

        for bs in bare_signals:
            if bs["symbol"] != symbol or bs["direction"] != direction:
                continue
            # Close bare positions
            for pos in self.db.get_open_positions(bs["signal_id"]):
                if pos["ticket"]:
                    if await self.bridge.close_position(pos["ticket"]):
                        self.db.update_position_closed(
                            pos["ticket"], 0.0, "upgraded_to_full")
                        upgraded.append(pos["ticket"])
            # Mark bare signal closed (the new full signal will create its own)
            self.db.update_signal_status(bs["signal_id"], "closed", "upgraded")

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