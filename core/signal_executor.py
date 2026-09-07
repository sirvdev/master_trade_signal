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


# Appended to the broker comment of a resting order the OPERATOR manages
# himself. The EA's stale-pending sweep skips any order carrying it.
#
# Gold Hunter posts limits and then says "cancel this one" explicitly. An
# automatic sweep deleting his orders behind his back would take the trade
# away from him seconds before he asks for it. The EA has no idea which
# channel an order came from, so the exemption has to travel with the order,
# and the comment is the only field that makes the round trip.
OPERATOR_MANAGED_MARK = "~"


def order_comment(channel_id: str, signal_id: str = "", leg: str = "",
                  operator_managed: bool = False) -> str:
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
    # NOT '|'. get_all_orders is the one EA reply that echoes this string back,
    # and the bridge used to treat a pipe as a record separator, so every
    # comment written here shredded the orders reply and the orders pool read
    # as permanently unavailable. The bridge no longer splits on pipes, but
    # putting a delimiter character inside a payload is a trap either way.
    out = "-".join(parts)[:31]
    if operator_managed:
        # Always fits: 11 + 6 + 4 + 2 hyphens = 23 at worst, so the mark is
        # never the character truncation eats.
        out = (out[:30] + OPERATOR_MANAGED_MARK)
    return out


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
        # Cross-channel mirror NOTICE. Off by default, by operator decision:
        # every channel keeps its own system_balance and is being graded
        # against the others, so a trade several channels relay is several
        # independent results and must be executed every time. It never
        # blocked anything even when on — set MIRROR_WINDOW_SEC to a positive
        # number of seconds if you ever want the heads-up back.
        self.mirror_window_sec = int(os.getenv("MIRROR_WINDOW_SEC", "0"))

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

    # Instructions that can only ever REDUCE exposure. A halted channel must
    # still obey these: halting is meant to bound a loss, and the operator's
    # "close all" is the fastest way to bound it.
    _RISK_REDUCING = {"close", "close_all", "close_partial", "breakeven",
                      "sl_correction", "cancel_pending", "tp_hit"}

    async def execute(self, signal: ParsedSignal, channel: ChannelConfig,
                       message_id: int = 0):
        t = signal.signal_type
        # 2026-09-06: this gate used to sit above the whole dispatch, so a
        # halted channel refused CLOSE as well as entries. The account guard
        # halts all 28 channels at once and its own notification says "close
        # them yourself if you want out" - while this line had disabled exactly
        # that path. In the state the breaker fires, every open position lost
        # its manual exit. Only new exposure is blocked now.
        if channel.halted and t not in self._RISK_REDUCING:
            await self._notify(
                f"⛔ <b>{channel.name}</b> halted (drawdown limit). "
                f"New entries ignored; close and breakeven instructions still run.")
            return

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
        # ── Idempotency, layer 4: the same trade is STILL LIVE ───────────────
        # Layer 3 expires after duplicate_window_sec, which is correct for a
        # market order: an hour later, with the first trade closed, a repost is
        # a genuine new trade. It is wrong for a pending order that never
        # filled. On 2026-08-24 Gold Hunter reposted the same buy limit @4643
        # 49 minutes later and the same @4621.74 eleven hours later; both times
        # the original was still sitting unfilled and a second identical order
        # went on top of it. This asks whether the trade is live, not how long
        # ago we saw it.
        live = None
        try:
            live = self.db.find_live_duplicate(channel.id, direction, sl, tps,
                                               exclude_message_id=message_id)
        except Exception as e:
            logger.debug("[EXECUTOR] live-duplicate check unavailable: %s", e)
        if live is not None:
            self._recent_trades.pop(fp, None)
            logger.warning(
                "[EXECUTOR] %s: %s %s (SL %s, TPs %s) is already live as %s "
                "from message %s — refusing the repost from message %s",
                channel.name, direction.upper(), symbol, sl, tps,
                live["signal_id"], live["message_id"], message_id)
            await self._notify(
                f"🔁 <b>Repost of a live trade</b> — {channel.name}\n"
                f"The same {direction.upper()} {symbol} "
                f"(SL <code>{sl}</code>) is still "
                f"{'pending' if str(live['entry_type']) != 'market' else 'open'}"
                f" from msg {live['message_id']}.\n"
                f"msg {message_id} not executed.")
            return

        # ── Mirror channels: NOTICE ONLY, and off by default ─────────────────
        # Several enabled channels relay the same desk. Every one of them still
        # trades it, always: each channel carries its own system_balance and is
        # being graded against the others, so the same setup arriving on four
        # channels is four independent results, not one duplicated. Their
        # entries also diverge in practice — the four channels that relayed the
        # 2026-08-25 07:34 SELL 4636 opened 0.06, 0.04, 0.04 and 0.03 lots at
        # different moments, which is exactly the difference being measured.
        #
        # Nothing below this line can refuse a trade. It is a heads-up, and it
        # is disabled unless MIRROR_WINDOW_SEC is set to a positive value.
        if self.mirror_window_sec > 0:
            try:
                mirrors = self.db.find_mirror_signals(
                    channel.id, direction, sl, tps, self.mirror_window_sec)
            except Exception:
                mirrors = []
            if mirrors:
                names = ", ".join(dict.fromkeys(m["channel_name"] for m in mirrors))
                logger.warning(
                    "[EXECUTOR] MIRROR: %s posted the same %s %s (SL %s) "
                    "already posted by %s within %ds — taking it again",
                    channel.name, direction.upper(), symbol, sl, names,
                    self.mirror_window_sec)
                await self._notify(
                    f"👯 <b>Mirror signal</b> — {channel.name}\n"
                    f"The same {direction.upper()} {symbol} "
                    f"(SL <code>{sl}</code>) was already taken from "
                    f"<b>{names}</b> in the last "
                    f"{self.mirror_window_sec // 60} min.\n"
                    f"Trading it anyway (that is what the measurement week is "
                    f"for). This desk's opinion is now on "
                    f"<b>{len(mirrors) + 1}</b> channels.")

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

        # The lot is sized as risk_amt / (sl_dist * contract * n). Only an
        # EXACTLY zero distance used to be refused, so a stop the market had
        # drifted to within a few cents of produced an enormous position: at
        # sl_dist 0.50 on a $1000 book at 10% across 3 legs that is 1.98 lots,
        # 198 ounces, and one dollar of adverse movement costs 20% of the book.
        # MAX_LOT=10 only binds below a 3-cent stop, so it was never the guard.
        #
        # The parser enforces min_sl_distance against the POSTED entry; this is
        # the same rule applied to the price we will actually pay.
        # ── Geometry against the REAL fill, not the posted level ──────────────
        # SignalParser.finalize_market() exists to run exactly these three
        # checks once the fill price is known, and it has never had a caller:
        # for every market entry, nothing verified the stop was on the correct
        # side of the price we actually pay. The parser's own comment says so.
        # Running the checks here keeps them next to the price they apply to.
        _d = (direction or "").lower()
        if (_d == "buy" and sl >= price) or (_d == "sell" and sl <= price):
            await self._notify(
                f"⚠️ <b>Stop is on the wrong side of the market</b> — {channel.name}\n"
                f"{_d.upper()} {symbol} would fill near <code>{price:g}</code> with "
                f"the stop at <code>{sl:g}</code>. The posted levels are stale — "
                f"the market has already gone through them. Not trading it.")
            self.db.log_skipped_signal(
                channel.id, message_id, "entry",
                f"stop {sl} on the wrong side of the live price {price} for a {_d}")
            return
        _live_tps = [t for t in (tps or []) if t]
        if _live_tps and all(
                (t <= price if _d == "buy" else t >= price) for t in _live_tps):
            await self._notify(
                f"⚠️ <b>Every target is behind the market</b> — {channel.name}\n"
                f"{_d.upper()} {symbol} near <code>{price:g}</code>, targets "
                f"{', '.join(f'{t:g}' for t in _live_tps)}. Nothing left to reach.")
            self.db.log_skipped_signal(
                channel.id, message_id, "entry",
                f"all targets behind the live price {price} for a {_d}")
            return

        sl_dist = abs(price - sl)
        min_dist = float((channel.parser or {}).get("min_sl_distance", 1.0) or 1.0)
        if sl_dist < min_dist:
            await self._notify(
                f"⚠️ <b>Stop too close</b> — {channel.name}\n"
                f"{symbol} at <code>{price:g}</code> is <code>{sl_dist:.2f}</code> "
                f"from the stop <code>{sl:g}</code>, under this channel's minimum "
                f"<code>{min_dist:g}</code>. The market has moved into the stop "
                f"since the signal was posted. Not sizing off a distance this "
                f"small — the lot would be enormous.")
            self.db.log_skipped_signal(
                channel.id, message_id, "entry",
                f"live stop distance {sl_dist:.2f} below min_sl_distance {min_dist:g}")
            return

        # Classify TPs — market or limit based on signal.entry_type
        entry_type  = signal.entry_type or "market"
        entry_price = signal.entry_price   # limit price (None = use current for market)

        # ── per-channel off switch for resting orders ────────────────────────
        # A limit that never fills is not free. It sits armed at a price the
        # market has left, and fills on the pullback into a move that has
        # already happened, with a stop sized for a completely different
        # entry. Set parser.allow_limit_orders=false on a channel to take its
        # signals at market instead, or "skip" to refuse them outright.
        # This operator cancels his own resting orders by hand ("cancel the
        # 4436 one"), so nothing here or in the EA may delete them behind him.
        op_managed = bool((channel.parser or {}).get(
            "operator_manages_limits", False))
        allow = (channel.parser or {}).get("allow_limit_orders", True)
        if entry_type in ("limit", "stop") and allow is not True:
            if str(allow).lower() == "skip":
                logger.info("[EXECUTOR] %s: resting %s order refused "
                            "(allow_limit_orders=skip)", channel.name, entry_type)
                try:
                    self.db.log_skipped_signal(
                        channel.id, message_id, "entry",
                        f"{entry_type} order refused: allow_limit_orders=skip")
                except Exception:
                    pass
                await self._notify(
                    f"⏭️ <b>Resting order skipped</b> — {channel.name}\n"
                    f"{direction.upper()} {symbol} at <code>{entry_price}</code> "
                    f"was a {entry_type} order and this channel has "
                    f"<code>allow_limit_orders</code> set to skip.")
                return
            logger.info("[EXECUTOR] %s: taking the %s order at market instead "
                        "(allow_limit_orders=false)", channel.name, entry_type)
            entry_type, entry_price = "market", None
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

        # ── Runner support (MUST come before sizing) ──────────────────────────
        # "TP open" / "TP runner" means a leg with no fixed target. It used to
        # be appended AFTER the budget was divided, so it was an extra leg at
        # the same lot: realised risk was (N+1)/N of intended. Measured over
        # the week of 31 Aug, 18 signals carrying a runner took 1.29x their
        # configured risk; the worst (one target plus a runner) took 2.00x -
        # 20% of a $1000 book on a channel set to 10%. Counting it here makes
        # the runner share the budget like any other leg.
        has_runner = bool(getattr(signal, "has_runner", False))

        # ── Lot sizing — keep risk == effective_risk_pct of working_balance ───
        risk_amt = working_balance * (effective_risk_pct / 100.0)
        n_tps    = max(1, len(classified) + (1 if has_runner else 0))
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

        # Append the runner now that it has already been paid for above. Its
        # index continues the ladder; _tp_passed may have dropped earlier legs,
        # so take the highest index in use rather than the list length, which
        # could otherwise collide with a surviving leg and put two orders at
        # one price with a duplicate (signal_id, tp_index).
        if has_runner:
            _next = max([ti for ti, _ in classified], default=0) + 1
            classified.append((_next, None))

        # Save signal
        signal_id = f"SIG-{message_id}-{uuid.uuid4().hex[:6].upper()}"
        self.db.save_signal(
            signal_id=signal_id, channel_id=channel.id,
            channel_name=channel.name, message_id=message_id,
            reply_to_id=signal.reply_to_id, raw_text=signal.raw_text,
            symbol=symbol, direction=direction,
            entry_type=entry_type,
            entry_price=entry_price, stop_loss=sl,
            take_profits=tps, status="open",
            # Carry the parser's own notes into the row. "SL synthesised at 70
            # pips", "stop treated as a typo", "zone collapsed to a single
            # entry" - these were being generated and then dropped, so the
            # trades taken on an inferred stop could never be graded as their
            # own population, which was the entire reason for tagging them.
            notes=" | ".join(getattr(signal, "warnings", None) or []) or None
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
            # halted was read once in execute(), before four awaits with 10-30
            # second timeouts. The monitor runs every 5 seconds and the account
            # guard halts all 28 channels at once, so a signal already in flight
            # placed its whole batch anyway - and a 4-leg ladder spends ~40s in
            # this loop. Re-read it between legs.
            if channel.halted:
                logger.warning(
                    "[EXECUTOR] %s halted mid-batch — stopping after %d leg(s)",
                    channel.name, len(placed))
                await self._notify(
                    f"⛔ <b>{channel.name}</b> halted while this signal was being "
                    f"placed. Stopped after <b>{len(placed)}</b> leg(s); the rest "
                    f"were not sent.")
                break
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
                    comment=order_comment(channel.id, signal_id, "run",
                                          operator_managed=op_managed))
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
                    comment=order_comment(channel.id, signal_id, f"t{tp_index}",
                                          operator_managed=op_managed))
                order_label = f"limit@{entry_price:.2f}"
            elif entry_type == "stop" and entry_price:
                res = await self.bridge.place_stop_order(
                    symbol, direction, lot_per_tp, entry_price, sl, tp_price,
                    comment=order_comment(channel.id, signal_id, f"t{tp_index}",
                                          operator_managed=op_managed))
                order_label = f"stop@{entry_price:.2f}"
            else:
                res = await self.bridge.place_market_order(
                    symbol, direction, lot_per_tp, sl, tp_price,
                    comment=order_comment(channel.id, signal_id, f"t{tp_index}"))
                order_label = "market"

            if res and res.get("ticket"):
                self._last_entry_placed = True
                t  = int(res["ticket"])
                # A pending order has no fill price yet and the EA reports
                # price 0.0 for one. dict.get returns that 0.0 rather than the
                # default, which is how every limit order ended up recorded
                # with entry_price=0.0 — and entry_price 0 makes _auto_breakeven
                # and the ladder anchor skip the leg silently.
                ep = res.get("price") or 0.0
                if not ep:
                    ep = (leg_entry if leg_entry is not None
                          else entry_price) or price
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
        price = await self.bridge.get_price(symbol, direction)

        for bare in self.db.get_bare_signals(channel.id):
            if bare["symbol"] != symbol or bare["direction"] != direction:
                continue
            for pos in self.db.get_open_positions(bare["signal_id"]):
                if not pos["ticket"]:
                    continue
                # Aim at the nearest target price has NOT already gone through.
                # This used to take tps[2] blindly. On 2026-08-28 Forex Expert
                # Team posted "Gold sell now" and then a full signal whose TP1,
                # TP2 and TP3 the market had already passed; the upgrade asked
                # the broker for TP 4595 on a sell filled at 4590.94 — a target
                # on the WRONG SIDE of the fill. The modify was rejected, the
                # bare kept its own wide protective stop, and it stopped out.
                # Twice in three minutes, -19.70.
                fill = float(pos["entry_price"] or 0) or (price or 0)
                live = [t for t in (tps or [])
                        if not self._tp_passed(direction, fill, t)]
                if not live:
                    logger.info(
                        "[EXECUTOR] bare ticket=%s: every posted target is "
                        "already behind the fill at %s — leaving its own stop "
                        "in place rather than sending an invalid one",
                        pos["ticket"], fill)
                    continue
                target_tp = live[0]
                # A stop on the wrong side of the fill is rejected too, and
                # would be a market order if it were not.
                #
                # THE BUG THIS REPLACES (live 2026-08-31 to 2026-09-07).
                # This called _tp_passed(direction, fill, sl), which asks "has
                # price reached this TARGET" - `fill >= sl` for a buy. A buy's
                # stop is ALWAYS below its fill, so that was always true and
                # the guard refused every correct stop. Same inverted for a
                # sell. Result: 72 refusals, ZERO successful upgrades, ever.
                # Every pre-signal leg kept its blind 70-pip stop and no
                # target while the full-size batch opened alongside it, so the
                # channel carried both.
                #
                # A stop is on the wrong side when it is where the TRADE IS
                # GOING, not where it came from.
                if fill and sl and self._sl_wrong_side(direction, fill, sl):
                    logger.warning(
                        "[EXECUTOR] bare ticket=%s: the signal's stop %s is "
                        "already through the fill at %s — keeping the "
                        "protective stop it opened with",
                        pos["ticket"], sl, fill)
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

    # Protective stop for a blind entry, in pips. 100 pips = $10 on gold with
    # pip_value 0.1. Wide enough that ordinary noise does not take it, narrow
    # enough that the loss is bounded while waiting for levels that arrive
    # only about 59% of the time. Per channel: parser.pre_signal_sl_pips.
    # Protective stop for a bare call, in pips, when the channel does not set
    # its own pre_signal_sl_pips. Matches defaults.parser.default_sl_pips so a
    # bare call and an unreadable stop are handled with one number rather than
    # two that can drift apart.
    PRE_SIGNAL_SL_PIPS_DEFAULT = 70.0

    async def _handle_pre_announcement(self, signal: ParsedSignal,
                                        channel: ChannelConfig, message_id: int):
        symbol    = signal.symbol or channel.symbol
        direction = signal.direction
        if not direction:
            return

        emoji     = "📢🟢" if direction == "buy" else "📢🔴"

        # A repeat of a bare call while the first one is still open is the
        # operator saying the same thing twice, not a second trade. Jason Noah
        # posted "Scalping buy gold slowly high risk" 119 times in the corpus.
        # find_live_duplicate cannot see these: it filters is_bare=0.
        try:
            live_bare = [s for s in self.db.get_bare_signals(channel.id)
                         if str(s["direction"]).lower() == direction.lower()
                         and s["status"] in ("pending", "open")]
        except Exception:
            live_bare = []
        if live_bare:
            logger.info(
                "[EXECUTOR] %s: a bare %s is already open (%s) — not opening "
                "another for message %s",
                channel.name, direction.upper(), live_bare[0]["signal_id"],
                message_id)
            return

        # A blind position with no stop is an unbounded loss waiting for an
        # instruction that arrives 41% of the time. Give it a real stop at a
        # distance wide enough not to be noise and narrow enough to be a stop.
        p = channel.parser or {}
        pip = float(p.get("pip_value", 0.1))
        sl_pips = float(p.get("pre_signal_sl_pips",
                              self.PRE_SIGNAL_SL_PIPS_DEFAULT))
        price = await self.bridge.get_price(symbol, direction)
        if not price:
            await self._notify(
                f"⚠️ Pre-signal — {channel.name}: no price for {symbol}, "
                f"cannot place a protective stop. Not opening blind.")
            return
        sl = (price - sl_pips * pip) if direction == "buy" \
            else (price + sl_pips * pip)
        sl = round(sl, 3)

        signal_id = f"BARE-{message_id}-{uuid.uuid4().hex[:6].upper()}"
        self.db.save_signal(
            signal_id=signal_id, channel_id=channel.id,
            channel_name=channel.name, message_id=message_id,
            reply_to_id=None, raw_text=signal.raw_text,
            symbol=symbol, direction=direction,
            entry_type="market", entry_price=None,
            stop_loss=sl, take_profits=[],
            status="pending", is_bare=True
        )

        opened = []
        for i in range(channel.pre_ann_positions):
            row_id = self.db.save_position(
                signal_id=signal_id, channel_id=channel.id,
                tp_index=i + 1, tp_price=0.0,
                lot_size=self.min_lot, stop_loss=sl, order_type="market"
            )
            res = await self.bridge.place_market_order(
                symbol, direction, self.min_lot, sl=sl, tp=0.0,
                comment=order_comment(channel.id, signal_id, "bare"))
            if res and res.get("ticket"):
                t = int(res["ticket"])
                self.db.update_position_opened(row_id, t,
                                               res.get("price") or price)
                opened.append(t)
                trades_log.info(
                    f"OPEN_BARE signal={signal_id} channel={channel.name} "
                    f"{direction.upper()} {symbol} lot={self.min_lot} "
                    f"sl={sl} ticket={t}"
                )

        self.db.update_signal_status(signal_id, "open" if opened else "failed")
        every = int(os.getenv("BARE_PARTIAL_MINUTES", "39"))
        arm_r = float(os.getenv("RUNNER_TRAIL_ARM_R", "1.0"))
        one_r = (price + arm_r * sl_pips * pip) if direction == "buy" \
            else (price - arm_r * sl_pips * pip)
        await self._notify(
            f"{emoji} <b>Pre-signal opened</b> — {channel.name}\n"
            f"<b>{direction.upper()} {symbol}</b>  ×{len(opened)} "
            f"at <code>{price:g}</code>\n"
            f"Protective SL <code>{sl:g}</code> ({sl_pips:g} pips)\n"
            f"Tickets: {', '.join(f'<code>{t}</code>' for t in opened)}\n"
            f"📈 No fixed target. At <code>{round(one_r, 2):g}</code> "
            f"({arm_r:g}R) the EA takes over the stop and trails it.\n"
            f"⏳ Upgrades when the levels arrive; otherwise scales out every "
            f"{every} min\n"
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
        """Move the stop to entry when the operator says to go risk free.

        Two guards, both added after Lion Trading on 2026-08-25. The operator
        posted "GOLD sell now" and the full signal 87 seconds later; our fill
        was 4635.028, roughly five pips worse than his. At 08:16 he said "you
        can go risk free". His stop sat at his entry, ours at ours, five pips
        lower — and price retraced through ours and not his. He was fine and we
        took the stop on a trade that was working.

        The structural fix is entering on the bare call, which is now done.
        These two are the belt:

          slack_pips        put the stop that far on the LOSING side of entry
                            rather than exactly at it. An exact-entry stop is
                            already a small loss once the spread is paid, and
                            it sits precisely where noise lives.
          min_profit_pips   refuse to move the stop at all until price is that
                            far in profit. Below it, "breakeven" means placing
                            a stop on top of the market, which is not risk-free,
                            it is an instant exit.

        Both default to 0, so a channel that sets neither behaves exactly as
        before.
        """
        be = channel.breakeven or {}
        p = channel.parser or {}
        pip = float(p.get("pip_value", 0.1))
        slack = float(be.get("slack_pips", 0) or 0) * pip
        min_profit = float(be.get("min_profit_pips", 0) or 0) * pip

        modified, deferred, skipped = 0, 0, 0
        for sig_row in self.db.get_open_signals(channel.id, signal.symbol):
            direction = str(sig_row["direction"] or "").lower()
            for pos in self.db.get_open_positions(sig_row["signal_id"]):
                if not (pos["ticket"] and pos["entry_price"]):
                    skipped += 1
                    continue
                entry = float(pos["entry_price"])
                want = entry
                if slack and direction in ("buy", "sell"):
                    want = entry - slack if direction == "buy" else entry + slack
                if min_profit and direction in ("buy", "sell"):
                    live = await self.bridge.get_price(
                        signal.symbol or channel.symbol, direction)
                    if live:
                        moved = (float(live) - entry) if direction == "buy" \
                            else (entry - float(live))
                        if moved < min_profit:
                            deferred += 1
                            logger.info(
                                "[EXECUTOR] %s: breakeven deferred on ticket %s "
                                "— only %.1f pips in profit, needs %.0f",
                                channel.name, pos["ticket"], moved / pip,
                                min_profit / pip)
                            continue
                if await self.bridge.modify_position(
                        pos["ticket"], round(want, 3),
                        float(pos["tp_price"] or 0)):
                    modified += 1

        if not modified and not deferred:
            await self._notify(
                f"⚠️ Breakeven: no open positions for {channel.name}")
            return
        note = ""
        if slack:
            note += f"\nStop set {slack / pip:.0f} pips beyond entry so noise " \
                    f"does not take it."
        if deferred:
            note += f"\n<b>{deferred}</b> left alone — not yet " \
                    f"{min_profit / pip:.0f} pips in profit, so a stop at entry " \
                    f"would sit on the market."
        await self._notify(
            f"⚖️ <b>Breakeven</b> — {channel.name}\n"
            f"{modified} position(s) updated.{note}")

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

        # "Close the gold buys here with loss" names ONE side. The parser
        # captures it and the adapter carries it; honouring it here is what
        # makes a directional close safe to detect at all. Without this the
        # instruction would flatten the other side of the book too.
        want_dir = (getattr(signal, "direction", None) or "").lower() or None
        if want_dir:
            kept = [r for r in sig_rows
                    if (r["direction"] or "").lower() == want_dir]
            if len(kept) != len(sig_rows):
                logger.info("[EXECUTOR] %s: directional close — %d of %d open "
                            "signal(s) are %s, the rest are left alone",
                            channel.name, len(kept), len(sig_rows), want_dir.upper())
            sig_rows = kept

        if not sig_rows:
            await self._notify(f"⚠️ Close: no open signals for {channel.name}"); return

        closed = 0
        failed = 0
        for row in sig_rows:
            row_failed = 0
            for pos in self.db.get_open_positions(row["signal_id"]):
                if not pos["ticket"]:
                    continue
                if not await self.bridge.close_position(pos["ticket"]):
                    failed += 1; row_failed += 1
                    logger.error("[EXECUTOR] close FAILED on ticket=%s (signal=%s) "
                                 "— leaving it open in the book",
                                 pos["ticket"], row["signal_id"])
                    continue
                # The realised P&L was hardcoded to 0.0 here, so every
                # operator-instructed close counted as a scratch: the drawdown
                # breaker never saw it, the account guard never saw it, and the
                # per-channel ledger never moved. Ask the broker what it was.
                pnl = 0.0
                try:
                    deal = await self.bridge.get_deal_history(pos["ticket"])
                    if deal and deal.get("status") == "success":
                        for key in ("net_profit", "total_profit", "profit"):
                            v = deal.get(key)
                            if v is not None:
                                pnl = float(v); break
                except Exception as e:
                    logger.error("[EXECUTOR] no deal history for ticket=%s after "
                                 "a manual close: %s", pos["ticket"], e)
                self.db.update_position_closed(pos["ticket"], pnl, "manual")
                self.db.book_realised(channel.id, pnl, "manual close")
                trades_log.info(
                    f"CLOSE_MANUAL channel={channel.name} "
                    f"ticket={pos['ticket']} signal={row['signal_id']} pnl={pnl:.2f}"
                )
                closed += 1
            # Only declare the signal closed if every leg actually closed. It
            # used to be set unconditionally, so a bridge timeout left the
            # positions live at the broker while the signal left
            # get_open_signals - and the operator's follow-up "close all" then
            # answered "no open signals" with the trade still on.
            if row_failed == 0:
                self.db.update_signal_status(row["signal_id"], "closed", "manual")
            else:
                logger.warning("[EXECUTOR] signal %s kept OPEN — %d leg(s) would "
                               "not close", row["signal_id"], row_failed)

        await self._notify(
            f"🔴 <b>{'Close All' if close_all else 'Close'}</b> — {channel.name}\n"
            f"Closed {closed} position(s)."
            + (f"\n⚠️ <b>{failed} would NOT close</b> and are still open at the "
               f"broker. Close them by hand." if failed else ""))

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
                    # Record what is LEFT. Without this the next partial
                    # measured its fraction against the original size again,
                    # so two "close 50%" instructions closed the whole
                    # position - and the "cannot be split" guard above
                    # compared against a stale full size and never fired.
                    # bare_trade_watcher already does this; the executor did not.
                    try:
                        self.db.update_position_lot(pos["ticket"],
                                                    round(full - want, 2))
                    except Exception as e:
                        logger.error("[EXECUTOR] could not record the remaining "
                                     "lot on ticket=%s: %s", pos["ticket"], e)
                    try:
                        _d = await self.bridge.get_deal_history(pos["ticket"])
                        if _d and _d.get("status") == "success":
                            for _k in ("net_profit", "total_profit", "profit"):
                                _v = _d.get(_k)
                                if _v is not None:
                                    # RUNNING TOTAL, not this slice: the EA sums
                                    # every closing deal on the position id, so
                                    # the second partial's reply still contains
                                    # the first. Book the increment only.
                                    self.db.book_position_cumulative(
                                        pos["ticket"], channel.id, float(_v),
                                        "partial close")
                                    break
                    except Exception:
                        pass
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
        """Move the stop on the trade the operator meant, not on all of them.

        This used to iterate every open signal on the channel with no
        reply_to_id scoping (which _handle_close honours), no direction filter,
        and no never-worsen check (which _auto_breakeven has). One "SL to 4350"
        was therefore applied to every open trade, could land on the wrong side
        of an opposite-direction position, and could WIDEN a stop that had
        already been tightened to breakeven.
        """
        if not signal.new_sl:
            return
        new_sl = float(signal.new_sl)

        # Prefer the signal the operator replied to.
        rows = []
        if getattr(signal, "reply_to_id", None):
            row = self.db.get_signal_by_message(channel.id, signal.reply_to_id)
            if row:
                rows = [row]
        if not rows:
            rows = self.db.get_open_signals(channel.id, signal.symbol)
            # With no reply and more than one live trade there is no way to know
            # which one he means, and guessing moves a stop on a trade he was
            # not talking about. Only act when it is unambiguous, or when the
            # direction he named picks exactly one out.
            want_dir = (getattr(signal, "direction", None) or "").lower()
            if want_dir:
                rows = [r for r in rows
                        if str(r["direction"]).lower() == want_dir]
            if len(rows) > 1:
                await self._notify(
                    f"⚠️ <b>SL → {new_sl:g} not applied</b> — {channel.name}\n"
                    f"{len(rows)} trades are open and the instruction did not "
                    f"reply to one of them. Moving them all could put a stop on "
                    f"the wrong side of an opposite trade, so nothing was "
                    f"changed. Reply to the signal, or name the direction.")
                return

        modified = skipped = 0
        for row in rows:
            direction = str(row["direction"] or "").lower()
            for pos in self.db.get_open_positions(row["signal_id"]):
                if not pos["ticket"]:
                    continue
                entry = float(pos["entry_price"] or 0)
                cur_sl = float(pos["stop_loss"] or 0)
                # Never through the market's own side of the entry, and never
                # backwards. A "correction" that widens a stop is not one.
                if direction == "buy":
                    if new_sl >= entry > 0 or (cur_sl and new_sl < cur_sl):
                        skipped += 1; continue
                elif direction == "sell":
                    if 0 < entry <= new_sl or (cur_sl and new_sl > cur_sl):
                        skipped += 1; continue
                if await self.bridge.modify_position(
                        pos["ticket"], new_sl, float(pos["tp_price"] or 0)):
                    self.db.update_position_sl(int(pos["ticket"]), new_sl)
                    modified += 1
        await self._notify(
            f"🛑 <b>SL → <code>{new_sl:.2f}</code></b> — {channel.name}\n"
            f"{modified} position(s) updated."
            + (f"\n{skipped} left alone (it would have widened the stop or put "
               f"it the wrong side of entry)." if skipped else ""))

    # ── Helper ─────────────────────────────────────────────────────────────────
    def _tp_passed(self, direction: str, price: float, tp: float) -> bool:
        """Has price already gone through this TARGET? Buy targets sit above."""
        return price >= tp if direction == "buy" else price <= tp

    def _sl_wrong_side(self, direction: str, fill: float, sl: float) -> bool:
        """Is this STOP on the wrong side of the fill?

        The mirror image of _tp_passed, and not interchangeable with it. A
        buy's stop belongs BELOW the fill and its target ABOVE; asking the
        target question about a stop answers "yes" for every correct stop,
        which is what silently disabled bare upgrades for a week.
        """
        return sl >= fill if direction == "buy" else sl <= fill

    async def _notify(self, msg: str):
        if self.notifier:
            try:
                await self.notifier.send(msg)
            except Exception as e:
                logger.debug(f"[EXECUTOR] notify error: {e}")