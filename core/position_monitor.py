"""
core/position_monitor.py
========================
Monitors all open MT5 positions.
- Detects externally closed positions (SL/TP hit, manual close)
- Updates live P&L and MFE/MAE in DB
- Triggers per-channel drawdown checks
"""

import asyncio
import json
import logging
import os
import types
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
# How far a LADDERLESS runner (a bare "gold buy now", which has a stop and no
# target) must travel in its own favour before the trailing engine takes over,
# measured in R - multiples of its own stop distance. 1.0 = one R.
# A ladder runner ignores this and waits for its siblings to close instead.
RUNNER_TRAIL_ARM_R     = float(os.getenv("RUNNER_TRAIL_ARM_R", "1.0"))

# How many cycles to keep asking for a deal history before giving up and
# booking the position at its last known floating P&L.
DEAL_RETRY_MAX_DEFAULT = 5
# How many consecutive EMPTY positions-pool replies before believing them.
EMPTY_POOL_CONFIRMATIONS = 3


class PositionMonitor:
    DEAL_RETRY_MAX = DEAL_RETRY_MAX_DEFAULT

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
        # Same idea for the ORDERS pool query. Cleared after two consecutive
        # failures so a terminal that never answers it is asked once, not on
        # every 5-second cycle for the rest of the session.
        self._ea_orders = True
        self._orders_fails = 0
        # Tickets whose deal history has not come back yet, and how many times
        # we have asked. Booking a close with no P&L is worse than waiting.
        self._deal_retry: dict[int, int] = {}
        # Consecutive cycles where the positions pool came back EMPTY. An
        # empty pool is normal when flat and catastrophic when it is a lie,
        # so it takes corroboration before anything is closed on it.
        self._empty_streak = 0
        # Orphan rows already reported, so the warning fires once each.
        self._orphans_seen: set[int] = set()
        # Tickets that have already had their profit slice taken.
        self._partial_taken: set[int] = set()

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

        # An EMPTY pool is not the same as None, and it used to be trusted
        # outright: every DB-open position was routed to _handle_close in a
        # single cycle. A terminal reconnect, an account switch, or one moment
        # where PositionsTotal() reads 0 was enough to write every live trade
        # off as closed, leaving them open at the broker with nothing tracking
        # them.
        #
        # The narrow fix is not to stop closing - it is to stop closing on the
        # pool's word alone. When the pool says empty while we believe we hold
        # positions, every ticket must be confirmed individually against its
        # deal history before it is booked. That path already exists for the
        # case where the orders pool is unavailable; this reuses it.
        mass_close_suspect = False
        if not live_tickets:
            _held = [r for r in self.db.get_all_open_positions() if r["ticket"]]
            if _held:
                self._empty_streak += 1
                mass_close_suspect = True
                logger.warning(
                    "[MONITOR] positions pool came back EMPTY while %d position(s) "
                    "are open here (streak %d) — confirming each ticket against "
                    "its deal history before booking anything closed",
                    len(_held), self._empty_streak)
        else:
            self._empty_streak = 0

        # Rows that never got a ticket are orphans: the bridge timed out after
        # the EA had already taken the command, so the order may well be live
        # at the broker under a ticket we never learned. They are invisible to
        # every loop below. Surface them once rather than leaving them silent.
        await self._report_orphans()

        # A PENDING order is not a position. MT5 keeps unfilled limit and stop
        # orders in the orders pool, and get_all_positions() does not return
        # them. Without this second query every limit order we placed vanished
        # from live_tickets on the very next tick and was written off as
        # "closed externally pnl=0.00" one second after it was placed.
        #
        # 2026-08-24 is the whole story: nine of the day's fifteen signals were
        # limit orders, every one of them was marked closed within 1-3 seconds
        # with pnl 0, and the orders themselves were left sitting in the
        # terminal with nothing tracking them. It also produced the duplicate
        # the operator noticed: with the first order forgotten, a repost of the
        # same setup 49 minutes later looked new and placed a second one.
        pending_tickets = await self._pending_tickets()

        # Get all positions we think are open
        db_open = self.db.get_all_open_positions()

        for pos in db_open:
          # Per-item isolation. Every other loop in this file has it; this one
          # did not, so one bad payload aborted the cycle and took the close
          # detection for every position after it with it.
          try:
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
                continue

            # Gone from the positions pool. Pending, or genuinely closed?
            if pending_tickets is not None and not mass_close_suspect:
                if int(ticket) in pending_tickets:
                    continue            # waiting for price to come to it
                await self._handle_close(pos)
                continue
            if pending_tickets is not None and int(ticket) in pending_tickets:
                continue                # still resting, whatever the pool said

            # No orders pool to consult. Ask about THIS ticket instead of
            # giving up on the whole cycle: a position that filled and closed
            # has closing deals, an order that never filled has none. One
            # round trip, and only for a ticket that has actually vanished.
            deal = await self._closing_deals(int(ticket))
            if deal is not None:
                await self._handle_close(pos, deal=deal)
          except Exception as e:
            logger.error("[MONITOR] position pass failed for ticket=%s: %s",
                         pos["ticket"], e, exc_info=True)

        # Trail any runner whose sibling take-profit legs are done
        if RUNNER_TRAIL_ENABLED:
            await self._trail_runners(db_open, live_tickets)

        # Zone-ladder housekeeping and per-channel breakeven policy
        await self._manage_ladders(db_open, live_tickets)
        await self._expire_stale_pendings(db_open)
        await self._take_partial_profit(db_open, live_tickets)
        await self._auto_breakeven(db_open, live_tickets)

        # The risk breaker used to be the last line of _auto_breakeven, which
        # meant every halt in the system depended on this whole cycle running
        # to completion - and the position loop above is the one loop here
        # without per-item exception isolation. A single malformed field from
        # the EA skipped the drawdown evaluation entirely, on exactly the kind
        # of unstable day when it is most needed. It gets its own try now.
        try:
            await self._update_drawdowns()
        except Exception as e:
            logger.error("[MONITOR] drawdown evaluation failed: %s", e, exc_info=True)

    async def _report_orphans(self):
        """Warn about position rows that never received a ticket.

        _send_command times out after 10s for an order, but the EA already has
        the command file by then: the order gets placed and Python never learns
        the ticket. The row stays status='pending' with ticket NULL, so nothing
        in this monitor can see it - no P&L, no close detection, no drawdown
        contribution. It has to be reconciled by hand in the terminal, so the
        least we can do is say it exists, once, rather than never.
        """
        try:
            rows = self.db.get_orphan_positions()
        except Exception:
            return
        new = [r for r in rows if int(r["id"]) not in self._orphans_seen]
        if not new:
            return
        for r in new:
            self._orphans_seen.add(int(r["id"]))
        logger.error("[MONITOR] %d position row(s) have no ticket — the order "
                     "may be live at the broker and is not being tracked here: %s",
                     len(new), ", ".join(str(r["signal_id"]) for r in new))
        if self.notifier:
            try:
                await self.notifier.send(
                    f"⚠️ <b>Untracked order(s)</b>\n"
                    f"{len(new)} leg(s) were sent but no ticket came back, so the "
                    f"bridge timed out after the EA had the command. They may be "
                    f"LIVE and are not being monitored.\n"
                    + "\n".join(f"<code>{r['signal_id']}</code>" for r in new[:6])
                    + "\nCheck the terminal.")
            except Exception:
                pass

    async def _pending_tickets(self):
        """Tickets sitting unfilled in MT5's ORDERS pool, or None if unknowable.

        Stops asking after two consecutive failures. On 2026-08-25 the EA on
        the chart never answered get_all_orders and the call timed out after a
        full 30 seconds, 777 times in one session: about six and a half hours
        of wall clock, and it stretched the 5-second monitor cycle to ~36
        seconds, which delays every stop move and close detection behind it.
        A capability the terminal does not have must be asked about once, not
        once per cycle forever.
        """
        if not self._ea_orders:
            return None
        try:
            pending = await self.bridge.get_all_orders()
        except Exception as e:
            logger.debug("[MONITOR] get_all_orders raised: %s", e)
            pending = None
        if pending is None:
            self._orders_fails += 1
            if self._orders_fails >= 2:
                self._ea_orders = False
                logger.error(
                    "[MONITOR] get_all_orders has failed %d times — the EA on "
                    "the chart does not answer it. Falling back to a per-ticket "
                    "deal-history check, which is correct but slower per closed "
                    "position. RECOMPILE PythonFileBridge.mq5 (v2.400+ "
                    "implements get_all_orders) and restart to restore the "
                    "bulk query.", self._orders_fails)
            return None
        self._orders_fails = 0
        return {int(o["ticket"]) for o in pending if o.get("ticket")}

    async def _closing_deals(self, ticket: int):
        """The deal history for `ticket`, or None if it has not closed.

        The EA answers "No closing deals found" as an error, and the bridge
        turns that into None, so None means one of two things: the order never
        filled, or the history is not written yet. Both say "do not close it",
        and the next cycle asks again. Erring this way leaves a closed position
        marked open for a few seconds; erring the other way writes an
        irreversible close into the ledger for an order that is still live.
        """
        try:
            deal = await self.bridge.get_deal_history(ticket)
        except Exception as e:
            logger.debug("[MONITOR] get_deal_history(%s) raised: %s", ticket, e)
            return None
        if not deal or deal.get("status") != "success":
            return None
        return deal

    # ── Zone ladder: cancel the legs that price left behind ───────────────────

    # A resting order that has outlived its idea, in minutes. The EA enforces
    # this too (PendingMaxAgeMinutes) and the broker enforces it a third time
    # when it accepts an expiry; this is the layer that also writes the row off
    # in our own database, which neither of the others can do.
    PENDING_MAX_AGE_MIN_DEFAULT = 300.0

    # Which target, once reached, retires an unfilled entry. 1 = TP1, 2 = TP2.
    # Per channel: parser.cancel_pending_after_tp.
    CANCEL_AFTER_TP_DEFAULT = 1

    async def _expire_stale_pendings(self, db_open):
        """Retire resting orders whose trade has already happened without them.

        THE PRIMARY RULE IS NOT AGE, IT IS THE TARGET.

        If the market reaches the signal's own TP1 while our entry is still
        sitting unfilled, that trade is over. The operator was right, the move
        happened, and we were not in it. What is left on the book is not an
        opportunity: it is an order armed to buy the top of a move that has
        already completed, on the pullback, with a stop sized for an entry the
        market rejected. That is the configuration that kills accounts, and it
        is exactly what left five 4-September sell limits at 4478-4495 armed
        with gold near 4400.

        Age is kept as a secondary backstop for the signal whose target is
        never reached in either direction and simply goes quiet.

        THE EXEMPTION. Some operators run their own resting orders and say
        "cancel the 4436 one" when they want it gone. Deleting those from
        under them takes the trade away seconds before they ask for it.
        parser.operator_manages_limits=true exempts a channel from both rules
        here, and marks the broker comment so the EA's own sweep skips it too.

        SAMPLING. Price is read once per monitor cycle, so a spike that touches
        TP1 and fully retraces within five seconds is not seen. That is a
        missed cancellation, never a wrong one.

        WHY THIS EXISTS SEPARATELY FROM _manage_ladders. That routine only
        cancels a leg when a SIBLING of the same signal has already FILLED and
        price then ran past it:

            filled = [p for p in legs if p["ticket"] in live_tickets]
            if not filled: continue

        So the case it cannot see is the one that actually happened. Nothing in
        those 4-September ladders ever filled, so `filled` was empty on every
        cycle and not one of them was ever a candidate for cancellation. It
        also covers the seventeen channels that never set ladder_cancel_pips
        at all, for which there was no cleanup whatsoever.
        """
        pend = await self._pending_tickets()
        if pend is None:
            return                       # no orders pool this cycle: say nothing
        now = datetime.utcnow()
        by_signal = {}
        for pos in db_open:
            if pos["ticket"] and int(pos["ticket"]) in pend:
                by_signal.setdefault(pos["signal_id"], []).append(pos)

        price_cache = {}
        for signal_id, legs in by_signal.items():
            try:
                sig = self.db.get_signal(signal_id)
                if not sig:
                    continue
                p = (self._channel_for(sig["channel_id"]) or
                     types.SimpleNamespace(parser={}))
                p = getattr(p, "parser", None) or {}
                if bool(p.get("operator_manages_limits", False)):
                    continue            # his orders, his cancellations

                direction = str(sig["direction"] or "").lower()
                tps = []
                try:
                    tps = [float(x) for x in json.loads(sig["take_profits"] or "[]")]
                except Exception:
                    tps = []
                after_tp = int(p.get("cancel_pending_after_tp",
                                     self.CANCEL_AFTER_TP_DEFAULT) or 0)
                max_age = float(p.get("pending_max_age_min",
                                      self.PENDING_MAX_AGE_MIN_DEFAULT) or 0)

                # Has the trade already played out without us?
                tp_hit, tp_level = False, None
                if after_tp > 0 and len(tps) >= after_tp and direction:
                    tp_level = tps[after_tp - 1]
                    sym = sig["symbol"] or ""
                    if sym not in price_cache:
                        price_cache[sym] = await self.bridge.get_price(sym, direction)
                    cur = price_cache[sym]
                    if cur:
                        tp_hit = (cur >= tp_level if direction == "buy"
                                  else cur <= tp_level)

                for pos in legs:
                    t = int(pos["ticket"])
                    age_min = None
                    placed = pos["opened_at"]
                    if placed:
                        try:
                            age_min = (now - datetime.fromisoformat(
                                str(placed).replace("Z", ""))).total_seconds() / 60.0
                        except Exception:
                            age_min = None

                    if tp_hit:
                        why = (f"TP{after_tp} {tp_level:g} was reached while this "
                               f"entry never filled")
                        reason = "tp_reached_unfilled"
                    elif (max_age > 0 and age_min is not None
                          and age_min >= max_age):
                        why = f"{age_min:.0f} min old, limit {max_age:.0f}"
                        reason = "expired_pending"
                    else:
                        continue

                    if not await self.bridge.cancel_order(t):
                        logger.warning("[MONITOR] could not cancel pending "
                                       "ticket=%s (%s)", t, why)
                        continue
                    self.db.update_position_closed(t, 0.0, reason)
                    logger.info("[MONITOR] %s: cancelled pending ticket=%s at "
                                "%.2f — %s", sig["channel_name"], t,
                                float(pos["entry_price"] or 0), why)
                    trades_log.info(
                        f"CANCEL_UNFILLED channel={sig['channel_id']} ticket={t} "
                        f"signal={signal_id} reason={reason} why=\"{why}\"")
                    if self.notifier:
                        try:
                            await self.notifier.send(
                                f"\U0001f9f9 <b>Unfilled order removed</b> — "
                                f"{sig['channel_name']}\n"
                                f"{direction.upper()} at "
                                f"<code>{pos['entry_price']}</code> never filled.\n"
                                f"{why}.\n"
                                + ("The move already happened without us; leaving "
                                   "it armed would buy the pullback into a finished "
                                   "trade." if tp_hit else
                                   "Retired before a reversal could fill it."))
                        except Exception:
                            pass
            except Exception as e:
                logger.error("[MONITOR] unfilled-order sweep failed for %s: %s",
                             signal_id, e, exc_info=True)

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

                # Same query, same off-switch. Calling the bridge directly here
                # meant this path kept paying the 30-second timeout even after
                # _cycle had given up on it.
                pend = await self._pending_tickets()
                if pend is None:
                    continue
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

    async def _take_partial_profit(self, db_open, live_tickets):
        """Bank part of a leg once it is far enough in its own favour.

        THE EVIDENCE. In the week of 31 Aug, 84% of losing stop-outs had been
        in profit first: 122 of 375 reached +0.50R and still ran to a full
        stop. The median losing trade peaked at 0.30R. The breakeven backstop
        was configured at 1.5R, which only 17 losing trades all week ever
        reached - it was parked where the money never went.

        WHY NOT JUST LOWER THE BREAKEVEN. Simulated, arming breakeven at 0.5R
        lands somewhere between -$1,060 and +$942, because a stop moved to
        entry also kills winners that dip back through it on the way up, and
        MFE/MAE carry no time ordering so the two bounds cannot be narrowed.

        WHY THIS INSTEAD. Taking part of the position off has no such branch:
        the stop does not move, so the remainder still runs to whatever it
        would have reached anyway. Simulated at 50% taken at 0.40R the week
        improves by about $470. That figure assumes a fill at the peak, so
        treat it as a ceiling - but the sign is not in question.

        Off by default. partial_profit.at_r = 0 disables it per channel.
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
                pp = (getattr(ch, "partial_profit", None) if ch else None) or {}
                at_r = float(pp.get("at_r", 0) or 0)
                frac = float(pp.get("fraction", 0.5) or 0.5)
                if at_r <= 0 or frac <= 0:
                    continue
                direction = str(sig["direction"]).lower()
                orig_sl = float(sig["stop_loss"] or 0)
                min_lot = float(os.getenv("MIN_LOT", "0.01"))

                for pos in legs:
                    t = pos["ticket"]
                    if not t or int(t) not in live_tickets:
                        continue
                    if int(t) in self._partial_taken:
                        continue
                    # _partial_taken is in-memory, so a restart would let this
                    # cut the same leg a second time. A non-zero booked_pnl on
                    # the row is the durable record that a slice already went.
                    if self.db.get_booked_pnl(int(t)):
                        self._partial_taken.add(int(t))
                        continue
                    entry = float(pos["entry_price"] or 0)
                    if entry <= 0 or orig_sl <= 0:
                        continue
                    risk = abs(entry - orig_sl)
                    if risk <= 0:
                        continue
                    cur = float(live_tickets[int(t)].get("current_price") or 0)
                    if cur <= 0:
                        continue
                    moved = (cur - entry) if direction == "buy" else (entry - cur)
                    if moved < at_r * risk:
                        continue
                    lot = float(pos["lot_size"] or 0)
                    cut = round(lot * frac, 2)
                    # Below the broker minimum a partial is not possible.
                    # Leave the leg whole rather than closing it outright -
                    # this routine exists to bank some, never to exit.
                    if cut < min_lot or (lot - cut) < min_lot:
                        self._partial_taken.add(int(t))
                        continue
                    if not await self.bridge.close_position(int(t), lot=cut):
                        continue
                    self._partial_taken.add(int(t))
                    self.db.update_position_lot(int(t), round(lot - cut, 2))
                    banked = 0.0
                    try:
                        d = await self.bridge.get_deal_history(int(t))
                        if d and d.get("status") == "success":
                            for k in ("net_profit", "total_profit", "profit"):
                                v = d.get(k)
                                if v is not None:
                                    banked = float(v); break
                    except Exception:
                        pass
                    if banked:
                        # Running total for the whole ticket, not this slice.
                        # book_position_cumulative applies only the increment,
                        # so the final close cannot bank this money again.
                        banked = self.db.book_position_cumulative(
                            int(t), sig["channel_id"], banked, "partial profit")
                    logger.info("[MONITOR] partial profit ticket=%s cut=%.2f of "
                                "%.2f at %.2fR (banked %+.2f)",
                                t, cut, lot, moved / risk, banked)
                    trades_log.info(
                        f"PARTIAL_PROFIT ticket={t} signal={signal_id} "
                        f"channel={sig['channel_id']} cut={cut}/{lot} "
                        f"r={moved / risk:.2f} pnl={banked:.2f}")
                    if self.notifier:
                        try:
                            await self.notifier.send(
                                f"💰 <b>Partial profit</b> — {sig['channel_name']}\n"
                                f"{sig['symbol']} {direction.upper()} at "
                                f"<code>{moved / risk:.2f}R</code>\n"
                                f"Took <b>{frac:.0%}</b> "
                                f"(<code>{cut:.2f}</code> of <code>{lot:.2f}</code>)"
                                + (f", banked <code>{banked:+.2f}</code>" if banked else "")
                                + "\nThe stop has not moved; the rest runs.")
                        except Exception:
                            pass
            except Exception as e:
                logger.error("[MONITOR] partial-profit error on %s: %s",
                             signal_id, e, exc_info=True)

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
                # Same cushion the operator-message path uses. Without it the
                # two routes to breakeven put the stop in different places on
                # the same channel, which is impossible to reason about.
                slack_px = float(be.get("slack_pips", 0) or 0) * pip_value
                direction = str(sig["direction"]).lower()

                armed = False
                why = ""
                if tp_idx > 0:
                    tps_done = [p for p in self.db.get_positions_by_signal(signal_id)
                                if p["status"] == "closed"
                                and str(p["close_reason"]).lower() == "tp"]
                    # "Breakeven after TP2" means TP2 is BANKED. The old test
                    # was `any leg with index <= 2 closed at tp`, which armed
                    # after TP1 and then logged "TP2 reached", so the setting
                    # did the wrong thing and said the wrong thing about it.
                    #
                    # Two ways to be satisfied, because a ladder can be trimmed
                    # before it opens: _tp_passed skips a target the market has
                    # already gone through, and the min_lot floor drops the far
                    # legs. Either the leg numbered tp_idx closed at target, or
                    # tp_idx legs have closed at target between them.
                    hit_that_leg = any((p["tp_index"] or 0) == tp_idx
                                       for p in tps_done)
                    if hit_that_leg:
                        armed, why = True, f"TP{tp_idx} closed at target"
                    elif len(tps_done) >= tp_idx:
                        armed, why = True, (f"{len(tps_done)} take profit(s) "
                                            f"banked (ladder was trimmed, so "
                                            f"leg {tp_idx} never opened)")

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

                    # buffer_px moves the stop INTO profit (aggressive);
                    # slack_px moves it the other way, leaving room for the
                    # noise that otherwise takes an exactly-at-entry stop.
                    # They are opposite by design and both may be set.
                    off = buffer_px - slack_px
                    want = round(entry + off, 3) if direction == "buy" \
                        else round(entry - off, 3)
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

        # Has the rest of the ladder been reached yet?
        siblings = [p for p in self.db.get_positions_by_signal(pos["signal_id"])
                    if float(p["tp_price"] or 0.0) != 0.0]
        if siblings:
            done = [p for p in siblings if p["status"] == "closed"]
            needed = RUNNER_TRAIL_AFTER_TPS or len(siblings)
            if len(done) < needed:
                return
        else:
            # NO LADDER. A bare "gold sell now" opens one leg with a protective
            # stop and no target at all, so there is nothing to wait for and
            # this used to return here - meaning a bare trade was never trailed
            # and never managed by anything except the timed scale-out.
            #
            # Arm on distance instead: once the trade is RUNNER_TRAIL_ARM_R
            # times its own stop distance in profit (1.0 = one R), hand it to
            # the EA's trailing engine. That is strictly better than parking a
            # fixed take profit at 1R, which would cap every bare trade at
            # exactly 1R and close the ones that were about to run.
            moved = (cur - entry) if direction == "buy" else (entry - cur)
            if moved < RUNNER_TRAIL_ARM_R * dist:
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

    @staticmethod
    def _broker_close_time(deal) -> "str | None":
        """When the BROKER closed it, as an ISO string, or None.

        get_channel_day_pnl buckets on positions.closed_at, so a stop hit at
        23:59:58 and detected at 00:00:03 landed in the next day - after the
        rollover had already un-halted the channel, so yesterday's breaker
        never saw the loss that should have tripped it.

        The EA sends this as `exit_time` on each element of deals[], a unix
        timestamp in the SERVER's timezone. A close_time key was never emitted
        at any version, so reading one returned None every time.
        """
        try:
            legs = deal.get("deals") or []
            stamps = [int(d.get("exit_time") or 0) for d in legs
                      if isinstance(d, dict)]
            stamps = [s for s in stamps if s > 0]
            if not stamps:
                return None
            # A plausibility gate. A server clock or a field the EA fills
            # differently later must not be able to write a 1970 or a 2087
            # timestamp into the column the drawdown breaker buckets on.
            newest = max(stamps)
            now = datetime.utcnow().timestamp()
            if not (now - 30 * 86400 < newest < now + 2 * 86400):
                return None
            return datetime.utcfromtimestamp(newest).isoformat()
        except Exception:
            return None

    async def _handle_close(self, pos, deal=None):
        ticket    = pos["ticket"]
        signal_id = pos["signal_id"]
        channel_id = pos["channel_id"]

        # Fetch deal history for P&L. The caller may already have it: the
        # fallback path in _cycle fetches it to decide whether the position
        # closed at all, and re-asking would double the round trips on the
        # very path taken when the bridge is already struggling.
        if deal is None:
            deal = await self.bridge.get_deal_history(ticket)
        pnl  = 0.0
        reason = "closed"
        closed_at = None
        # True when `pnl` came from the deal history, which reports the
        # ticket's RUNNING TOTAL. False when it is the last floating value of
        # whatever was left. The two are booked differently.
        deal_backed = False

        if deal and deal.get("status") == "success":
            deal_backed = True
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
            # A trailing stop that closes in PROFIT is not a stop-out. The EA
            # reports both as "sl", so 22 profitable exits worth +$416.71 were
            # recorded as losses in the week of 31 Aug, which understated every
            # win rate the system reports - including the nightly scorecard.
            if str(reason).lower() == "sl" and pnl > 0:
                reason = "trail"
            # The EA reports when the broker closed it. Bucketing on our own
            # detection time put a 23:59:58 stop into the next day, after the
            # rollover had already un-halted the channel, so yesterday's
            # breaker never saw the loss that should have tripped it.
            # The EA does NOT emit close_time. It emits exit_time, a unix
            # timestamp, on each entry of the deals[] array - so reading
            # close_time silently returned None on every close and this fell
            # back to our own detection time, which is the bug it was written
            # to fix. Take the last closing deal's exit_time.
            closed_at = self._broker_close_time(deal)
        else:
            # get_deal_history returns None on EVERY failure - timeout,
            # unparseable reply, write error - so the old `elif deal:` branch
            # here was unreachable and this landed silently on pnl=0.00.
            #
            # That was not merely a missing write. _cycle writes the LIVE
            # floating P&L into positions.pnl every 5 seconds, so the row
            # already held the right number and update_position_closed
            # OVERWROTE it with zero: the measured drawdown improved at the
            # exact moment a channel realised a loss. 53 legs went through
            # here in the week of 31 Aug, 17 of them market orders that had
            # certainly filled, 11 in one burst when the bridge recovered.
            #
            # Keep the last known floating value, mark the row for retry
            # instead of closing it, and say so out loud.
            self._deal_retry[int(ticket)] = self._deal_retry.get(int(ticket), 0) + 1
            tries = self._deal_retry[int(ticket)]
            if tries <= self.DEAL_RETRY_MAX:
                logger.warning(
                    "[MONITOR] ticket=%s deal history unavailable (attempt %d/%d) "
                    "- leaving the position open and retrying next cycle rather "
                    "than booking it at zero", ticket, tries, self.DEAL_RETRY_MAX)
                return
            # Out of retries. Close it, but keep the last floating P&L rather
            # than zero, and make the uncertainty visible in the reason.
            pnl = float(pos["pnl"] or 0.0)
            reason = "closed_unconfirmed"
            self._deal_retry.pop(int(ticket), None)
            logger.error(
                "[MONITOR] ticket=%s deal history never arrived after %d tries "
                "- booking the last known floating P&L %.2f and flagging it",
                ticket, self.DEAL_RETRY_MAX, pnl)
            if self.notifier:
                try:
                    await self.notifier.send(
                        f"⚠️ <b>Unconfirmed close</b> — ticket <code>{ticket}</code>\n"
                        f"The broker never returned its deal history. Booked at the "
                        f"last seen floating P&L <code>{pnl:+.2f}</code>, which may be "
                        f"wrong. Check this trade in the terminal.")
                except Exception:
                    pass
        self._deal_retry.pop(int(ticket), None)

        self._armed.discard(int(ticket))

        # Conditional on the row not already being closed. bare_trade_watcher
        # closes and books positions on its own timer while this cycle is
        # part-way through the round trips it started with a snapshot of the
        # open rows, so without this gate the same close is booked twice.
        if not self.db.update_position_closed(ticket, pnl, reason,
                                              closed_at=closed_at):
            logger.info("[MONITOR] ticket=%s was closed by another path before "
                        "this cycle reached it - not booking it again", ticket)
            return
        logger.info(f"[MONITOR] ticket={ticket} closed externally pnl={pnl:.2f} reason={reason}")
        trades_log.info(
            f"CLOSE ticket={ticket} signal={signal_id} channel={channel_id} "
            f"pnl={pnl:.2f} reason={reason}"
        )

        # Update channel system_balance with realized P&L.
        # `pnl` from the deal history is the ticket's RUNNING TOTAL, so if a
        # slice was already banked by _take_partial_profit or close_partial,
        # only the remainder belongs in the ledger now.
        sig = self.db.get_signal(signal_id)
        if sig and sig["channel_id"]:
            if deal_backed:
                self.db.book_position_cumulative(int(ticket), sig["channel_id"],
                                                 pnl, reason)
            else:
                # No deal history: `pnl` is the last floating value of what was
                # LEFT of the position, not a running total. Book it as its own
                # increment and record that it went to the ledger.
                self.db.book_realised(sig["channel_id"], pnl, reason)
                try:
                    self.db.mark_pnl_booked(int(ticket), pnl)
                except Exception:
                    pass

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
                "trail":   "📈 <b>Trailing stop — locked in</b>",
                "closed_unconfirmed": "⚠️ <b>Closed, P&L unconfirmed</b>",
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
        """Per-channel drawdown, measured against each channel's own book.

        THE BUG THIS REPLACES (live from the first commit to 2026-08-30):

            dd = (start_eq - equity) / start_eq * 100

        `start_eq` was the channel's notional book, $1000. `equity` was the
        WHOLE ACCOUNT's live equity. With a $9,794 account that evaluates to
        (1000 - 9794)/1000 = -879% for every channel on every cycle. It is
        negative and stays negative, so `dd >= ch.drawdown_pct` was never true
        and not one halt fired in the system's entire log history. Worse, the
        expression flips sign if account equity ever drops below the notional:
        below $700 EVERY channel halts at once, profitable ones included,
        because they all read the same account number.

        The replacement compares like with like: a channel's own realised and
        floating P&L today, against its own book at the start of the day.

            dd = -(realised_today + floating_now) / day_start_book * 100

        Nothing here reads account equity, so 28 channels sharing one MT5
        account no longer contaminate each other's numbers.

        The account-level guard is separate and deliberately so. Per-channel
        limits cannot bound total exposure: 28 channels each allowed a 30% loss
        on a $1000 book is $8,400 of permitted loss on a $9,794 account. The
        account guard is the only thing that caps the sum.
        """
        # No config means no per-channel drawdown to track. Returning here
        # rather than raising matters because this runs inside _cycle: an
        # AttributeError thrown from the drawdown pass aborts the whole cycle,
        # so nothing gets its P&L updated and nothing gets closed either.
        if not self.config or not getattr(self.config, "channels", None):
            return
        equity = await self.bridge.get_equity()
        if not equity:
            return

        today = datetime.utcnow().date().isoformat()

        # A halt is a decision about TODAY. Roll it off at the date change, or
        # a channel stopped on Monday stays stopped all week and silently stops
        # being measured. Previously `halted` only cleared on a process restart.
        if getattr(self, "_halt_day", None) != today:
            for ch in self.config.channels:
                # A CUMULATIVE halt is not about today, so midnight does not
                # clear it. Only the daily breaker rolls over.
                try:
                    _led = self.db.get_system_balance(ch.id)
                    if _led and float(_led["starting_balance"]) > 0:
                        _s, _n = float(_led["starting_balance"]), float(_led["system_balance"])
                        if (_s - _n) / _s * 100.0 >= float(ch.drawdown_pct):
                            logger.info("[MONITOR] New UTC day — %s stays halted "
                                        "on cumulative drawdown", ch.name)
                            continue
                except Exception:
                    pass
                if getattr(ch, "halted", False):
                    logger.info("[MONITOR] New UTC day — un-halting %s",
                                getattr(ch, "name", ch))
                try:
                    ch.halted = False
                except Exception:
                    pass
            self._halt_day = today
            self._account_halted = False

        day_total = 0.0

        # The account guard's denominator is whole-account equity, so its
        # numerator has to be whole-account P&L. Accumulating only over ENABLED
        # channels meant a channel disabled mid-week while still holding open
        # trades contributed nothing, and the only ceiling on total exposure
        # under-read by exactly that much. Count every channel's money; only
        # the halting below is gated on enabled.
        for _ch in self.config.channels:
            if _ch.enabled:
                continue
            try:
                _r, _f = self.db.get_channel_day_pnl(_ch.id, today)
                if _r or _f:
                    day_total += _r + _f
                    logger.info("[MONITOR] %s is disabled but still holds "
                                "%+.2f today — counted toward the account guard",
                                _ch.name, _r + _f)
            except Exception:
                pass

        for ch in self.config.channels:
            if not ch.enabled:
                continue

            # The channel's book at the start of the day. channel_stats keeps
            # it once set, so intraday P&L cannot move the denominator.
            today_stats = self.db.get_today_stats(ch.id)
            if today_stats and today_stats["date"] == today and \
               today_stats["starting_equity"]:
                start_book = float(today_stats["starting_equity"])
            else:
                sys_rec = self.db.get_system_balance(ch.id)
                if sys_rec:
                    start_book = float(sys_rec["system_balance"])
                elif ch.starting_balance > 0:
                    start_book = float(ch.starting_balance)
                else:
                    # starting_balance 0 means "size off live equity", so the
                    # account IS this channel's book and equity is the right
                    # denominator. That is the one case the old code got right.
                    start_book = float(equity)
                logger.info(
                    f"[MONITOR] Day-start book for {ch.name} on {today}: "
                    f"${start_book:.2f}"
                )

            realised, floating = self.db.get_channel_day_pnl(ch.id, today)
            day_pnl = realised + floating
            day_total += day_pnl

            # Record the channel's own book, not account equity, so the
            # dashboard and the daily report stop showing 28 identical numbers.
            self.db.upsert_channel_equity(ch.id, start_book + day_pnl, start_book)

            if start_book <= 0:
                continue

            dd = max(0.0, -day_pnl / start_book * 100.0)
            ch.current_drawdown = dd

            # CUMULATIVE, not just today. The daily measure above is a correct
            # daily breaker and a useless cumulative one: a channel losing 6% a
            # day never comes near a 30% DAILY limit while ending the week down
            # a third. FX4Team did exactly that in the week of 31 Aug - worst
            # single day -25.9%, inside the limit every day, week -34.4%.
            #
            # The ledger already holds the number. Same limit, measured against
            # the channel's ORIGINAL book rather than this morning's.
            try:
                _led = self.db.get_system_balance(ch.id)
                if _led and float(_led["starting_balance"]) > 0:
                    _start = float(_led["starting_balance"])
                    _now = float(_led["system_balance"])
                    cum_dd = max(0.0, (_start - _now) / _start * 100.0)
                    if cum_dd >= ch.drawdown_pct and not ch.halted:
                        ch.halted = True
                        ch.current_drawdown = cum_dd
                        logger.warning(
                            "[MONITOR] Channel %s HALTED — CUMULATIVE drawdown "
                            "%.1f%% >= limit %.1f%% (book $%.2f of $%.2f)",
                            ch.name, cum_dd, ch.drawdown_pct, _now, _start)
                        if self.notifier:
                            try:
                                await self.notifier.send(
                                    f"🚨 <b>Channel Halted: {ch.name}</b>\n"
                                    f"<b>Cumulative</b> drawdown "
                                    f"<code>{cum_dd:.1f}%</code> "
                                    f"(limit {ch.drawdown_pct}%)\n"
                                    f"Book <code>${_now:.2f}</code> of "
                                    f"<code>${_start:.2f}</code>\n"
                                    f"This is a slow bleed, not one bad day. It "
                                    f"will not un-halt at midnight — reset the "
                                    f"channel's balance to restart it.")
                            except Exception:
                                pass
                        continue
            except Exception as e:
                logger.error("[MONITOR] cumulative drawdown check failed for "
                             "%s: %s", ch.name, e)

            if dd >= ch.drawdown_pct and not ch.halted:
                ch.halted = True
                logger.warning(
                    f"[MONITOR] Channel {ch.name} HALTED — drawdown {dd:.1f}% "
                    f">= limit {ch.drawdown_pct}% "
                    f"(realised {realised:+.2f}, floating {floating:+.2f}, "
                    f"book ${start_book:.2f})"
                )
                if self.notifier:
                    try:
                        await self.notifier.send(
                            f"🚨 <b>Channel Halted: {ch.name}</b>\n"
                            f"Drawdown: <code>{dd:.1f}%</code> "
                            f"(limit: {ch.drawdown_pct}%)\n"
                            f"Realised <code>{realised:+.2f}</code>  "
                            f"Floating <code>{floating:+.2f}</code>  "
                            f"on a <code>${start_book:.2f}</code> book\n"
                            f"No new signals from this channel until "
                            f"{today} rolls over."
                        )
                    except Exception:
                        pass

        await self._check_account_guard(day_total, float(equity))

    async def _check_account_guard(self, day_total: float, equity: float):
        """Account-level kill switch. Halts EVERY channel, not just one.

        Per-channel limits bound each channel in isolation and therefore bound
        nothing in aggregate. This is the only ceiling on the account itself.

        ACCOUNT_MAX_DAILY_LOSS_PCT is read from the environment so it can be
        changed without a code edit. 0 disables the guard, which restores the
        old (unbounded) behaviour for anyone who wants it.
        """
        try:
            limit_pct = float(os.getenv("ACCOUNT_MAX_DAILY_LOSS_PCT", "10"))
        except ValueError:
            limit_pct = 10.0
        if limit_pct <= 0 or getattr(self, "_account_halted", False):
            return

        # Denominator is the account as it stood before today's damage, so the
        # percentage does not shrink as the account does.
        base = equity - day_total
        if base <= 0:
            return
        loss_pct = max(0.0, -day_total / base * 100.0)
        if loss_pct < limit_pct:
            return

        self._account_halted = True
        halted_now = [c for c in self.config.channels if c.enabled and not c.halted]
        for ch in halted_now:
            ch.halted = True
        logger.critical(
            "[MONITOR] ACCOUNT GUARD TRIPPED — day P&L %+.2f = %.1f%% of "
            "$%.2f, limit %.1f%%. Halting all %d enabled channels.",
            day_total, loss_pct, base, limit_pct,
            sum(1 for c in self.config.channels if c.enabled))
        if self.notifier:
            try:
                await self.notifier.send(
                    f"🛑 <b>ACCOUNT GUARD TRIPPED</b>\n"
                    f"Day P&L: <code>{day_total:+.2f}</code> "
                    f"= <code>{loss_pct:.1f}%</code> of "
                    f"<code>${base:.2f}</code>\n"
                    f"Limit: <code>{limit_pct:.1f}%</code> "
                    f"(ACCOUNT_MAX_DAILY_LOSS_PCT)\n"
                    f"<b>All channels halted for the rest of the UTC day.</b>\n"
                    f"Open positions are NOT closed — this stops new entries "
                    f"only. Close them yourself if you want out."
                )
            except Exception:
                pass