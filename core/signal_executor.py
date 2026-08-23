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
import time
import uuid
from datetime import datetime
from typing import Optional

from bridge.mt5_bridge import MT5FileBridge
from config import ChannelConfig
from core.ai_parser import ParsedSignal
from db.database import Database

logger       = logging.getLogger(__name__)
trades_log   = logging.getLogger("trades")   # writes to trades.log


def order_comment(channel_id: str, signal_id: str = "", leg: str = "") -> str:
    """Broker-visible attribution tag, <= 31 chars (the MT5 comment limit).

    The database already records channel_id and channel_name on every signal and
    position, so attribution inside this system was never the problem. The MT5
    comment was: it read `sig_{channel.name[:8]}`, so two channels whose names
    share eight characters were indistinguishable in the terminal and in the
    broker statement, and the name could be edited in channels.json at any time
    which silently orphaned every trade already placed under the old one.

    The id is stable and unique. Format:  c<id>|<sig6>|<leg>
    e.g. "c1643924999|A3F9C1|t2" -> channel -1001643924999, signal ...A3F9C1, TP2.
    """
    cid = str(channel_id).lstrip("-")
    if cid.startswith("100"):           # Telegram's supergroup/channel prefix
        cid = cid[3:]
    parts = [f"c{cid[:11]}"]
    frag = signal_id.rsplit("-", 1)[-1] if signal_id else ""
    if frag:
        parts.append(frag[:6])
    if leg:
        parts.append(leg[:4])
    return "|".join(parts)[:31]


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
        # NOTE: never sent. The bridge does not put a magic number in any
        # command; the EA stamps its own MagicNumber input on every order.
        # Kept so an existing .env entry does not look meaningful.
        self.magic         = int(os.getenv("SIGNAL_MAGIC",    "234567"))
        # Double-cut guard for repeated partial-close announcements.
        self.partial_cooldown_sec = int(os.getenv("PARTIAL_COOLDOWN_SEC", "180"))
        self._last_partial: dict[tuple, float] = {}
        # Symbols whose broker spec has been checked this run.
        self._spec_checked: set[str] = set()
        # (channel_id, message_id) currently mid-flight in _handle_entry.
        # Claimed synchronously; see the comment there.
        self._inflight: set[tuple] = set()
        # Fingerprint -> time, for the same trade arriving as several messages.
        self._recent_trades: dict[tuple, float] = {}
        self.duplicate_window_sec = int(os.getenv("DUPLICATE_WINDOW_SEC", "600"))

    @staticmethod
    def _trade_fingerprint(channel_id, symbol, direction, sl, tps) -> tuple:
        """Identifies the TRADE, not the message that carried it."""
        return (str(channel_id), str(symbol), str(direction).lower(),
                round(float(sl or 0), 2),
                tuple(round(float(t), 2) for t in sorted(tps or [])[:4]))

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
            elif t == "close_partial":
                await self._handle_close_partial(signal, channel)
            elif t == "cancel_pending":
                await self._handle_cancel_pending(signal, channel)
            elif t == "sl_correction":
                await self._handle_sl_correction(signal, channel)
            else:
                # Previously these fell through in silence. A management intent
                # the executor cannot service must be visible, not invisible.
                logger.info("[EXECUTOR] no handler for signal_type=%r — ignored", t)
        except Exception as e:
            logger.error(f"[EXECUTOR] {t} error: {e}", exc_info=True)
            await self._notify(f"⚠️ Error processing {t}: {e}")

    # ── Zone ladder ───────────────────────────────────────────────────────────

    def _ladder_prices(self, signal, channel, direction, classified,
                       market_price) -> dict:
        """{tp_index: entry_price} spreading the legs across the posted zone.

        Returns {} unless the channel asked for it AND the message really did
        post a range. Everything here is a guard against treating something that
        is not a zone as one:

          - the parser must have marked it is_zone (a genuine "a - b" / "a/b")
          - both bounds present, distinct, and the right way round
          - the width must be at least min_zone_width and at most
            max_zone_width, so "4333-4339" ladders but a mis-parse spanning
            hundreds of dollars does not
          - at least two real TP legs; a single leg has nothing to spread
        """
        p = channel.parser or {}
        if str(p.get("zone_fill", "worst")).lower() != "ladder":
            return {}
        if not getattr(signal, "is_zone", False):
            return {}
        lo, hi = getattr(signal, "entry_low", None), getattr(signal, "entry_high", None)
        if lo is None or hi is None:
            return {}
        lo, hi = float(min(lo, hi)), float(max(lo, hi))
        width = hi - lo
        min_w = float(p.get("min_zone_width", 1.0))
        max_w = float(p.get("max_zone_width", 30.0))
        if width < min_w:
            logger.info("[EXECUTOR] zone %.2f-%.2f is %.2f wide, below "
                        "min_zone_width %.2f — single entry", lo, hi, width, min_w)
            return {}
        if width > max_w:
            logger.warning("[EXECUTOR] zone %.2f-%.2f is %.2f wide, above "
                           "max_zone_width %.2f — refusing to ladder, this "
                           "looks like a mis-parsed range", lo, hi, width, max_w)
            return {}

        legs = [ti for ti, tp in classified if tp is not None]
        if len(legs) < 2:
            return {}

        # Nearest bound to the market first. For a buy the zone sits below
        # price, so the HIGH edge fills first; for a sell, the LOW edge.
        near, far = (hi, lo) if direction == "buy" else (lo, hi)
        step = (far - near) / (len(legs) - 1)
        out = {}
        for i, ti in enumerate(sorted(legs)):          # TP1 first => nearest
            out[ti] = round(near + step * i, 2)
        # The runner, if any, rides the nearest edge: it is the leg we most want
        # filled and it has no target to reach.
        for ti, tp in classified:
            if tp is None:
                out[ti] = round(near, 2)
        return out

    # ── Symbol spec check ─────────────────────────────────────────────────────

    async def _verify_symbol_spec(self, symbol: str):
        """Check CONTRACT_SIZE against what the broker actually says, once.

        Lot size is risk_amt / (sl_dist * contract_size * n_tps). CONTRACT_SIZE
        is an env var defaulting to 100.0 and nothing ever checked it against
        the symbol. A broker whose gold contract is 10 (common behind an "m" or
        "micro" suffix) would give every position ten times the intended size,
        in the same direction, silently, forever.
        """
        if symbol in self._spec_checked:
            return
        self._spec_checked.add(symbol)
        fn = getattr(self.bridge, "get_symbol_info", None)
        if fn is None:
            return
        try:
            spec = await fn(symbol)
        except Exception as e:
            logger.debug(f"[EXECUTOR] get_symbol_info failed: {e}")
            return
        if not spec or spec.get("status") != "success":
            logger.info("[EXECUTOR] broker spec for %s unavailable "
                        "(EA older than v2.505?) — CONTRACT_SIZE=%s unverified",
                        symbol, self.contract_size)
            return

        broker_cs = float(spec.get("contract_size") or 0.0)
        if broker_cs > 0 and abs(broker_cs - self.contract_size) > 1e-6:
            ratio = broker_cs / self.contract_size
            msg = (f"⚠️ <b>CONTRACT_SIZE mismatch</b> — {symbol}\n"
                   f"Broker says <code>{broker_cs:g}</code>, .env says "
                   f"<code>{self.contract_size:g}</code>.\n"
                   f"Every lot is being sized <b>{ratio:.3g}x</b> off. "
                   f"Set <code>CONTRACT_SIZE={broker_cs:g}</code> and restart.")
            logger.error("[EXECUTOR] CONTRACT_SIZE mismatch on %s: broker=%s "
                         "env=%s (lots are %.3gx off)",
                         symbol, broker_cs, self.contract_size, ratio)
            await self._notify(msg)

        for key, mine, name in (("volume_min", self.min_lot, "MIN_LOT"),
                                ("volume_step", self.lot_step, "LOT_STEP")):
            theirs = float(spec.get(key) or 0.0)
            if theirs > 0 and abs(theirs - mine) > 1e-9:
                logger.warning("[EXECUTOR] %s on %s: broker=%s env %s=%s",
                               key, symbol, theirs, name, mine)

    # ── Entry ──────────────────────────────────────────────────────────────────

    async def _handle_entry(self, signal: ParsedSignal, channel: ChannelConfig,
                              message_id: int):
        # ── Idempotency, layer 1: claim the message BEFORE any await ─────────
        # The DB check below is necessary but was not sufficient. Between it and
        # save_signal this coroutine awaits get_price, the symbol spec and the
        # working balance: one to two seconds of file-bridge round trips. Two
        # events for the same message (Telegram delivers NewMessage and
        # MessageEdited when an operator fixes a typo, often in the same second)
        # both passed the check while neither had saved yet, and both traded.
        # 2026-08-21 msg 44295 did exactly this 56 ms apart.
        # This claim is synchronous, so there is no await for a second
        # invocation to interleave into.
        claim = (str(channel.id), int(message_id or 0))
        if claim in self._inflight:
            logger.warning(
                f"[EXECUTOR] message_id={message_id} is already being executed "
                f"right now — refusing the concurrent duplicate")
            return
        self._inflight.add(claim)
        fp_holder: list = []
        try:
            await self._execute_entry(signal, channel, message_id, fp_holder)
        except Exception:
            raise
        finally:
            self._inflight.discard(claim)
            # If the fingerprint was reserved but nothing was actually placed,
            # release it. Otherwise a signal that failed on a bridge timeout
            # would block the operator's legitimate repost of the same trade.
            if fp_holder and not getattr(self, "_last_entry_placed", False):
                self._recent_trades.pop(fp_holder[0], None)

    async def _execute_entry(self, signal: ParsedSignal, channel: ChannelConfig,
                             message_id: int, fp_holder: Optional[list] = None):
        self._last_entry_placed = False
        # ── Idempotency, layer 2: has this message already traded? ────────────
        # any_status=True on purpose. The old query only looked at pending/open
        # signals, so once a signal closed, a later edit of the same message
        # opened the whole trade again.
        existing = self.db.get_signals_by_message(channel.id, message_id,
                                                  any_status=True)
        existing_full = [s for s in existing if not s["is_bare"]]
        if existing_full:
            logger.info(
                f"[EXECUTOR] message_id={message_id} already has full signal "
                f"{existing_full[0]['signal_id']} — skipping duplicate"
            )
            prev = existing_full[0]
            if (prev["stop_loss"] and signal.stop_loss
                    and abs(float(prev["stop_loss"]) - float(signal.stop_loss)) > 1e-9):
                # An edit that moved the levels after we already traded them.
                # Nothing is auto-corrected: the position is already open at the
                # original levels and changing it silently would be worse.
                await self._notify(
                    f"⚠️ <b>Edited after execution</b> — {channel.name}\n"
                    f"msg {message_id} now says SL <code>{signal.stop_loss}</code>, "
                    f"traded at <code>{prev['stop_loss']}</code>.\n"
                    f"Not re-executed. Check the open position yourself.")
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

        # ── Idempotency, layer 3: the same TRADE from a different message ────
        # Layers 1 and 2 key on message_id, so they cannot see an operator who
        # posts the same setup twice as two separate messages. GTMO did that on
        # 2026-08-21: msg 44294 and msg 44295 carried byte-identical text, and
        # together with the edit of 44295 one trade opened fifteen positions.
        # This keys on the trade itself: symbol, direction, stop and targets.
        fp = self._trade_fingerprint(channel.id, symbol, direction, sl, tps)
        now = time.time()
        self._recent_trades = {k: v for k, v in self._recent_trades.items()
                               if now - v <= self.duplicate_window_sec}
        prev_t = self._recent_trades.get(fp)
        if prev_t is not None:
            age = now - prev_t
            logger.warning(
                "[EXECUTOR] %s: identical trade already opened %.0fs ago "
                "(SL %s, TPs %s) — refusing repost from message %s",
                channel.name, age, sl, tps, message_id)
            await self._notify(
                f"🔁 <b>Duplicate suppressed</b> — {channel.name}\n"
                f"Same {direction.upper()} {symbol} (SL <code>{sl}</code>) was "
                f"opened <b>{age:.0f}s</b> ago.\n"
                f"msg {message_id} not executed. "
                f"Window: {self.duplicate_window_sec}s "
                f"(<code>DUPLICATE_WINDOW_SEC</code>).")
            return
        self._recent_trades[fp] = now
        if fp_holder is not None:
            fp_holder.append(fp)

        price = await self.bridge.get_price(symbol, direction)
        if not price:
            self._recent_trades.pop(fp, None)   # nothing opened; do not block a retry
            await self._notify(f"⚠️ Cannot get price for {symbol}"); return

        await self._verify_symbol_spec(symbol)

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
        # ── Zone ladder ───────────────────────────────────────────────────────
        # "Buy from 4333-4339" is an instruction to accumulate ACROSS the range,
        # not to pick one price out of it. zone_fill="worst" collapsed the range
        # to a single number and threw the rest away. In ladder mode each TP leg
        # rests at its own price, spread evenly across the zone.
        #
        # Pairing: the leg NEAREST the market carries TP1. That leg fills on the
        # smallest retrace, so it must carry the target most likely to be hit;
        # giving it the furthest target means the leg that almost always fills is
        # the one that almost never completes.
        ladder = self._ladder_prices(signal, channel, direction, classified, price)
        if ladder:
            lo, hi = min(ladder.values()), max(ladder.values())
            logger.info("[EXECUTOR] %s zone ladder %.2f-%.2f across %d legs "
                        "(nearest leg carries TP1)",
                        channel.name, lo, hi, len(ladder))
            entry_type = "limit"

        for tp_index, tp_price in classified:
            is_runner = tp_price is None
            leg_entry = ladder.get(tp_index) if ladder else None
            if leg_entry is not None:
                entry_price = leg_entry
            row_id = self.db.save_position(
                signal_id=signal_id, channel_id=channel.id,
                tp_index=tp_index, tp_price=(0.0 if is_runner else tp_price),
                lot_size=lot_per_tp, stop_loss=sl, order_type=entry_type
            )

            if is_runner and leg_entry is not None:
                # Laddered runner: rest it at the near edge like the other legs,
                # still with no take profit.
                res = await self.bridge.place_limit_order(
                    symbol, direction, lot_per_tp, leg_entry, sl, 0.0,
                    comment=order_comment(channel.id, signal_id, "run"))
                order_label = f"runner@{leg_entry:.2f}"
            elif is_runner:
                # Runner: open with SL only, no TP (tp=0 = unlimited)
                res = await self.bridge.place_market_order(
                    symbol, direction, lot_per_tp, sl=sl, tp=0.0,
                    comment=order_comment(channel.id, signal_id, "run"))
                order_label = "runner"
            elif entry_type == "limit" and entry_price:
                res = await self.bridge.place_limit_order(
                    symbol, direction, lot_per_tp, entry_price, sl, tp_price,
                    comment=order_comment(channel.id, signal_id, f"t{tp_index}"))
                order_label = f"limit@{entry_price:.2f}"
            elif entry_type == "stop" and entry_price:
                res = await self.bridge.place_stop_order(
                    symbol, direction, lot_per_tp, entry_price, sl, tp_price,
                    comment=order_comment(channel.id, signal_id, f"t{tp_index}"))
                order_label = f"stop@{entry_price:.2f}"
            else:
                res = await self.bridge.place_market_order(
                    symbol, direction, lot_per_tp, sl, tp_price,
                    comment=order_comment(channel.id, signal_id, f"t{tp_index}"))
                order_label = "market"

            if res and res.get("ticket"):
                self._last_entry_placed = True
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
        Marshal posts "sell/buy now" to enter early while he types the rest of
        the signal. When the full details arrive we MODIFY the bare positions
        in place (add SL + a target TP) so the early entry rides the trade —
        we do NOT close them. The full-sized batch for all TPs is opened on
        top of the modified bare(s) by the caller.

        Returns the list of bare tickets that were successfully modified.
        """
        upgraded = []
        # Pick a mid-TP for the bare to aim at (TP3 if available, else last).
        target_tp = tps[min(2, len(tps) - 1)] if tps else 0.0

        for bare in self.db.get_bare_signals(channel.id):
            if bare["symbol"] != symbol or bare["direction"] != direction:
                continue
            for pos in self.db.get_open_positions(bare["signal_id"]):
                if not pos["ticket"]:
                    continue
                ok = await self.bridge.modify_position(
                    pos["ticket"], sl, target_tp)
                if ok:
                    upgraded.append(pos["ticket"])
                    logger.info(
                        f"[EXECUTOR] Upgraded bare ticket={pos['ticket']} "
                        f"sl={sl} tp={target_tp}"
                    )
                else:
                    logger.warning(
                        f"[EXECUTOR] Failed to modify bare ticket={pos['ticket']} "
                        f"sl={sl} tp={target_tp} — leaving as-is"
                    )
            # Mark the bare signal as upgraded (status='open' so position_monitor
            # keeps tracking it through to close).
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
                comment=order_comment(channel.id, signal_id, "bare"))
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
        """Log only. Deliberately does NOT notify.

        The operator posting "TP2 hit" is them telling their followers, not the
        broker telling us anything. PositionMonitor announces the real fill
        when MT5 actually closes the leg, with the real reason and the real
        P&L. Announcing both produced two messages per take profit, the first
        of which carried no P&L and could not be trusted: the operator's post
        and our fill are not always the same event, and sometimes there is no
        fill at all.
        """
        logger.info(
            "[EXECUTOR] %s reported TP%s hit on %s — no action, the fill "
            "notification comes from the position monitor",
            channel.name, signal.tp_number or "?", signal.symbol)

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

    # ── Partial close ─────────────────────────────────────────────────────────

    async def _handle_close_partial(self, signal, channel):
        """Close a fraction of every open position for this channel.

        Guarded against double-cutting. Several operators post a partial-close
        announcement at each TP hit, but the broker-side TP ladder has usually
        already taken that leg. Re-cutting on the announcement shrinks the
        runner a second time for the same event.
        """
        fraction = getattr(signal, "close_fraction", None) or 0.5
        fraction = min(max(fraction, 0.01), 1.0)

        rows = []
        if signal.reply_to_id:
            row = self.db.get_signal_by_message(channel.id, signal.reply_to_id)
            if row:
                rows = [row]
        if not rows:
            rows = self.db.get_open_signals(channel.id, signal.symbol)
        if not rows:
            await self._notify(
                f"⚠️ Partial close: no open signals for {channel.name}"); return

        now = time.time()
        closed, skipped = 0, 0
        for row in rows:
            key = (channel.id, row["signal_id"])
            last = self._last_partial.get(key, 0.0)
            if now - last < self.partial_cooldown_sec:
                skipped += 1
                logger.info(
                    "[EXECUTOR] partial on %s suppressed: another partial "
                    "%.0fs ago (cooldown %ds)",
                    row["signal_id"], now - last, self.partial_cooldown_sec)
                continue

            took_any = False
            for pos in self.db.get_open_positions(row["signal_id"]):
                if not pos["ticket"]:
                    continue
                full = float(pos["lot_size"] or 0.0)
                want = _floor_lot(full * fraction, self.lot_step, self.min_lot)
                if full <= 0 or want <= 0:
                    continue
                if want >= full - 1e-9:
                    # Rounding pushed the slice to the whole position. Closing
                    # it entirely is not what "close half" asked for; a broker
                    # that cannot split this lot leaves nothing to run.
                    logger.info(
                        "[EXECUTOR] ticket %s lot %.2f cannot be split at "
                        "%.0f%% with step %.2f — left untouched",
                        pos["ticket"], full, fraction * 100, self.lot_step)
                    continue
                if await self.bridge.close_position(pos["ticket"], lot=want):
                    trades_log.info(
                        f"CLOSE_PARTIAL channel={channel.name} "
                        f"ticket={pos['ticket']} lot={want:.2f}/{full:.2f} "
                        f"signal={row['signal_id']}"
                    )
                    closed += 1
                    took_any = True
            if took_any:
                self._last_partial[key] = now

        msg = (f"🟠 <b>Partial close {fraction * 100:.0f}%</b> — {channel.name}\n"
               f"Reduced {closed} position(s).")
        if skipped:
            msg += f"\n{skipped} signal(s) skipped (partial cooldown)."
        await self._notify(msg)

    # ── Cancel pending orders ─────────────────────────────────────────────────

    async def _handle_cancel_pending(self, signal, channel):
        """Delete unfilled orders only. Never touch a filled position.

        The database marks a limit order 'open' the moment it is accepted, so it
        cannot tell a resting order from a filled one. The terminal can:
        get_all_orders() returns only orders that have NOT filled. Anything not
        in that set is either filled or gone, and is left alone.
        """
        live = await self.bridge.get_all_orders()
        if live is None:
            await self._notify(
                f"⚠️ Cancel: cannot read pending orders from MT5 — nothing "
                f"cancelled for {channel.name}.")
            return
        pending = {int(o["ticket"]) for o in live if o.get("ticket")}
        if not pending:
            await self._notify(
                f"ℹ️ Cancel — {channel.name}: no unfilled orders."); return

        want_dir = (signal.direction or "").lower() or None
        cancelled, kept = 0, 0
        for row in self.db.get_open_signals(channel.id, signal.symbol):
            if want_dir and (row["direction"] or "").lower() != want_dir:
                continue
            for pos in self.db.get_open_positions(row["signal_id"]):
                t = pos["ticket"]
                if not t:
                    continue
                if int(t) not in pending:
                    kept += 1          # already filled: not ours to cancel
                    continue
                if await self.bridge.cancel_order(int(t)):
                    self.db.update_position_closed(int(t), 0.0, "cancelled")
                    trades_log.info(
                        f"CANCEL_PENDING channel={channel.name} ticket={t} "
                        f"signal={row['signal_id']}"
                    )
                    cancelled += 1

        await self._notify(
            f"🚫 <b>Cancelled pending"
            f"{' ' + want_dir.upper() if want_dir else ''}</b> — {channel.name}\n"
            f"{cancelled} order(s) deleted, {kept} filled position(s) untouched.")

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