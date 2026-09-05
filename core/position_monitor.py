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
# How far a LADDERLESS runner (a bare "gold buy now", which has a stop and no
# target) must travel in its own favour before the trailing engine takes over,
# measured in R - multiples of its own stop distance. 1.0 = one R.
# A ladder runner ignores this and waits for its siblings to close instead.
RUNNER_TRAIL_ARM_R     = float(os.getenv("RUNNER_TRAIL_ARM_R", "1.0"))


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
        # Same idea for the ORDERS pool query. Cleared after two consecutive
        # failures so a terminal that never answers it is asked once, not on
        # every 5-second cycle for the rest of the session.
        self._ea_orders = True
        self._orders_fails = 0

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
            if pending_tickets is not None:
                if int(ticket) in pending_tickets:
                    continue            # waiting for price to come to it
                await self._handle_close(pos)
                continue

            # No orders pool to consult. Ask about THIS ticket instead of
            # giving up on the whole cycle: a position that filled and closed
            # has closing deals, an order that never filled has none. One
            # round trip, and only for a ticket that has actually vanished.
            deal = await self._closing_deals(int(ticket))
            if deal is not None:
                await self._handle_close(pos, deal=deal)

        # Trail any runner whose sibling take-profit legs are done
        if RUNNER_TRAIL_ENABLED:
            await self._trail_runners(db_open, live_tickets)

        # Zone-ladder housekeeping and per-channel breakeven policy
        await self._manage_ladders(db_open, live_tickets)
        await self._auto_breakeven(db_open, live_tickets)

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