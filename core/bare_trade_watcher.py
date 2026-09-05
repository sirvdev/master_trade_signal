"""
core/bare_trade_watcher.py
==========================
Manages positions opened from a bare directional call ("Gold buy now") before
the operator posted any levels.

What changed and why
--------------------
The old rule was a hard close at 15 minutes: profit or loss, the whole position
went. That threw away the reason for taking the bare call in the first place.
GTMO's "Gold buy now" on 2026-08-25 was followed by a 100-pip move inside a
minute and then by nothing at all for hours; a flat 15-minute exit takes a
small piece of that and a flat hold takes an unbounded risk.

The rule now is a timed scale-out. Every BARE_PARTIAL_MINUTES (default 39) with
no instruction from the operator, close a fraction of what is left. The
position bleeds down instead of being cut in one go, so a move that keeps
running keeps some size on it, and one that goes nowhere is out of the book
within a few steps. The protective stop placed at entry (see
_handle_pre_announcement) bounds the downside the whole time.

Measured on the exported corpus: 59% of bare calls are followed by a full
signal within an hour, median head start 69 seconds. The other 41% never get
levels at all, and those are the ones this exists for.

An upgrade to a full signal still takes precedence and is handled in the
executor (_upgrade_bare_trades).
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from bridge.mt5_bridge import MT5FileBridge
from db.database import Database

logger     = logging.getLogger(__name__)
trades_log = logging.getLogger("trades")

# Minutes between scale-out steps while the operator stays silent.
BARE_PARTIAL_MINUTES = float(os.getenv("BARE_PARTIAL_MINUTES", "39"))
# Fraction of the REMAINING size closed at each step.
BARE_PARTIAL_FRACTION = float(os.getenv("BARE_PARTIAL_FRACTION", "0.5"))
# Steps after which whatever is left is closed outright, so a bare position
# cannot linger forever at a size too small to partial any further.
BARE_MAX_STEPS = int(os.getenv("BARE_MAX_STEPS", "4"))
CHECK_INTERVAL = 30  # seconds


class BareTradeWatcher:
    def __init__(self, bridge: MT5FileBridge, db: Database, notifier=None):
        self.bridge   = bridge
        self.db       = db
        self.notifier = notifier
        self._running = False
        # signal_id -> how many scale-out steps have already been taken.
        # In memory on purpose: after a restart a bare position is re-aged from
        # its open time, and starting the count again is the safe direction
        # (one extra partial, never one fewer).
        self._steps: dict[str, int] = {}

    async def start(self):
        self._running = True
        logger.info("[BARE] watcher started — scaling out %.0f%% of what is "
                    "left every %g min with no instruction, flat after %d steps",
                    BARE_PARTIAL_FRACTION * 100, BARE_PARTIAL_MINUTES,
                    BARE_MAX_STEPS)
        while self._running:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"[BARE] Error: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)

    def stop(self):
        self._running = False

    async def _check(self):
        for sig in self.db.get_bare_signals():
            opened_str = sig["bare_opened_at"] or sig["created_at"]
            try:
                opened_at = datetime.fromisoformat(opened_str.replace("Z", "+00:00"))
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            age_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60.0
            due = int(age_min // BARE_PARTIAL_MINUTES)
            if due < 1:
                continue

            sid = sig["signal_id"]
            taken = self._steps.get(sid, 0)
            if due <= taken:
                continue        # this step has already been serviced

            self._steps[sid] = due
            if due >= BARE_MAX_STEPS:
                logger.info("[BARE] %s reached step %d of %d after %.0f min — "
                            "closing what is left", sid, due, BARE_MAX_STEPS,
                            age_min)
                await self._close_remaining(sig, due)
            else:
                logger.info("[BARE] %s: %.0f min with no instruction — "
                            "scale-out step %d of %d", sid, age_min, due,
                            BARE_MAX_STEPS)
                await self._scale_out(sig, due)

    # ── steps ────────────────────────────────────────────────────────────────

    async def _scale_out(self, sig, step: int):
        """Close BARE_PARTIAL_FRACTION of each remaining leg."""
        positions = self.db.get_open_positions(sig["signal_id"])
        closed, pnl = 0, 0.0
        for pos in positions:
            ticket = pos["ticket"]
            if not ticket:
                continue
            lot = float(pos["lot_size"] or 0)
            cut = round(lot * BARE_PARTIAL_FRACTION, 2)
            # Below the broker minimum a "partial" is not possible. Close the
            # leg instead of silently doing nothing, which is what would
            # otherwise happen on a 0.01-lot bare position every single step.
            min_lot = float(os.getenv("MIN_LOT", "0.01"))
            profit = await self._profit(ticket)
            if cut < min_lot or (lot - cut) < min_lot:
                if await self.bridge.close_position(ticket):
                    self.db.update_position_closed(ticket, profit, "bare_scaleout")
                    closed += 1
                    pnl += profit
                    trades_log.info(
                        f"BARE_SCALEOUT_CLOSE signal={sig['signal_id']} "
                        f"ticket={ticket} step={step} lot={lot} pnl={profit:.2f}")
                continue
            if await self.bridge.close_position(ticket, lot=cut):
                # A partial leaves the ticket open at a smaller size; record
                # the new size so the next step measures against what is left.
                self.db.update_position_lot(ticket, round(lot - cut, 2))
                closed += 1
                trades_log.info(
                    f"BARE_SCALEOUT signal={sig['signal_id']} ticket={ticket} "
                    f"step={step} cut={cut} left={round(lot - cut, 2)}")

        if not closed:
            return
        await self._notify(
            f"⏳ <b>Pre-signal scale-out</b> — {sig['channel_name']}\n"
            f"{sig['symbol']} {str(sig['direction']).upper()}\n"
            f"Step <b>{step}</b>/{BARE_MAX_STEPS} — no levels from the "
            f"operator after {step * BARE_PARTIAL_MINUTES:g} min.\n"
            f"Closed {BARE_PARTIAL_FRACTION * 100:.0f}% of {closed} leg(s)"
            + (f", realised <code>{pnl:+.2f}</code>" if pnl else "") + ".\n"
            f"Signal: <code>{sig['signal_id']}</code>")

    async def _close_remaining(self, sig, step: int):
        positions = self.db.get_open_positions(sig["signal_id"])
        total, wins, losses = 0.0, 0, 0
        for pos in positions:
            ticket = pos["ticket"]
            if not ticket:
                continue
            profit = await self._profit(ticket)
            if await self.bridge.close_position(ticket):
                self.db.update_position_closed(ticket, profit, "bare_timeout")
                total += profit
                wins += profit > 0
                losses += profit <= 0
                logger.info("[BARE] closed ticket=%s profit=%.2f", ticket, profit)
            else:
                logger.error("[BARE] failed to close ticket=%s", ticket)

        self.db.update_signal_status(sig["signal_id"], "closed",
                                     "bare_scaleout_complete")
        self._steps.pop(sig["signal_id"], None)
        await self._notify(
            f"{'✅' if total >= 0 else '⚠️'} <b>Pre-signal closed</b> — "
            f"{sig['channel_name']}\n"
            f"{sig['symbol']} {str(sig['direction']).upper()}\n"
            f"The operator never posted levels. Scaled out over "
            f"{step * BARE_PARTIAL_MINUTES:g} min.\n"
            f"Wins {wins}  Losses {losses}  "
            f"Net <code>{total:+.2f}</code>\n"
            f"Signal: <code>{sig['signal_id']}</code>")

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _profit(self, ticket) -> float:
        try:
            pos = await self.bridge.get_position(ticket)
        except Exception:
            return 0.0
        return float((pos or {}).get("profit", 0.0) or 0.0)

    async def _notify(self, text: str):
        if not self.notifier:
            return
        try:
            await self.notifier.send(text)
        except Exception as e:
            logger.debug(f"[BARE] notify error: {e}")
