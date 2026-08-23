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
import os
from datetime import datetime
from typing import Optional

from bridge.mt5_bridge import MT5FileBridge
from config import AppConfig, ChannelConfig
from db.database import Database

logger     = logging.getLogger(__name__)
trades_log = logging.getLogger("trades")

MONITOR_INTERVAL = 5  # seconds

# ── Runner trailing ───────────────────────────────────────────────────────────
# A runner is the leg opened with no take profit (tp_price == 0) when the
# operator posts "TP: open". It was being opened correctly and then left
# untouched for the rest of its life: no target, and the original stop still
# sitting below entry. The whole point of a runner is that it keeps its upside
# while its risk is progressively removed, so it needs a trailing stop.
#
# Trailing runs here rather than in the EA on purpose. This process already
# polls every position every 5s, it is the only place that knows which leg is
# the runner and whether the other legs have been reached, and modify_position
# is an existing, tested path. The position stays an ordinary MT5 position, so
# it remains closable by hand or by the EA at any time.
RUNNER_TRAIL_ENABLED   = os.getenv("RUNNER_TRAIL_ENABLED", "true").lower() == "true"
# Start trailing once this many of the signal's other legs have closed.
# 0 means "wait until every other leg is done" (the default reading of
# "trail the runner after the other TPs are reached").
RUNNER_TRAIL_AFTER_TPS = int(os.getenv("RUNNER_TRAIL_AFTER_TPS", "0"))
# Distance held behind price, in price units. 0 = reuse the signal's own
# original stop distance, which is already sized to this operator's volatility.
RUNNER_TRAIL_DISTANCE  = float(os.getenv("RUNNER_TRAIL_DISTANCE", "0"))
# Minimum improvement before sending another modify. Stops the 5s loop from
# spamming the terminal with sub-tick adjustments.
RUNNER_TRAIL_STEP      = float(os.getenv("RUNNER_TRAIL_STEP", "0.50"))


class PositionMonitor:
    def __init__(self, bridge: MT5FileBridge, db: Database,
                 config: AppConfig, notifier=None):
        self.bridge   = bridge
        self.db       = db
        self.config   = config
        self.notifier = notifier
        self._running = False
        # Tickets already handed to the EA's trailing engine.
        self._armed: set[int] = set()
        # Cleared the first time an EA answers "Unknown action" for
        # set_trailing, so an older build degrades to Python trailing instead
        # of retrying a command it will never understand.
        self._ea_trailing = True

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

        # Trail any runner whose sibling take-profit legs are done
        if RUNNER_TRAIL_ENABLED:
            await self._trail_runners(db_open, live_tickets)

        # Zone-ladder housekeeping and per-channel breakeven policy
        await self._manage_ladders(db_open, live_tickets)
        await self._auto_breakeven(db_open, live_tickets)

    # ── Zone ladder: cancel the legs that price left behind ───────────────────

    async def _manage_ladders(self, db_open, live_tickets):
        """Once price has run far enough past the filled leg, delete the
        unfilled discount legs of the same signal.

        The reason is asymmetric risk: a limit sitting below a market that has
        already left is not a discount any more, it is an invitation for a
        pullback to fill you right before the move against you, on a position
        whose stop was sized for a completely different entry.
        """
        by_signal = {}
        for pos in db_open:
            by_signal.setdefault(pos["signal_id"], []).append(pos)

        for signal_id, legs in by_signal.items():
            try:
                sig = self.db.get_signal(signal_id)
                if not sig or not sig["direction"]:
                    continue
                ch = self._channel_for(sig["channel_id"])
                if ch is None:
                    continue
                pips = float((ch.parser or {}).get("ladder_cancel_pips", 0) or 0)
                if pips <= 0:
                    continue
                pip_value = float((ch.parser or {}).get("pip_value", 0.1))
                trigger = pips * pip_value
                direction = str(sig["direction"]).lower()

                filled = [p for p in legs if p["ticket"] in live_tickets]
                if not filled:
                    continue
                cur = max(float(live_tickets[p["ticket"]].get("current_price") or 0)
                          for p in filled)
                best_entry = [float(p["entry_price"] or 0) for p in filled
                              if p["entry_price"]]
                if not best_entry or cur <= 0:
                    continue
                anchor = max(best_entry) if direction == "buy" else min(best_entry)
                moved = (cur - anchor) if direction == "buy" else (anchor - cur)
                if moved < trigger:
                    continue

                pending = await self.bridge.get_all_orders()
                if pending is None:
                    continue
                pend = {int(o["ticket"]) for o in pending if o.get("ticket")}
                cancelled = 0
                for p in legs:
                    t = p["ticket"]
                    if t and int(t) in pend:
                        if await self.bridge.cancel_order(int(t)):
                            self.db.update_position_closed(int(t), 0.0,
                                                           "ladder_cancelled")
                            cancelled += 1
                if cancelled:
                    logger.info("[MONITOR] %s: price moved %.2f (%.0f pips) past "
                                "%.2f — cancelled %d unfilled ladder leg(s)",
                                sig["channel_name"], moved, moved / pip_value,
                                anchor, cancelled)
                    trades_log.info(
                        f"LADDER_CANCEL signal={signal_id} "
                        f"channel={sig['channel_id']} legs={cancelled} "
                        f"moved={moved:.2f} anchor={anchor}")
                    if self.notifier:
                        try:
                            await self.notifier.send(
                                f"🪜 <b>Ladder trimmed</b> — {sig['channel_name']}\n"
                                f"Price ran {moved / pip_value:.0f} pips past "
                                f"<code>{anchor}</code>.\n"
                                f"{cancelled} unfilled leg(s) cancelled.")
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"[MONITOR] ladder management error on {signal_id}: {e}",
                             exc_info=True)

    def _channel_for(self, channel_id):
        if not self.config:
            return None
        return next((c for c in self.config.channels
                     if str(c.id) == str(channel_id)), None)

    # ── Per-channel automatic breakeven ───────────────────────────────────────

    async def _auto_breakeven(self, db_open, live_tickets):
        """Move a signal's remaining legs to entry when the channel's policy
        says so, rather than only when the operator remembers to post it."""
        by_signal = {}
        for pos in db_open:
            by_signal.setdefault(pos["signal_id"], []).append(pos)

        for signal_id, legs in by_signal.items():
            try:
                sig = self.db.get_signal(signal_id)
                if not sig or not sig["direction"]:
                    continue
                ch = self._channel_for(sig["channel_id"])
                be = (ch.breakeven if ch else None) or {}
                tp_idx = int(be.get("on_tp_index", 0) or 0)
                on_pips = float(be.get("on_pips", 0) or 0)
                if tp_idx <= 0 and on_pips <= 0:
                    continue
                pip_value = float((ch.parser or {}).get("pip_value", 0.1))
                buffer_px = float(be.get("buffer_pips", 0) or 0) * pip_value
                direction = str(sig["direction"]).lower()

                armed = False
                why = ""
                if tp_idx > 0:
                    done = [p for p in self.db.get_positions_by_signal(signal_id)
                            if p["status"] == "closed"
                            and str(p["close_reason"]).lower() == "tp"
                            and (p["tp_index"] or 99) <= tp_idx]
                    if done:
                        armed, why = True, f"TP{tp_idx} reached"

                for p in legs:
                    t = p["ticket"]
                    if not t or int(t) not in live_tickets:
                        continue
                    entry = float(p["entry_price"] or 0)
                    if entry <= 0:
                        continue
                    mt5 = live_tickets[int(t)]
                    cur = float(mt5.get("current_price") or 0)
                    live_sl = float(mt5.get("sl") or 0)
                    if cur <= 0:
                        continue

                    leg_armed, leg_why = armed, why
                    if not leg_armed and on_pips > 0:
                        moved = (cur - entry) if direction == "buy" else (entry - cur)
                        if moved >= on_pips * pip_value:
                            leg_armed = True
                            leg_why = f"{moved / pip_value:.0f} pips in profit"
                    if not leg_armed:
                        continue

                    want = round(entry + buffer_px, 3) if direction == "buy" \
                        else round(entry - buffer_px, 3)
                    # Only ever improve, and never put the stop through the market.
                    if direction == "buy":
                        if live_sl >= want - 1e-9 or want >= cur:
                            continue
                    else:
                        if (live_sl and live_sl <= want + 1e-9) or want <= cur:
                            continue
                    if await self.bridge.modify_position(
                            int(t), want, float(p["tp_price"] or 0)):
                        self.db.update_position_sl(int(t), want)
                        logger.info("[MONITOR] auto-breakeven ticket=%s -> %s (%s)",
                                    t, want, leg_why)
                        trades_log.info(
                            f"AUTO_BE ticket={t} signal={signal_id} sl={want} "
                            f"reason={leg_why}")
            except Exception as e:
                logger.error(f"[MONITOR] auto-breakeven error on {signal_id}: {e}",
                             exc_info=True)

        # Update per-channel equity / drawdown
        await self._update_drawdowns()

    # ── Runner trailing ───────────────────────────────────────────────────────

    async def _trail_runners(self, db_open, live_tickets):
        for pos in db_open:
            try:
                if float(pos["tp_price"] or 0.0) != 0.0:
                    continue                      # a normal TP leg, not a runner
                ticket = pos["ticket"]
                if not ticket or ticket not in live_tickets:
                    continue
                await self._trail_one(pos, live_tickets[ticket])
            except Exception as e:
                logger.error(f"[MONITOR] runner trail error on "
                             f"ticket={pos['ticket']}: {e}", exc_info=True)

    async def _trail_one(self, pos, mt5):
        sig = self.db.get_signal(pos["signal_id"])
        if not sig or not sig["direction"]:
            return
        direction = str(sig["direction"]).lower()

        # Has the rest of the ladder been reached yet?
        siblings = [p for p in self.db.get_positions_by_signal(pos["signal_id"])
                    if float(p["tp_price"] or 0.0) != 0.0]
        if not siblings:
            return                                # no ladder: nothing to wait for
        done = [p for p in siblings if p["status"] == "closed"]
        needed = RUNNER_TRAIL_AFTER_TPS or len(siblings)
        if len(done) < needed:
            return

        cur = float(mt5.get("current_price") or 0.0)
        if cur <= 0:
            return

        entry = float(pos["entry_price"] or 0.0)
        dist = RUNNER_TRAIL_DISTANCE
        if dist <= 0:
            orig_sl = float(sig["stop_loss"] or 0.0)
            dist = abs(entry - orig_sl) if (entry and orig_sl) else 0.0
        if dist <= 0:
            return

        ticket = int(pos["ticket"])

        # Hand it to the EA once, then let the 100 ms timer do the tracking.
        # Arming is idempotent on the EA side, but we track it here so a
        # healthy runner does not generate a bridge round trip every 5s.
        if ticket in self._armed:
            return
        if await self._arm_on_ea(ticket, dist):
            self._armed.add(ticket)
            logger.info(f"[MONITOR] runner ticket={ticket} handed to the EA "
                        f"trailing engine (hold {dist}, step {RUNNER_TRAIL_STEP})")
            trades_log.info(
                f"TRAIL_ARM ticket={ticket} signal={pos['signal_id']} "
                f"channel={pos['channel_id']} hold={dist} step={RUNNER_TRAIL_STEP}")
            return

        # Fallback for an EA older than v2.504, which has no trailing engine.
        # Coarse (one step per monitor cycle) but better than a runner that is
        # never managed at all.
        want = round(cur - dist, 3) if direction == "buy" else round(cur + dist, 3)
        live_sl = float(mt5.get("sl") or 0.0) or float(pos["stop_loss"] or 0.0)
        if direction == "buy":
            if want <= live_sl + RUNNER_TRAIL_STEP or want >= cur:
                return
        else:
            if want >= live_sl - RUNNER_TRAIL_STEP or want <= cur:
                return

        # tp=0 keeps the runner uncapped. Sending a real TP here would defeat
        # the entire purpose of the leg.
        if await self.bridge.modify_position(ticket, want, 0.0):
            self.db.update_position_sl(ticket, want)
            logger.info(f"[MONITOR] runner ticket={ticket} trailed in Python "
                        f"SL {live_sl} -> {want} (price {cur}, hold {dist}) "
                        f"— upgrade the EA to v2.504 for tick-rate trailing")
            trades_log.info(
                f"TRAIL ticket={ticket} signal={pos['signal_id']} "
                f"channel={pos['channel_id']} sl={want} price={cur} hold={dist}")

    async def _arm_on_ea(self, ticket: int, dist: float) -> bool:
        """True when the EA accepted the position for trailing."""
        if not self._ea_trailing:
            return False
        fn = getattr(self.bridge, "set_trailing", None)
        if fn is None:
            self._ea_trailing = False
            return False
        try:
            resp = await fn(ticket, dist, RUNNER_TRAIL_STEP)
        except Exception as e:
            logger.debug(f"[MONITOR] set_trailing failed: {e}")
            return False
        if resp and resp.get("status") == "success":
            return True
        err = (resp or {}).get("error", "")
        if "Unknown action" in str(err) or "unknown" in str(err).lower():
            # Old EA. Stop asking and use the Python fallback from here on.
            self._ea_trailing = False
            logger.warning("[MONITOR] EA does not support set_trailing "
                           "(needs v2.504+) — falling back to Python trailing")
        return False

    async def _handle_close(self, pos):
        ticket    = pos["ticket"]
        signal_id = pos["signal_id"]
        channel_id = pos["channel_id"]

        # Fetch deal history for P&L
        deal = await self.bridge.get_deal_history(ticket)
        pnl  = 0.0
        reason = "closed"

        if deal and deal.get("status") == "success":
            # The EA's field is total_profit. Reading only net_profit/profit,
            # neither of which it sent before v2.504, meant EVERY closed
            # position was recorded as pnl=0.00: the per-channel ledger never
            # moved and the scoreboard had nothing to rank. Accept whichever
            # the running EA provides, newest name first, and fall back to
            # summing the individual deals.
            for key in ("net_profit", "total_profit", "profit"):
                v = deal.get(key)
                if v is not None:
                    pnl = float(v)
                    break
            else:
                legs = deal.get("deals") or []
                if isinstance(legs, list) and legs:
                    pnl = sum(float(d.get("profit") or 0.0)
                              + float(d.get("commission") or 0.0)
                              + float(d.get("swap") or 0.0) for d in legs)
            reason = deal.get("exit_reason") or "closed"
        elif deal:
            logger.warning(f"[MONITOR] ticket={ticket} deal history unavailable "
                           f"({deal.get('error')}) — recording pnl=0.00")

        self._armed.discard(int(ticket))

        self.db.update_position_closed(ticket, pnl, reason)
        logger.info(f"[MONITOR] ticket={ticket} closed externally pnl={pnl:.2f} reason={reason}")
        trades_log.info(
            f"CLOSE ticket={ticket} signal={signal_id} channel={channel_id} "
            f"pnl={pnl:.2f} reason={reason}"
        )

        # Update channel system_balance with realized P&L
        sig = self.db.get_signal(signal_id)
        if sig and sig["channel_id"]:
            self.db.update_system_balance(sig["channel_id"], pnl)

        # Notify
        # This is the ONLY place a fill is announced. The executor used to send
        # a second "TP Hit" the moment the operator posted about it, so every
        # take profit was announced twice: once from the message and once from
        # the actual close. Only the close knows the real price and P&L.
        if self.notifier:
            sig = self.db.get_signal(signal_id)
            ch_name = sig["channel_name"] if sig else channel_id
            sign = "+" if pnl >= 0 else ""
            title = {
                "tp":      "🎯 <b>Take Profit</b>",
                "sl":      "🛑 <b>Stop Loss</b>",
                "manual":  "✋ <b>Closed manually</b>",
                "expert":  "🤖 <b>Closed by EA</b>",
                "stopout": "🚨 <b>Stop out</b>",
            }.get(str(reason).lower(),
                  "✅ <b>Position Closed</b>" if pnl >= 0 else "❌ <b>Position Closed</b>")
            try:
                await self.notifier.send(
                    f"{title} — {ch_name}\n"
                    f"Ticket: <code>{ticket}</code>\n"
                    f"P&L: <code>{sign}{pnl:.2f}</code>"
                )
            except Exception as e:
                logger.debug(f"[MONITOR] Notify error: {e}")

    async def _update_drawdowns(self):
        equity = await self.bridge.get_equity()
        if not equity:
            return

        today = datetime.utcnow().date().isoformat()

        for ch in self.config.channels:
            if not ch.enabled:
                continue

            today_stats = self.db.get_today_stats(ch.id)

            if today_stats and today_stats["date"] == today and \
               today_stats["starting_equity"]:
                # Use today's recorded starting equity
                start_eq = float(today_stats["starting_equity"])
            else:
                # First record of the day OR new day — initialise
                # If we have a system_balance, prefer that (more reliable than
                # raw equity which may include floating P&L).
                sys_rec = self.db.get_system_balance(ch.id)
                if sys_rec and ch.starting_balance > 0:
                    start_eq = float(sys_rec["system_balance"])
                else:
                    start_eq = float(equity)
                logger.info(
                    f"[MONITOR] Initialising starting_equity for {ch.name} "
                    f"on {today}: ${start_eq:.2f}"
                )

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