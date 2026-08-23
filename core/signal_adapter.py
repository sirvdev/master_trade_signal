"""
core/signal_adapter.py
======================
Translates core.signal_parser.ParsedMessage into core.ai_parser.ParsedSignal,
which is the contract core/signal_executor.py already speaks.

Why an adapter instead of changing the executor:
the executor's dispatch, idempotency, lot sizing, drawdown halting and
notification paths are all keyed off ParsedSignal. Rewriting them to a new
shape would put every one of those behaviours back into "unverified". The
deterministic parser is the part we want to change; the execution path is not.

The single most dangerous mismatch this file exists to prevent:
    signal_parser emits direction "BUY" / "SELL"
    signal_executor compares  direction == "buy"
`_tp_passed()` is `price >= tp if direction == "buy" else price <= tp`, so an
unconverted "BUY" silently takes the sell branch and every take profit on every
long is classified backwards. It fails quietly and it fails on all of them.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from core.ai_parser import ParsedSignal
from core.signal_parser import Intent, OrderType, ParsedMessage

logger = logging.getLogger(__name__)

# Off until the EA is recompiled. See _entry_type().
ALLOW_STOP_ORDERS = os.getenv("ALLOW_STOP_ORDERS", "false").lower() == "true"

# ParsedMessage.intent -> ParsedSignal.signal_type
_INTENT_MAP = {
    Intent.NEW_SIGNAL:     "entry",
    Intent.CANCEL_PENDING: "cancel_pending",   # handler added to the executor
    Intent.CLOSE_ALL:      "close_all",
    Intent.CLOSE_PARTIAL:  "close_partial",    # handler added to the executor
    Intent.MOVE_SL_BE:     "breakeven",
    Intent.MOVE_SL_PRICE:  "sl_correction",
    Intent.STATUS_REPORT:  "tp_hit",
}

# Intents that should fall through to the AI parser rather than be dropped.
_FALLBACK_INTENTS = (Intent.UNKNOWN,)


def should_fallback(res: ParsedMessage) -> bool:
    """True when the deterministic layer abstained and ai_parser should try."""
    return res.intent in _FALLBACK_INTENTS


def adapt_all(res: ParsedMessage, channel_symbol: str,
              is_reply: bool = False,
              reply_to_id: Optional[int] = None) -> list[ParsedSignal]:
    """Every executable instruction in the message, in the order to run them.

    Usually one. A message like "Take some profits now and set breakeven ok"
    carries two, and running only the first threw away half of what the
    operator asked for. The partial runs before the breakeven so the stop is
    moved on what is actually left open.
    """
    out = []
    for r in (res, res.follow_up):
        if r is None:
            continue
        sig = adapt(r, channel_symbol, is_reply=is_reply, reply_to_id=reply_to_id)
        if sig is not None:
            out.append(sig)
    return out


def adapt(res: ParsedMessage, channel_symbol: str,
          is_reply: bool = False,
          reply_to_id: Optional[int] = None) -> Optional[ParsedSignal]:
    """ParsedMessage -> ParsedSignal, or None if there is nothing to execute.

    Returning None is not an error. It is the correct outcome for chatter, for
    an intent the deterministic layer refused, and for anything the executor has
    no handler for. The caller logs and moves on.
    """
    if res.intent in _FALLBACK_INTENTS:
        return None

    if res.intent == Intent.CHATTER:
        return None

    if res.rejected_reasons:
        logger.info(
            "[ADAPTER] refused %s: %s",
            res.intent.value, "; ".join(res.rejected_reasons))
        return None

    stype = _INTENT_MAP.get(res.intent)
    if stype is None:
        # MODIFY_TP and ADD_ENTRY have no executor handler. Do not invent one.
        logger.warning("[ADAPTER] no executor handler for intent %s; ignoring",
                       res.intent.value)
        return None

    out = ParsedSignal(
        signal_type = stype,
        raw_text    = res.signal.raw_text if res.signal else "",
        symbol      = channel_symbol,
        confidence  = res.confidence,
        is_reply    = is_reply,
        reply_to_id = reply_to_id,
        warnings    = list(res.notes),
    )

    if res.signal is not None:
        s = res.signal
        # ── the casing conversion this module exists for ──────────────────────
        out.direction = s.direction.lower() if s.direction else None
        out.stop_loss = s.sl
        out.take_profits = list(s.tps)
        out.has_runner = s.has_open_runner
        out.entry_low, out.entry_high = s.entry_low, s.entry_high
        out.is_zone = bool(s.is_zone)

        entry_type = _entry_type(s)
        if entry_type == "stop" and not ALLOW_STOP_ORDERS:
            # PythonFileBridge only learned ORDER_TYPE_*_STOP in v2.503. Sending
            # one to an older build returns "Invalid order type" and the entry
            # is lost anyway, so refuse here where the reason is legible.
            # Recompile the EA, then set ALLOW_STOP_ORDERS=true.
            logger.warning(
                "[ADAPTER] %s needs a stop order. ALLOW_STOP_ORDERS is off "
                "(recompile PythonFileBridge v2.503+ first). Skipped.",
                s.order_type.value)
            return None
        out.entry_type = entry_type
        # entry_price is the LIMIT price. A market order must leave it None so
        # the executor uses the live price instead of the operator's quote.
        out.entry_price = s.entry if entry_type in ("limit", "stop") else None
        if s.needs_market_entry:
            out.warnings.append(
                "market fill: re-check geometry against the fill price")

    if res.action is not None:
        a = res.action
        if res.intent == Intent.MOVE_SL_PRICE:
            out.new_sl = a.price
        if res.intent == Intent.CLOSE_PARTIAL:
            out.close_fraction = a.fraction or 0.5
        if res.intent == Intent.CANCEL_PENDING and a.direction:
            out.direction = a.direction.lower()

    return out


def _entry_type(s) -> Optional[str]:
    """ParsedSignal.entry_type, or None when the order cannot be placed."""
    ot = s.order_type
    if ot == OrderType.MARKET:
        return "market"
    if ot in (OrderType.BUY_LIMIT, OrderType.SELL_LIMIT):
        return "limit"
    if ot in (OrderType.BUY_STOP, OrderType.SELL_STOP):
        return "stop"
    # AUTO: unresolved because no live price was supplied at parse time.
    # A posted entry means a resting order; no entry means fill at market.
    return "limit" if s.entry is not None else "market"
