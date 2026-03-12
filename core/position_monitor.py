"""
core/position_monitor.py
========================
Monitors all open MT5 positions.
- Detects externally closed positions (SL/TP hit, manual close)
- Updates live P&L and MFE/MAE in DB
- Triggers per-channel drawdown checks
"""

import asyncio
import logging
from typing import Optional

from bridge.mt5_bridge import MT5FileBridge
from config import AppConfig, ChannelConfig
from db.database import Database

logger = logging.getLogger(__name__)

MONITOR_INTERVAL = 15  # seconds


class PositionMonitor:
    def __init__(self, bridge: MT5FileBridge, db: Database,
                 config: AppConfig, notifier=None):
        self.bridge   = bridge
        self.db       = db
        self.config   = config
        self.notifier = notifier
        self._running = False

    async def start(self):
        self._running = True
        logger.info("[MONITOR] Position monitor started")
        while self._running:
            try:
                await self._cycle()
            except Exception as e:
                logger.error(f"[MONITOR] Error: {e}", exc_info=True)
            await asyncio.sleep(MONITOR_INTERVAL)

    def stop(self):
        self._running = False

    async def _cycle(self):
        # Get all live MT5 positions
        live = await self.bridge.get_all_positions()
        if live is None:
            logger.warning("[MONITOR] get_all_positions returned None — skipping cycle")
            return

        live_tickets = {int(p["ticket"]): p for p in live if p.get("ticket")}

        # Get all positions we think are open
        db_open = self.db.get_all_open_positions()

        for pos in db_open:
            ticket = pos["ticket"]
            if not ticket:
                continue

            if ticket in live_tickets:
                # Still open — update P&L
                mt5 = live_tickets[ticket]
                profit = float(mt5.get("profit", 0.0) or 0.0)
                entry  = float(pos["entry_price"] or 0.0)
                cur    = float(mt5.get("current_price", entry) or entry)

                mfe = max(float(pos["mfe"] or 0.0), max(0.0, profit))
                mae = min(float(pos["mae"] or 0.0), min(0.0, profit))
                self.db.update_position_pnl(ticket, profit, mfe, mae)
            else:
                # Not found on MT5 — was closed externally
                await self._handle_close(pos)

        # Update per-channel equity / drawdown
        await self._update_drawdowns()

    async def _handle_close(self, pos):
        ticket    = pos["ticket"]
        signal_id = pos["signal_id"]
        channel_id = pos["channel_id"]

        # Fetch deal history for P&L
        deal = await self.bridge.get_deal_history(ticket)
        pnl  = 0.0
        reason = "closed"

        if deal and deal.get("status") == "success":
            pnl    = float(deal.get("net_profit") or deal.get("profit") or 0.0)
            reason = deal.get("exit_reason", "closed")

        self.db.update_position_closed(ticket, pnl, reason)
        logger.info(f"[MONITOR] ticket={ticket} closed externally pnl={pnl:.2f} reason={reason}")

        # Notify
        if self.notifier:
            sig = self.db.get_signal(signal_id)
            emoji  = "✅" if pnl >= 0 else "❌"
            ch_name = sig["channel_name"] if sig else channel_id
            sign   = "+" if pnl >= 0 else ""
            try:
                await self.notifier.send(
                    f"{emoji} <b>Position Closed</b> — {ch_name}\n"
                    f"Ticket: <code>{ticket}</code>\n"
                    f"P&L: <code>{sign}{pnl:.2f}</code>  Reason: {reason}"
                )
            except Exception as e:
                logger.debug(f"[MONITOR] Notify error: {e}")

    async def _update_drawdowns(self):
        equity = await self.bridge.get_equity()
        if not equity:
            return

        for ch in self.config.channels:
            if not ch.enabled:
                continue

            # Initialize starting equity for the day if not set
            today_stats = self.db.get_today_stats(ch.id)
            if today_stats:
                start_eq = float(today_stats["starting_equity"] or equity)
            else:
                start_eq = equity

            self.db.upsert_channel_equity(ch.id, equity, start_eq)

            # Calculate drawdown
            if start_eq > 0:
                dd = (start_eq - equity) / start_eq * 100
                ch.current_drawdown = dd

                if dd >= ch.drawdown_pct and not ch.halted:
                    ch.halted = True
                    logger.warning(
                        f"[MONITOR] Channel {ch.name} HALTED — "
                        f"drawdown {dd:.1f}% >= limit {ch.drawdown_pct}%"
                    )
                    if self.notifier:
                        try:
                            await self.notifier.send(
                                f"🚨 <b>Channel Halted: {ch.name}</b>\n"
                                f"Drawdown: <code>{dd:.1f}%</code> "
                                f"(limit: {ch.drawdown_pct}%)\n"
                                f"No new signals will be executed today."
                            )
                        except Exception:
                            pass