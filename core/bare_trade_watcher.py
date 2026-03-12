"""
core/bare_trade_watcher.py
==========================
Watches positions opened without SL/TP (pre-announcements).
Rules:
  - If full signal arrives for same symbol+direction → upgrade (handled in executor)
  - If 15 minutes pass with no upgrade:
      - profit > 0 → close to lock in gain
      - profit ≤ 0 → close at market to cut loss
"""

import asyncio
import logging
from datetime import datetime, timezone

from bridge.mt5_bridge import MT5FileBridge
from db.database import Database

logger = logging.getLogger(__name__)

BARE_TIMEOUT_SECONDS = int(15 * 60)
CHECK_INTERVAL       = 30  # seconds


class BareTradeWatcher:
    def __init__(self, bridge: MT5FileBridge, db: Database, notifier=None):
        self.bridge   = bridge
        self.db       = db
        self.notifier = notifier
        self._running = False

    async def start(self):
        self._running = True
        logger.info("[BARE] Bare trade watcher started")
        while self._running:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"[BARE] Error: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)

    def stop(self):
        self._running = False

    async def _check(self):
        bare_signals = self.db.get_bare_signals()
        for sig in bare_signals:
            # Parse open time
            opened_str = sig["bare_opened_at"] or sig["created_at"]
            try:
                opened_at = datetime.fromisoformat(opened_str.replace("Z", "+00:00"))
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            now     = datetime.now(timezone.utc)
            age_sec = (now - opened_at).total_seconds()

            if age_sec < BARE_TIMEOUT_SECONDS:
                remaining = int(BARE_TIMEOUT_SECONDS - age_sec)
                logger.debug(
                    f"[BARE] signal={sig['signal_id']} age={int(age_sec)}s "
                    f"— {remaining}s until auto-close"
                )
                continue

            # Timeout reached — close all positions for this signal
            logger.info(
                f"[BARE] Timeout reached for signal={sig['signal_id']} "
                f"({sig['symbol']} {sig['direction']}) — auto-closing"
            )
            await self._close_bare_signal(sig)

    async def _close_bare_signal(self, sig):
        signal_id = sig["signal_id"]
        positions = self.db.get_open_positions(signal_id)

        closed_profit = 0
        closed_loss   = 0
        total_pnl     = 0.0

        for pos in positions:
            ticket = pos["ticket"]
            if not ticket:
                continue

            # Get current P&L from MT5
            mt5_pos = await self.bridge.get_position(ticket)
            profit  = 0.0
            if mt5_pos:
                profit = float(mt5_pos.get("profit", 0.0))

            ok = await self.bridge.close_position(ticket)
            if ok:
                self.db.update_position_closed(ticket, profit, "bare_timeout")
                total_pnl += profit
                if profit > 0:
                    closed_profit += 1
                else:
                    closed_loss += 1
                logger.info(
                    f"[BARE] Closed ticket={ticket} profit={profit:.2f} "
                    f"({'win' if profit > 0 else 'loss'})"
                )
            else:
                logger.error(f"[BARE] Failed to close ticket={ticket}")

        self.db.update_signal_status(signal_id, "closed", "bare_timeout_auto_close")

        pnl_sign = "+" if total_pnl >= 0 else ""
        emoji    = "✅" if total_pnl >= 0 else "⚠️"
        if self.notifier:
            try:
                await self.notifier.send(
                    f"{emoji} <b>Bare Trade Auto-Closed</b> (15min timeout)\n"
                    f"{sig['symbol']} {sig['direction'].upper()}\n"
                    f"Channel: <b>{sig['channel_name']}</b>\n"
                    f"Wins: {closed_profit}  Losses: {closed_loss}\n"
                    f"Net P&L: <code>{pnl_sign}{total_pnl:.2f}</code>\n"
                    f"Signal: <code>{signal_id}</code>"
                )
            except Exception as e:
                logger.debug(f"[BARE] Notify error: {e}")