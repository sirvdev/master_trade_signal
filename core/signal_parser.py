"""
core/signal_parser.py
=====================
Deterministic, per-channel Telegram signal parser.
Design contract
---------------
1. This layer is AUTHORITATIVE for numbers. An LLM is never asked for a price.
2. Every parse returns a ParsedMessage with an explicit `intent` and a
   `confidence` in [0,1]. The executor acts only on intents it recognises AND
   whose confidence clears the channel's `min_confidence`.
3. Anything the deterministic layer cannot classify is returned as
   intent=UNKNOWN so an optional AI fallback can look at it. UNKNOWN is never
   auto-executed.
4. Validation is fail-closed. A signal that fails geometry, range, or
   freshness checks is returned with intent=NEW_SIGNAL and
   `rejected_reasons` non-empty; the executor must skip it.
Public API
----------
    parser = SignalParser(channel_cfg)          # channel_cfg = one entry from channels.json
    result = parser.parse(text, msg_id=..., msg_dt=..., market_price=...)
    result.intent, result.signal, result.action, result.rejected_reasons
Multi-message signals (a channel that posts direction, then SL, then TPs as
separate messages) are handled by SignalAssembler, which wraps SignalParser.
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional
# ══════════════════════════════════════════════════════════════════════════════
# Intents
# ══════════════════════════════════════════════════════════════════════════════
class Intent(str, Enum):
    NEW_SIGNAL     = "NEW_SIGNAL"       # open a position / place a pending order
    ADD_ENTRY      = "ADD_ENTRY"        # add another layer to an existing direction
    CANCEL_PENDING = "CANCEL_PENDING"   # delete unfilled pending orders
    CLOSE_ALL      = "CLOSE_ALL"        # close every open position for this channel
    CLOSE_PARTIAL  = "CLOSE_PARTIAL"    # close a fraction (pct in Action.fraction)
    MOVE_SL_BE     = "MOVE_SL_BE"       # move stop to entry (+ buffer)
    MOVE_SL_PRICE  = "MOVE_SL_PRICE"    # move stop to an explicit price
    MODIFY_TP      = "MODIFY_TP"        # replace TP ladder
    PRE_SIGNAL     = "PRE_SIGNAL"       # "Gold buy now" -> get in before the levels
    STATUS_REPORT  = "STATUS_REPORT"    # "TP2 hit, 130 pips" -> log only, no action
    CHATTER        = "CHATTER"          # greetings, promos, member talk -> ignore
    UNKNOWN        = "UNKNOWN"          # deterministic layer abstained
class OrderType(str, Enum):
    MARKET     = "MARKET"
    BUY_LIMIT  = "BUY_LIMIT"
    SELL_LIMIT = "SELL_LIMIT"
    BUY_STOP   = "BUY_STOP"
    SELL_STOP  = "SELL_STOP"
    AUTO       = "AUTO"      # resolve against live price at execution time
# ══════════════════════════════════════════════════════════════════════════════
# Result objects
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    direction: str                        # "BUY" | "SELL"
    order_type: OrderType = OrderType.AUTO
    entry_low: Optional[float] = None     # zone bound (min)
    entry_high: Optional[float] = None    # zone bound (max)
    entry: Optional[float] = None         # resolved single entry price
    sl: Optional[float] = None
    tps: list[float] = field(default_factory=list)
    has_open_runner: bool = False         # a "TP: open" leg with no fixed target
    is_zone: bool = False
    needs_market_entry: bool = False   # MARKET order: entry unknown until fill
    # The message named an instrument other than the one this channel trades.
    foreign_symbol: Optional[str] = None
    raw_text: str = ""
    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "direction": self.direction,
            "order_type": self.order_type.value,
            "entry": self.entry, "entry_low": self.entry_low,
            "entry_high": self.entry_high, "sl": self.sl, "tps": self.tps,
            "has_open_runner": self.has_open_runner, "is_zone": self.is_zone,
            "needs_market_entry": self.needs_market_entry,
        }
@dataclass
class Action:
    """Management instruction attached to a non-NEW_SIGNAL intent."""
    fraction: Optional[float] = None       # 0.5 for "close half"
    price: Optional[float] = None          # explicit SL price for MOVE_SL_PRICE
    scope: str = "channel_all"             # channel_all | channel_latest
    direction: Optional[str] = None        # "cancel all BUY orders"
    def as_dict(self) -> dict:
        return {"fraction": self.fraction, "price": self.price,
                "scope": self.scope, "direction": self.direction}
@dataclass
class ParsedMessage:
    intent: Intent
    confidence: float = 0.0
    signal: Optional[Signal] = None
    action: Optional[Action] = None
    rejected_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    channel_id: str = ""
    msg_id: Optional[int] = None
    # A second instruction carried by the same message, e.g. "Take some profits
    # now and set breakeven ok". One message, two things the operator asked for.
    # Emitting only the first silently discarded the other; which one got
    # discarded depended on nothing more principled than pattern order.
    follow_up: Optional["ParsedMessage"] = None
    @property
    def executable(self) -> bool:
        return not self.rejected_reasons and self.intent not in (
            Intent.CHATTER, Intent.STATUS_REPORT, Intent.UNKNOWN)
    def as_dict(self) -> dict:
        return {
            "channel_id": self.channel_id, "msg_id": self.msg_id,
            "intent": self.intent.value, "confidence": round(self.confidence, 3),
            "executable": self.executable,
            "signal": self.signal.as_dict() if self.signal else None,
            "action": self.action.as_dict() if self.action else None,
            "rejected_reasons": self.rejected_reasons, "notes": self.notes,
        }
# ══════════════════════════════════════════════════════════════════════════════
# Text normalisation
# ══════════════════════════════════════════════════════════════════════════════
# Telegram "styled" alphabets (mathematical bold, etc.) collapse to ASCII under
# NFKC. Without this, "GOLD Sell" never matches /gold/i.
_MD_NOISE = re.compile(r"(\*\*|__|~~|\|\|)")
# Markdown that opens or closes inside a number: "437**1" -> "4371".
# Requires a digit on both sides, so it can never join two real values.
#
# ASTERISKS AND TILDES ONLY. "__" is NOT in this class and must never be,
# because two channels use it as a ZONE SEPARATOR, not as markdown:
#   John Wick FX  "GOLD BUY NOW 4816__4814"  = the zone 4814-4816
#   Emily Pips    "Price Open @ 4097_4000"
# Joining those produces 48164814, which is outside price_max, so the entry
# vanishes. Adding "__" here cost 100 John Wick signals and 13 Mr Zack
# signals their entry price when it was tried on 2026-08-30. The same two
# characters mean opposite things on different channels and no global rule
# can serve both; the one "Sl 46__75" in the corpus stays unread on purpose.
_MD_SPLIT_NUM = re.compile(r"(?<=\d)(?:\*\*|~~)(?=\d)")
# "4816__4814" / "4097_4000" -> a canonical zone separator. See normalize().
_ZONE_UNDERSCORE = re.compile(r"(?<=\d)_+(?=\d)")
_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿͏︀-️]")
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"', 0x00a0: " "}
# Homoglyph repair: several channels post "HlT" / "PlPS" with a lowercase L
# standing in for a capital I to dodge keyword filters.
_HOMOGLYPH_WORDS = {
    r"\bHlT\b": "HIT", r"\bPlPS\b": "PIPS", r"\bPROFlT\b": "PROFIT",
    r"\bPOEN\b": "OPEN", r"\bOPNE\b": "OPEN", r"\bXAUUD\b": "XAUUSD",
    r"\bBUYY\b": "BUY", r"\bSELLL?\b": "SELL",
}
def normalize(text: Optional[str]) -> str:
    """NFKC-fold, strip markdown/zero-width/emoji noise, repair known homoglyphs."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = t.translate(_DASHES).translate(_QUOTES)
    t = _ZERO_WIDTH.sub("", t)
    # A bold run that OPENS OR CLOSES INSIDE A NUMBER. Telegram lets an
    # operator bold part of a price, and the export preserves it literally:
    #   "SL 437**1"   "Sl :4**009"   "Sl 46__75"   "STOP Loss 42**10"
    # _MD_NOISE below turns the marker into a space, which is right between
    # words and fatal between digits - 4009 became "4 009" and the stop was
    # then unreadable, so the whole signal was refused for having no stop.
    # Deleting rather than spacing is only safe when a digit sits on BOTH
    # sides, which is why this runs first and separately. 15 messages in the
    # 21,738-message corpus; every one of them a stop or a target.
    t = _MD_SPLIT_NUM.sub("", t)
    # A digit-flanked underscore run is a ZONE SEPARATOR, not markdown:
    #   John Wick FX  "GOLD BUY NOW 4816__4814"  = the zone 4814-4816
    #   Emily Pips    "Price Open @ 4097_4000"
    # _MD_NOISE below turns "__" into a space, which erases the pairing and
    # left both channels reading a single entry at one edge. Canonicalising
    # to "-" first hands the pair to the zone rule intact, and _MD_NOISE
    # never sees it. Deleting the underscore instead would join the two
    # prices into 48164814 - the regression of 2026-08-30.
    t = _ZONE_UNDERSCORE.sub("-", t)
    t = _MD_NOISE.sub(" ", t)
    for pat, rep in _HOMOGLYPH_WORDS.items():
        t = re.sub(pat, rep, t, flags=re.I)
    # An underscore is a WORD character, so "GOLD_BUY NOW 4610/4608" never
    # matched \bbuy\b and the whole channel parsed as chatter: 1432 messages,
    # zero signals. Split underscores that sit between letters.
    t = re.sub(r"(?<=[A-Za-z])_(?=[A-Za-z])", " ", t)
    # Drop emoji / pictographs but keep the space so tokens stay separated.
    t = "".join(" " if unicodedata.category(c) == "So" else c for c in t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()
# ══════════════════════════════════════════════════════════════════════════════
# Lexicons
# ══════════════════════════════════════════════════════════════════════════════
_BUY  = r"(?:buy|long|bull(?:ish)?)"
_SELL = r"(?:sell|short|bear(?:ish)?)"
RE_BUY   = re.compile(rf"\b{_BUY}\b", re.I)
RE_SELL  = re.compile(rf"\b{_SELL}\b", re.I)
# Arrow separators. Green Pips Zone (-1002145295870) writes every ticket as
# "Entry → 4322 ... SL → 4332". Without the arrow in the separator class the
# stop is unreadable, requires_sl refuses the signal, and 116 of that channel's
# 261 signals — 44% — never trade. The arrow only ever appears between a label
# and its price, so widening the class cannot capture anything else. Both the
# real arrow (U+2192) and the emoji arrow (U+27A1, usually followed by a
# variation selector) are in use; ">" is included for the ASCII spelling.
_ARROW = "→➡️>"
RE_SL = re.compile(
    r"(?:stop\s*-?\s*loss|\bstoploss\b|\bs\s*[./]\s*l\b|\bsl\b|\bstop\b)"
    r"\s*(?:\(\s*(?:sl|s/l)\s*\))?"
    r"\s*(?:place[d]?\s*(?:at)?)?"
    rf"[\s:.\-=@()/_{_ARROW}]*"
    r"(\d{3,5}(?:\.\d{1,3})?)"
    r"(?!\s*\+?\s*(?:pips?|points?|%))", re.I)
# "TP1: 4416", "TP 2:4412", "TP 3.4408", "TP. 4415", "@TP: 4302",
# "Tp 1(R) 4568", "Target 1: 4063.3", "TP5 OPEN"
#
# The optional index group MUST use a (?!\d) lookahead. Without it, "TP 4360"
# parses as index=4, price=360, and the target is silently dropped for being
# out of range. That failure mode is invisible: the order still goes out, just
# with no take profit attached.
#
# The optional "at" group is load-bearing for GTMO, whose documented split-post
# format is literally "TP1 at 4407". Without it RE_TP does not match, the TP
# line is not stripped by _extract_entry, and 4407 is then read as the ENTRY.
# "TP hit at 4407" still does not match, because "hit" is not in the separator
# class and the alternation is anchored immediately after the index.
RE_TP = re.compile(
    r"(?:\btp|\bt\s*/\s*p|take\s*profit|\btarget)"
    r"\s*(?:\(\s*\d{1,2}\s*\)|\d{1,2}(?!\d))?"
    r"\s*(?:@|\bat\b)?"
    rf"[\s:.\-=@(){_ARROW}]*"
    # "open" and "free" both mean an UNCAPPED leg. Mr FullMargin FX writes
    # "TP   : Free!" on all 180 of his signals, so without this his third leg
    # was silently dropped and a two-leg trade was opened where he posted
    # three. This only matches where the TP pattern has ALREADY matched, so
    # "risk free" and "join our free group" cannot reach it.
    r"(\d{3,5}(?:\.\d{1,3})?|open|free)"
    r"(?!\s*\+?\s*(?:pips?|points?|%))", re.I)
RE_TP_EVERY = re.compile(
    r"\btp\b[^\n]{0,20}\bevery\b\s*(\d{2,4})\s*pips?", re.I)
# A line that is nothing but a "Targets:" header. See _targets_block.
_RE_TARGETS_HDR = re.compile(r"^\W*(?:targets?|tps?)\W*$", re.I)
RE_ENTRY_LABEL = re.compile(
    r"(?:\bentry\b|\bentries\b|\benter\b|open\s+new\s+positions?\s+at|\bzone\b|\bat\b|@)"
    rf"\s*(?:zone)?\s*[:.\-={_ARROW}]?\s*", re.I)
RE_LIMIT   = re.compile(r"\b(limit|pending)\b", re.I)
RE_STOPORD = re.compile(r"\bbuy\s+stop\b|\bsell\s+stop\b|\bstop\s+order\b", re.I)
# The word "stop" is overloaded. In "Gold buy stop 4420-4425" it names the ORDER
# TYPE and the number after it is the ENTRY, not the stop loss. RE_SL matches
# `\bstop\b` and would capture 4420 as the SL, discarding the real stop posted
# two lines down. The signal then passes every gate with a stop distance several
# times too tight, so position sizing comes out correspondingly oversized.
# _extract_sl skips any RE_SL match overlapping one of these spans.
RE_STOP_ORDER_PHRASE = re.compile(
    r"\b(?:buy|sell)\s*[-/]?\s*stop\b|\bstop\s+(?:order|entry|limit)\b", re.I)


def _sl_matches(t: str):
    """Split RE_SL hits into (n_skipped, genuine_matches).

    A hit is discarded when it overlaps a stop-ORDER phrase, because there the
    number belongs to the entry, not the stop loss.
    """
    order = [m.span() for m in RE_STOP_ORDER_PHRASE.finditer(t)]
    good, skipped = [], 0
    for m in RE_SL.finditer(t):
        if any(m.start() < ob and oa < m.end() for oa, ob in order):
            skipped += 1
            continue
        good.append(m)
    return skipped, good
RE_MARKET  = re.compile(r"\b(now|market\s*(?:execution|order)?|instant)\b", re.I)

# ── Bare directional calls ("pre-signals") ────────────────────────────────────
# Several operators post the direction first and the levels a minute or two
# later: GTMO's "Gold buy now" (2026-08-25 09:32:22Z) was followed by a 100-pip
# move before any stop or target existed, and Lion Trading's "Sell gold"
# (07:36:16Z) came 87 seconds before the full signal. By the time the levels
# arrive the price has left, so the trade either fills worse than the operator's
# or is refused for slippage outright. Taking the bare call is the only way to
# be in at the operator's price.
#
# The detection has to be strict, because a false positive opens a real
# position with no levels. The rule is: after stripping punctuation, EVERY word
# must be a direction, the instrument, or a known filler. Anything else — a
# number, a name, a verb that is not on the list — disqualifies it. That is
# what separates "Gold buy now" from "when you say buy 1 minutes next
# flyyyyying", which is a member cheering and contains the word "buy".
RE_PRE_INSTRUMENT = re.compile(r"^(gold|xau|xauusd\w*|xau/?usd)$", re.I)
_PRE_FILLER = {
    # timing / imperative
    "now", "here", "soon", "ready", "incoming", "active", "running", "live",
    "again", "another", "next", "more",
    # address
    "guys", "guy", "team", "everyone", "all", "please", "pls", "friends",
    # hedging the operator adds to a heads-up
    "slowly", "slow", "small", "careful", "carefully", "light", "lightly",
    "scalping", "scalp", "scalp​", "quick", "fast",
    "high", "low", "risk", "risky", "only", "some",
    # shape words that carry no levels
    "zone", "zones", "area", "setup", "signal", "signals", "alert", "entry",
    "entries", "position", "positions", "order", "orders", "market", "trade",
    "trades", "idea",
    # connective filler
    "lets", "let", "us", "we", "im", "i", "am", "going", "go", "to", "the",
    "a", "on", "in", "at", "for", "and", "is", "it", "this", "that", "with",
    "from", "of", "s", "will", "be", "can", "may", "look", "looking",
}
# Management vocabulary
RE_CANCEL_OBJECT_EXCLUDE = re.compile(
    r"\b(?:delete|remove)\b[^\n]{0,15}\b(?:message|post|comment|chat|channel|link|it\s+in)\b"
    r"|\b(?:i|we)\s*(?:will|'ll|am\s+going\s+to|\s+gonna)\s+(?:delete|remove|cancel)\b", re.I)
RE_CANCEL = re.compile(
    r"\b(cancel|cancelled|canceled|delete|remove)\b[^\n]{0,40}?"
    r"\b(order|orders|pending|limit|limits|template|buy|buys|sell|sells|this|all|it)\b"
    r"|\b(cancel|cancelled|canceled)\b\s*$"
    r"|\b(cancel|delete)\s+(all|this|it|everything)\b", re.I)
RE_CLOSE_ALL = re.compile(
    r"\b(?:close|exit|flatten)\s+(?:all|every|everything|the\s+lot|out)\b"
    r"|\ball\s+(?:closed|out)\b"
    r"|\bclose\s+(?:all\s+)?(?:trades?|positions?|profits?|entries|orders?)\b"
    r"|\bclose\s+(?:the\s+)?(?:trade|position)\s+(?:now|immediately)\b"
    r"|\bclose\s+(?:your\s+)?positions?\s+immediately\b"
    r"|\blet'?s\s+close\b"
    r"|(?:^|[.!?\n]\s*)(?:let'?s\s+|please\s+|now\s+)?close\s+(?:it|now)\b(?!\s+with)"
    r"|\bclose\s+remaining\s+position\b"
    # Gold Hunter's terse exit: "Full closed". Word order matters. A member
    # writing "Closed full profit, this one PERFECT" is the reverse order and
    # must not match, and "I fully closed" is caught by the first-person guard.
    r"|\bfull(?:y)?\s+closed?\b"
    # Lion, 2026-08-19: "Close with small loss it's not good anymore".
    r"|\bclose\s+with\s+(?:a\s+)?(?:small|big|little|tiny)?\s*(?:loss|profit|gain)\b",
    re.I)

# A close instruction that names the DIRECTION, with the instrument and/or a
# noun in between: "Close gold buy trade", "Close the gold buys here with
# loss", "Gold sell trade close". RE_CLOSE_ALL needs its noun immediately
# after "close", so all of these fell through to UNKNOWN and then to the AI.
#
# The direction is captured and HONOURED downstream: _handle_close filters the
# open signals by it. Emitting this as an undirected close-all would shut the
# other side of the book on an instruction that never mentioned it, which is
# worse than missing the message.
RE_CLOSE_DIRECTIONAL = re.compile(
    r"\bclose\s+(?:the\s+|all\s+|your\s+|our\s+)?"
    r"(?:gold\s+|xau\s*/?\s*usd\s+)?"
    r"\b(buy|sell)s?\b"
    r"(?:\s+(?:trade|position|order)s?)?"
    r"|\b(?:gold\s+|xau\s*/?\s*usd\s+)?(buy|sell)s?\s+"
    r"(?:trade|position|order)s?\s+close(?:d)?\b",
    re.I)
# Management vocabulary that did NOT match any specific intent above. A short
# imperative carrying one of these is ambiguous, not chatter: "IF HAPPY CLOSE",
# "Breakeven hit gold now". Routing it to UNKNOWN hands it to the AI fallback,
# which has this channel's house style in its prompt. Silently calling it
# CHATTER threw the operator's instruction away with no second opinion.
RE_MGMT_SHAPED = re.compile(
    r"\b(?:close|closed|closing|cancel|delete|breakeven|break\s*even"
    r"|risk\s*-?\s*free|partial|partials|secure|flatten|exit)\b", re.I)
RE_CLOSE_PARTIAL = re.compile(
    r"\bclose\s+(?:some|part|partial(?:ly)?|a\s+few)\b"
    r"|\btake\s+partials?\b|\bpartial(?:s|ly)?\s+(?:out|close|profit)"
    # No trailing \b after the percent sign. "%" is a non-word character, so
    # \b there demands a word character next: "Close 50% position" and a
    # trailing "Close 50%" both failed to match, and Wealth Growth Lab's only
    # partial-close format was classified as chatter.
    r"|\bclose\s+half\b"
    r"|\bclose\s+(?:out\s+)?\d{1,3}\s*%"
    r"|\bclose\s+(?:the\s+)?(?:first|top|highest|lowest|last)\s+"
    r"(?:entry|entries|layer|layers|positions?)\b"
    r"|\bsecure\s+(?:half|some|part|your\s+first)\b"
    r"|\bcollect\s+now\b|\breduce\s+(?:my\s+|our\s+)?risk\b"
    # "lets take some more profits ok" (GTMO, 2026-08-19) and "take profit now"
    # (Gold Hunter). A bare "take profits" is NOT enough: it also appears inside
    # "Move SL to BE TAKE profits", which is a breakeven instruction, so the
    # imperative has to be marked by "let's" or by a trailing now/here/ok.
    r"|\blet'?s\s+take\s+(?:some\s+)?(?:more\s+)?profits?\b"
    r"|\btake\s+(?:some\s+)?(?:more\s+)?profits?\s+(?:now|here|ok)\b"
    r"|\btake\s+(?:some\s+)?profits?\s+(?:at|@)\s*\d{2,4}\s*pips?\b", re.I)
RE_BE = re.compile(
    r"\b(?:move|set|shift|pull|slide|go|adjust)\b[^\n]{0,25}"
    r"\b(?:b\.?/?e\.?|break\s*-?\s*even|breakeven|risk\s*-?\s*free|riskfree)\b"
    r"|\b(?:sl|stop\s*loss|stop)\s*(?:to|at|->|=)\s*"
    r"(?:b\.?/?e\.?|break\s*-?\s*even|breakeven)\b"
    r"|(?:^|[,;!.]\s*|\band\s+)(?:breakeven|break\s*even)\s+(?:now|please|everyone)\b"
    r"|\bgo\s+risk\s*-?\s*free\b", re.I)
# Verbs operators actually use for "move the stop". The list grew from the
# 2026-08-24 log: GTMO posted "Adjust SL +20 pips to 4630" and it came out as
# CHATTER, so the stop never moved. Two separate faults were in that one line —
# a pip qualifier sitting between the SL token and the price, and several
# common verbs missing entirely.
_SL_MOVE_VERB = (r"move|shift|adjust|set|trail|pull|change|update|amend|"
                 r"revise|put|bring|raise|lower|tighten|"
                 r"last\s+adjustment\s+of")
# "+20 pips", "20 points" — the operator says how far as well as where to.
_PIP_QUALIFIER = r"(?:[+\-]?\s*\d{1,3}\s*(?:pips?|points?|pts)\s*)?"
RE_MOVE_SL_PRICE = re.compile(
    r"\b(?:" + _SL_MOVE_VERB + r")\b[^\n]{0,25}"
    r"\b(?:sl|s/l|stop\s*loss|stop)\b\s*" + _PIP_QUALIFIER +
    r"(?:to|at|->|=|:)?\s*(\d{3,5}(?:\.\d{1,3})?)"
    # Verbless form: "SL to 4630". An explicit directional connector is
    # REQUIRED here. Allowing a bare "SL 4600" would turn the stop line of
    # every new signal into a stop-move instruction.
    r"|\b(?:sl|s/l|stop\s*loss)\b\s*" + _PIP_QUALIFIER +
    r"(?:to|->|=)\s*(\d{3,5}(?:\.\d{1,3})?)", re.I)
RE_TP_HIT = re.compile(
    r"\btp\s*\d?\b[^\n]{0,20}\b(hit|smashed|done|reached|secured|complete)"
    r"|\b(hit|smashed|reached)\b[^\n]{0,10}\btp\s*\d?\b"
    r"|\b\d{2,4}\s*\+?\s*pips?\b[^\n]{0,20}\b(profit|running|secured|done|locked)"
    r"|\ball\s+(?:take\s+)?(?:profits?|targets?)\s+(?:hit|completed|smashed)", re.I)
# Precision guards: these mark a message as a REPORT or a QUESTION, not an order.
RE_FIRST_PERSON_PAST = re.compile(
    r"\b(?:i|we)\s+(?:have\s+)?(?:just\s+)?(?:closed|exited|took|booked|"
    r"secured|cancelled|canceled)\b"
    r"|\bi'?m\s+out\b|\bi\s+am\s+out\b"
    r"|\bhas\s+been\s+closed\b|\bhave\s+(?:all\s+)?been\s+closed\b"
    r"|\bwas\s+closed\b|\bclosed\s+early\b"
    r"|\bmy\s+(?:trade|position|entries)\b[^\n]{0,20}\bclosed\b", re.I)
RE_QUESTION = re.compile(r"\?\s*$|\?\s*\n|\b(?:how much|are you|did you|who|"
                         r"anyone still)\b", re.I)
RE_CONDITIONAL = re.compile(
    r"\bif\s+you\s+(?:wish|want|like|prefer|dont|don'?t)\b"
    r"|\byou\s+can\b|\bfeel\s+free\b|\beither\b|\bor\s+set\b|\bif\s+anyone\b", re.I)
RE_PROMO = re.compile(
    r"https?://|t\.me/|\bvip\b|\bgiveaway\b|\bjoin\s+(?:now|my|us)\b|\bsubscribe\b"
    r"|\bebook\b|\bgumroad\b|\bdm\s+me\b|\bpm\s+me\b|\bregister\b|\bspots?\s+(?:open|left)\b"
    r"|\bcontact\b|\baccount\s+management\b|\bticket", re.I)
RE_POSSESSIVE_BE = re.compile(
    r"\b(?:my|his|her|their|our)\s+(?:sl\s+)?(?:b\.?/?e\.?|break\s*-?\s*even|breakeven)\b"
    r"|\b(?:hit|touched|reached)\s+(?:my\s+|the\s+)?(?:b\.?/?e\.?|break\s*-?\s*even|breakeven)\b",
    re.I)
RE_HYPOTHETICAL = re.compile(
    r"\bclose\s+to\s+(?:the\s+)?(?:first|tp|\d)"   # "running close to 250 pips"
    r"|\bclose\s+to\s+\d"
    r"|\bclosed\s+the\s+(?:week|day)\b"
    r"|\bcloses?\s+in\s+\d"                        # "This H1 closes in 9 minutes"
    r"|\bmarket\s+closed?\b|\bdoor\s+(?:is\s+)?closed?\b"
    r"|\bwhen\s+to\b|\bhow\s+to\b|\bknowing\s+when\b|\bwant\s+to\s+talk\b"
    r"|\blearn(?:ing)?\s+to\b|\bthe\s+key\s+is\b", re.I)
# ══════════════════════════════════════════════════════════════════════════════
# Parser
# ══════════════════════════════════════════════════════════════════════════════
class SignalParser:
    def __init__(self, channel_cfg: dict):
        self.cfg = channel_cfg
        p = channel_cfg.get("parser", {})
        self.p = p
        self.channel_id     = str(channel_cfg.get("id", ""))
        self.symbol         = channel_cfg.get("symbol", "XAUUSD")
        self.aliases        = {k.upper(): v for k, v in p.get("symbol_aliases", {}).items()}
        self.price_min      = float(p.get("price_min", 500.0))
        self.price_max      = float(p.get("price_max", 20000.0))
        self.requires_sl    = bool(p.get("requires_sl", True))
        self.default_sl_pips = p.get("default_sl_pips")
        self.pip_value      = float(p.get("pip_value", 0.1))   # XAUUSD: 1 pip = $0.10
        self.min_sl_dist    = float(p.get("min_sl_distance", 1.0))
        self.max_sl_dist    = float(p.get("max_sl_distance", 60.0))
        self.max_tps        = int(p.get("max_tps", 8))
        self.zone_fill      = p.get("zone_fill", "worst")
        # Widest "a - b" this channel could plausibly mean as one zone.
        # Above it the pair is a typo or two separate ideas and is collapsed
        # to the near edge. 30 is deliberately generous: the widest genuine
        # zone measured across the whole 21,738-message corpus is 20.
        self.max_zone_width = float(p.get("max_zone_width", 30.0))
        self.max_entry_dev  = float(p.get("max_entry_deviation", 6.0))
        # Applies ONLY to resting (limit/stop) orders. None = no bound, which is
        # the right default: a pending order is meant to sit away from the
        # market. Set it if you want a backstop against a mis-parsed entry.
        _mld = p.get("max_limit_distance")
        self.max_limit_dist = float(_mld) if _mld else None
        self.max_age_sec    = int(p.get("max_signal_age_sec", 300))
        self.min_conf       = float(p.get("min_confidence", 0.7))
        self.trust_first_person = bool(p.get("trust_operator_first_person", False))
        self.follow_conditional = bool(p.get("follow_conditional_close", False))
        self.tp_ladder_step = p.get("tp_ladder_pips")
        self.close_scope    = p.get("close_scope", "channel_all")
        self.allow_bare     = bool(p.get("allow_bare_signals", False))
        self.future_tolerance_sec = int(p.get("future_tolerance_sec", 120))
        # Longest message still treated as a possible terse instruction when it
        # carries a management verb but matches no intent. Above this it is prose.
        self.mgmt_ambiguous_max_chars = int(p.get("mgmt_ambiguous_max_chars", 40))
        # Refuse a signal naming an instrument this channel does not trade.
        self.refuse_foreign = bool(p.get("refuse_foreign_symbol", True))
        # Sender filtering. The export schema has no sender_id, so this cannot be
        # exercised in replay, but Telethon exposes it live at zero cost. With an
        # allowlist configured, a member typing "close all" can no longer flatten
        # the book. Empty list = unverified (current behaviour, replay-compatible).
        self.operator_ids   = {str(x) for x in p.get("operator_sender_ids", [])}
        self.require_sender = bool(p.get("require_sender_verification", False))
        self.assemble_max_chars   = int(p.get("assemble_max_fragment_chars", 64))
        # Bare directional calls. On by default: the whole point of watching a
        # scalping channel is to be in when the operator is, and several of
        # them post the direction before the levels.
        self.pre_signal          = bool(p.get("pre_signal", True))
        self.pre_signal_max_chars = int(p.get("pre_signal_max_chars", 64))
        # Its own floor: 0.70 is the score for a bare "Sell gold" with no
        # "now", which is still a real instruction. The channel's
        # min_confidence is tuned for full signals and would refuse it.
        self.pre_signal_min_conf  = float(p.get("pre_signal_min_confidence", 0.65))
        self.assemble_max_parts   = int(p.get("assemble_max_parts", 4))
    # ── public ────────────────────────────────────────────────────────────────
    MANAGEMENT_INTENTS = (Intent.CANCEL_PENDING, Intent.CLOSE_ALL,
                          Intent.CLOSE_PARTIAL, Intent.MOVE_SL_BE,
                          Intent.MOVE_SL_PRICE, Intent.MODIFY_TP)

    def _check_sender(self, res: ParsedMessage, sender_id) -> None:
        """Management intents move real money on positions that are already
        open. Suppression by phrasing is a weaker mechanism than filtering by
        sender and will fail on any member who happens to type 'close all'."""
        if res.intent not in self.MANAGEMENT_INTENTS:
            return
        if self.operator_ids:
            if sender_id is None:
                if self.require_sender:
                    res.rejected_reasons.append(
                        "management intent with no sender_id and "
                        "require_sender_verification=true")
                else:
                    res.notes.append("sender not supplied; allowlist not applied")
            elif str(sender_id) not in self.operator_ids:
                res.rejected_reasons.append(
                    f"sender {sender_id} is not in operator_sender_ids")
        elif self.require_sender:
            res.rejected_reasons.append(
                "require_sender_verification=true but operator_sender_ids is empty")
        else:
            res.notes.append("management intent accepted without sender verification")

    def parse(self, text: str, msg_id: Optional[int] = None,
              msg_dt: Optional[datetime] = None,
              market_price: Optional[float] = None,
              now: Optional[datetime] = None,
              sender_id=None) -> ParsedMessage:
        t = normalize(text)
        res = ParsedMessage(intent=Intent.CHATTER, channel_id=self.channel_id,
                            msg_id=msg_id)
        if not t:
            return res
        # 1. A complete/near-complete signal always wins over management keywords,
        #    because signal posts often contain the word "stop" and "close".
        sig, conf, notes = self._try_signal(t)
        if sig is not None:
            res.intent, res.signal, res.confidence = Intent.NEW_SIGNAL, sig, conf
            res.notes.extend(notes)
            self._validate(res, msg_dt=msg_dt, market_price=market_price, now=now)
            return res
        # 2. Management intents.
        mgmt = self._try_management(t)
        if mgmt is not None:
            intent, action, conf, notes = mgmt
            res.intent, res.action, res.confidence = intent, action, conf
            res.notes.extend(notes)
            if conf < self.min_conf:
                res.rejected_reasons.append(
                    f"confidence {conf:.2f} < min_confidence {self.min_conf:.2f}")
            self._check_sender(res, sender_id)
            self._attach_follow_up(res, t, sender_id)
            return res
        # 3. Pure outcome announcements -> log only.
        if RE_TP_HIT.search(t):
            res.intent, res.confidence = Intent.STATUS_REPORT, 0.9
            return res
        # 3b. Management-shaped but unmatched. Short, imperative, and carrying a
        #     management verb: hand it to the AI fallback rather than binning it.
        #     Excluded: anything that reads as a report or a question, which is
        #     what the overwhelming majority of these actually are.
        if (RE_MGMT_SHAPED.search(t)
                and len(t) <= self.mgmt_ambiguous_max_chars
                and not RE_PROMO.search(t)
                and not RE_QUESTION.search(t)
                and not RE_FIRST_PERSON_PAST.search(t)
                and not RE_TP_HIT.search(t)
                and not RE_HYPOTHETICAL.search(t)):
            res.intent, res.confidence = Intent.UNKNOWN, 0.0
            res.notes.append("management verb in a short imperative that matched "
                             "no intent; deferring to the AI fallback")
            return res
        # 3c. A bare directional call, posted ahead of the levels. This must sit
        #     ABOVE the len(t) < 25 rule below: "Sell gold" is ten characters
        #     and was being binned as chatter, which is how Lion Trading's
        #     87-second head start was thrown away on 2026-08-25.
        pre = self._try_pre_signal(t)
        if pre is not None:
            sig, conf = pre
            res.intent, res.signal, res.confidence = Intent.PRE_SIGNAL, sig, conf
            res.notes.append("bare directional call: no levels posted yet")
            self._check_sender(res, sender_id)
            self._validate(res, msg_dt=msg_dt, market_price=None, now=now)
            return res

        # 4. Marketing / greetings / member talk.
        if RE_PROMO.search(t) or len(t) < 25:
            res.intent, res.confidence = Intent.CHATTER, 0.8
            return res
        # 5. Has a direction word and a plausible price but did not form a signal:
        #    hand to the AI fallback rather than guessing.
        if (RE_BUY.search(t) or RE_SELL.search(t)) and self._prices(t):
            res.intent, res.confidence = Intent.UNKNOWN, 0.0
            res.notes.append("direction+price present but template did not match")
            return res
        res.intent, res.confidence = Intent.CHATTER, 0.6
        return res
    # ── bare directional calls ────────────────────────────────────────────────

    def _try_pre_signal(self, t: str) -> Optional[tuple]:
        """A direction with no levels: "Gold buy now", "Sell gold".

        Returns (Signal, confidence) or None. Deliberately strict — see the
        note on _PRE_FILLER. Every word has to be recognised, so a message
        gains nothing by being short.
        """
        if not self.pre_signal or len(t) > self.pre_signal_max_chars:
            return None
        d = self._direction(t)
        if not d:
            return None
        # Any number at all disqualifies it. A bare call has no levels, and a
        # message with a price that failed to parse as a signal is a different
        # problem that already routes to the AI fallback.
        if re.search(r"\d", t):
            return None
        if self.has_sl_token(t) or self.has_tp_token(t):
            return None
        for guard in (RE_QUESTION, RE_FIRST_PERSON_PAST, RE_TP_HIT,
                      RE_HYPOTHETICAL, RE_PROMO, RE_MGMT_SHAPED):
            if guard.search(t):
                return None
        words = [w for w in re.split(r"[^A-Za-z/]+", t) if w]
        if not words:
            return None
        seen_instrument = False
        for w in words:
            lw = w.lower()
            if RE_BUY.fullmatch(w) or RE_SELL.fullmatch(w):
                continue
            if RE_PRE_INSTRUMENT.match(w):
                seen_instrument = True
                continue
            if lw in _PRE_FILLER:
                continue
            return None                 # an unrecognised word: not a bare call
        explicit = bool(RE_MARKET.search(t))
        if not (explicit or seen_instrument):
            # A lone "buy" with no instrument and no "now" is not an order.
            return None
        sig = Signal(symbol=self.symbol, direction=d,
                     order_type=OrderType.MARKET, needs_market_entry=True,
                     raw_text=t)
        return sig, (0.85 if (explicit and seen_instrument) else 0.7)

    # ── signal extraction ─────────────────────────────────────────────────────
    def _prices(self, t: str) -> list[float]:
        out = []
        for m in re.finditer(r"\b\d{3,5}(?:\.\d{1,3})?\b", t):
            v = float(m.group())
            if self.price_min <= v <= self.price_max:
                out.append(v)
        return out
    def _direction(self, t: str) -> Optional[str]:
        """First directional token in the message wins; a later 'sell setup' in
        prose must not flip a BUY header."""
        b = RE_BUY.search(t)
        s = RE_SELL.search(t)
        if b and s:
            return "BUY" if b.start() < s.start() else "SELL"
        if b:
            return "BUY"
        if s:
            return "SELL"
        return None
    def _same_instrument(self, a: str, b: str) -> bool:
        """Compare ignoring broker suffixes: XAUUSD == XAUUSDc == XAUUSDm."""
        def base(x: str) -> str:
            x = self.aliases.get(x.upper(), x).upper()
            return re.sub(r"(?:[CMZ]|MICRO|\.[A-Z]+|_[A-Z]+)$", "", x)
        return base(a) == base(b)

    def _symbol(self, t: str) -> Optional[str]:
        up = t.upper()
        for alias, sym in self.aliases.items():
            if re.search(rf"\b{re.escape(alias)}\b", up):
                return sym
        return None
    def _extract_sl(self, t: str) -> tuple[Optional[float], list[str]]:
        notes = []
        skipped, hits = _sl_matches(t)
        if skipped:
            notes.append("ignored a 'buy/sell stop' order-type phrase that "
                         "otherwise looks like an SL label")
        for m in hits:
            v = float(m.group(1))
            if self.price_min <= v <= self.price_max:
                return v, notes
            notes.append(f"SL value {v} outside price range, ignored")
        if re.search(r"stop\s*loss[^\n]{0,40}\bpips?\b", t, re.I):
            notes.append("SL expressed in pips relative to an unposted candle")
        return None, notes

    @staticmethod
    def _sl_wrong_side(s) -> bool:
        return ((s.direction == "BUY" and s.sl >= s.entry) or
                (s.direction == "SELL" and s.sl <= s.entry))

    def _sl_is_implausible(self, s) -> bool:
        """A posted stop that cannot be what the operator meant."""
        if self._sl_wrong_side(s):
            return True
        return abs(s.entry - s.sl) > self.max_sl_dist

    @staticmethod
    def _tps_agree_with_direction(s) -> bool:
        """Every posted target on the profitable side of the entry.

        This is the consistency check that makes rescuing a mistyped stop safe.
        If the targets also disagree with the direction, the message was not
        parsed correctly and must be refused, not repaired.
        """
        if not s.tps or s.entry is None:
            return False
        if s.direction == "BUY":
            return all(float(t) > float(s.entry) for t in s.tps)
        return all(float(t) < float(s.entry) for t in s.tps)

    def has_sl_token(self, t: str) -> bool:
        return self._extract_sl(t)[0] is not None

    def has_tp_token(self, t: str) -> bool:
        tps, runner, _ = self._extract_tps(t, None, None)
        return bool(tps) or runner

    def _targets_block(self, t: str) -> list[float]:
        """Read an unlabelled target list sitting under a 'Targets:' header.

        Green Pips Zone (-1002145295870) posts every ticket as

            Entry → 4322
            Targets:
            ✅ 4319
            ✅ 4316
            ...
            SL → 4332

        The target lines carry no TP token at all, so RE_TP sees nothing and
        the signal reaches the executor with a stop and no destination — which
        _handle_entry refuses outright ("All TPs passed"). The channel is
        unmeasurable without this.

        The rule is deliberately narrow, and only runs when RE_TP found
        nothing anywhere in the message:
          - a line must be a bare 'Targets:' / 'TP:' header, nothing else on it
          - collection stops at the first blank line, or at any line carrying
            a stop-loss or entry token, or at any line holding more than one
            number
          - a collected line must be exactly one in-range price plus
            decoration (emoji, markdown, bullets) and no letters
        Across the whole 21,738-message corpus only 118 messages contain such
        a header, 116 of them from this one channel.
        """
        out: list[float] = []
        started = False
        for ln in t.splitlines():
            s = ln.strip()
            if not started:
                if _RE_TARGETS_HDR.match(s):
                    started = True
                continue
            if not s:
                break
            if re.search(r"[A-Za-z]", s):
                break
            nums = re.findall(r"\d{3,5}(?:\.\d{1,3})?", s)
            if len(nums) != 1:
                break
            v = float(nums[0])
            if not (self.price_min <= v <= self.price_max):
                break
            out.append(v)
        return out
    def _extract_tps(self, t: str, entry_hint: Optional[float],
                     direction: Optional[str]) -> tuple[list[float], bool, list[str]]:
        tps, runner, notes = [], False, []
        for m in RE_TP.finditer(t):
            raw = m.group(1)
            if raw.lower() in ("open", "free"):
                runner = True
                continue
            v = float(raw)
            if self.price_min <= v <= self.price_max:
                tps.append(v)
        if not tps:
            tps.extend(self._targets_block(t))
        # "TP every 100 pips" -> synthesise the ladder between entry and final TP.
        if not runner and re.search(
                r"(?:\btp\s*\d?\s*[:.]?|/)\s*open\b"
                r"|\btp\s*\d?\s*[:.]\s*free\b", t, re.I):
            runner = True
        step_m = RE_TP_EVERY.search(t)
        step_pips = float(step_m.group(1)) if step_m else self.tp_ladder_step
        if step_pips and entry_hint and tps and direction:
            step = float(step_pips) * self.pip_value
            far = max(tps) if direction == "BUY" else min(tps)
            ladder, cur = [], entry_hint
            for _ in range(self.max_tps):
                cur = cur + step if direction == "BUY" else cur - step
                if (direction == "BUY" and cur >= far) or \
                   (direction == "SELL" and cur <= far):
                    break
                ladder.append(round(cur, 3))
            if ladder:
                tps = ladder + [far]
                notes.append(f"synthesised {len(ladder)}-step TP ladder at "
                             f"{step_pips} pips")
        # dedupe, keep order by distance from entry
        seen, clean = set(), []
        for v in tps:
            if v not in seen:
                seen.add(v)
                clean.append(v)
        return clean[: self.max_tps], runner, notes
    def _extract_entry(self, t: str, sl: Optional[float], tps: list[float],
                       direction_hint: Optional[str] = None
                       ) -> tuple[Optional[float], Optional[float], bool, list[str]]:
        """Return (low, high, is_zone, notes). Excludes SL/TP numbers."""
        notes: list[str] = []
        excluded = set(tps) | ({sl} if sl else set())
        # Strip SL and TP lines so their numbers cannot be mistaken for entries.
        lines = []
        for ln in t.splitlines():
            if _sl_matches(ln)[1] or RE_TP.search(ln):
                continue
            lines.append(ln)
        head = "\n".join(lines) if lines else t
        # a) explicit zone: "4410.4 - 4415", "4420/4425", "4054 OR 4050",
        #    "b/n 4385-4390", "4306/7"
        # "_" and "__" are zone separators for two channels and MUST be here:
        #   John Wick FX  "GOLD BUY NOW 4816__4814"
        #   Emily Pips    "Price Open @ 4097_4000"
        # Without them both channels read as a single entry at one edge. They
        # are deliberately NOT in the markdown-stripping rule in normalize()
        # for the same reason, from the opposite direction.
        zone = re.search(
            r"(\d{3,5}(?:\.\d{1,3})?)"
            r"\s*(?:-|/|_+|or\b|\s+to\s+|b/n)\s*"
            r"(\d{1,5}(?:\.\d{1,3})?)"
            r"(?!\s*\+?\s*(?:pips?|points?|%))", head, re.I)
        if zone:
            a = float(zone.group(1))
            b_raw = zone.group(2)
            b = float(b_raw)
            if len(b_raw.split(".")[0]) < len(str(int(a))):
                # "4306/7" shorthand -> 4307
                pref = str(int(a))[: len(str(int(a))) - len(b_raw.split(".")[0])]
                b = float(pref + b_raw)
                notes.append(f"expanded shorthand zone bound to {b}")
            if (self.price_min <= a <= self.price_max
                    and self.price_min <= b <= self.price_max
                    and a not in excluded and b not in excluded):
                lo_, hi_ = min(a, b), max(a, b)
                # A "zone" wider than any operator would really quote is a
                # typo or two separate ideas, not a range. Real examples:
                #   Emily Pips   "SELL 4060 / 4163"   (4163 means 4063)
                #   Emily Pips   "Price Open @ 4097_4000"  (4000 means 4090)
                #   Mr William   "SELL Zone 4200 OR 4403"  (two ideas)
                # Treating those as a range puts one leg hundreds of dollars
                # off market. Collapse to the edge nearest the market instead
                # of discarding the signal: the near edge is the number the
                # operator certainly meant, and it is also the conservative
                # fill under zone_fill "worst".
                if (hi_ - lo_) > self.max_zone_width:
                    # Which bound did he mean? The stop answers it: an operator
                    # places his stop a short way BEYOND the entry, so the bound
                    # sitting near the stop is the real one and the far bound is
                    # the typo.
                    #   "SELL 4060 / 4163" SL 4068 -> |4060-4068|=8 vs 95  -> 4060
                    #   "Zone 4200 OR 4403" SL 4210 -> |4200-4210|=10 vs 193 -> 4200
                    #   "Price Open @ 4097_4000" SL 4107 -> 10 vs 107     -> 4097
                    # With no stop, the nearest target answers the same way.
                    # With neither, fall back to the edge the market reaches
                    # first for this direction.
                    ref = sl if sl else (min(tps, key=lambda x: abs(x - lo_))
                                         if tps else None)
                    if ref is not None:
                        keep = min((lo_, hi_), key=lambda v: abs(v - float(ref)))
                        how = f"it sits nearest the stop/target at {float(ref)}"
                    else:
                        keep = hi_ if direction_hint == "BUY" else lo_
                        how = "no stop or target to disambiguate; took the near edge"
                    notes.append(
                        f"zone {lo_}-{hi_} is {hi_ - lo_:.2f} wide, above "
                        f"max_zone_width {self.max_zone_width}: reading it as a "
                        f"single entry at {keep} because {how}")
                    return keep, keep, False, notes
                return lo_, hi_, True, notes
        # b) single price after an entry label
        lbl = RE_ENTRY_LABEL.search(head)
        if lbl:
            m = re.search(r"(\d{3,5}(?:\.\d{1,3})?)", head[lbl.end():])
            if m:
                v = float(m.group(1))
                if self.price_min <= v <= self.price_max and v not in excluded:
                    return v, v, False, notes
        # c) first in-range price on the direction line
        for ln in head.splitlines():
            if RE_BUY.search(ln) or RE_SELL.search(ln):
                for m in re.finditer(r"\b(\d{3,5}(?:\.\d{1,3})?)\b", ln):
                    v = float(m.group(1))
                    if self.price_min <= v <= self.price_max and v not in excluded:
                        return v, v, False, notes
        return None, None, False, notes
    def _resolve_entry(self, lo, hi, direction) -> Optional[float]:
        if lo is None:
            return None
        if lo == hi:
            return lo
        mode = self.zone_fill
        if mode == "midpoint":
            return round((lo + hi) / 2, 3)
        if mode == "best":     # most favourable edge
            return lo if direction == "BUY" else hi
        # "worst" (default): the edge nearest current price, i.e. the fill you
        # actually get when price enters the zone. Conservative for R sizing.
        return hi if direction == "BUY" else lo
    def _order_type(self, t: str, direction: str) -> OrderType:
        if RE_STOPORD.search(t):
            return OrderType.BUY_STOP if direction == "BUY" else OrderType.SELL_STOP
        if RE_LIMIT.search(t):
            return OrderType.BUY_LIMIT if direction == "BUY" else OrderType.SELL_LIMIT
        if RE_MARKET.search(t):
            return OrderType.MARKET
        return OrderType.AUTO
    def _try_signal(self, t: str):
        direction = self._direction(t)
        if not direction:
            return None, 0.0, []
        sl, n1 = self._extract_sl(t)
        # entry_hint needed before TP ladder synthesis; do a cheap first pass.
        lo0, hi0, _, _ = self._extract_entry(t, sl, [], direction)
        # Anchor the synthetic ladder on the fill we actually expect, not on the
        # low bound. For a BUY zone the worst fill is the HIGH bound, so anchoring
        # on lo0 put every rung one zone-width too close.
        tps, runner, n2 = self._extract_tps(
            t, self._resolve_entry(lo0, hi0, direction), direction)
        lo, hi, is_zone, n3 = self._extract_entry(t, sl, tps, direction)
        # A signal needs a direction plus at least one of {SL, TP}, unless the
        # channel is configured to allow bare calls (which then hand off to
        # core/bare_trade_watcher.py for its own timed exit).
        if sl is None and not tps and not runner:
            if not (self.allow_bare and lo0 is not None):
                return None, 0.0, []
        # Reject "TP2 hit 130 pips" style reports that also carry a direction.
        if RE_TP_HIT.search(t) and sl is None and len(tps) <= 1 and lo is None:
            return None, 0.0, []
        # The CHANNEL's symbol always wins. It carries the broker suffix
        # (XAUUSDc, XAUUSDm), which the alias map does not: resolving "GOLD" in
        # the text to "XAUUSD" would route the order to an instrument that does
        # not exist on a suffixed account. The alias map is only used to notice
        # that the operator is talking about a different instrument entirely.
        sym = self.symbol
        mentioned = self._symbol(t)
        foreign = bool(mentioned and not self._same_instrument(mentioned, sym))
        if foreign:
            n1 = n1 + [f"message names {mentioned} but the channel trades {sym}"]
        sig = Signal(symbol=sym, direction=direction, is_zone=is_zone,
                     entry_low=lo, entry_high=hi, sl=sl, tps=tps,
                     has_open_runner=runner, raw_text=t[:400],
                     foreign_symbol=(mentioned if foreign else None))
        sig.entry = self._resolve_entry(lo, hi, direction)
        sig.order_type = self._order_type(t, direction)
        if sl is None and not tps and not runner:
            n3 = n3 + ["BARE signal: no SL and no TP posted; hand to bare_trade_watcher"]
        conf = 0.55
        if sl is not None:
            conf += 0.2
        if tps:
            conf += 0.15
        if lo is not None:
            conf += 0.1
        return sig, min(conf, 1.0), n1 + n2 + n3
    # ── management extraction ─────────────────────────────────────────────────
    @staticmethod
    def _clause_at(t: str, pos: int) -> str:
        """Return the sentence/line containing `pos`.
        Guards must be evaluated against the clause holding the imperative, not
        the whole message. These operators routinely write
            "Let's CLOSE our profits. If you wish to hold, move SL to BE."
        Judging that message-wide flags it as conditional and suppresses the
        only exit instruction the channel ever gives.
        """
        start = max((t.rfind(c, 0, pos) for c in "\n.!?"), default=-1) + 1
        ends = [i for i in (t.find(c, pos) for c in "\n.!?") if i != -1]
        end = min(ends) + 1 if ends else len(t)
        return t[start:end].strip()
    def _guard_penalty(self, clause: str, notes: list[str],
                       intent_text: str = "") -> float:
        pen = 0.0
        # RE_HYPOTHETICAL exists to stop the WORD "close" being read as a verb
        # ("close to TP4", "the market closed"). If the intent was matched on
        # text that never contains "close", the guard is about a different part
        # of the sentence and must not apply. GTMO's "at the top of our range
        # and close to TP4 lets take some more profits ok" is one clause: the
        # instruction is "lets take some more profits", and penalising it for
        # the unrelated "close to TP4" pushed a real partial to 0.38 and
        # silently dropped it.
        hypo_applies = (not intent_text) or ("clos" in intent_text.lower())
        if RE_FIRST_PERSON_PAST.search(clause) and not self.trust_first_person:
            pen += 0.45
            notes.append("first-person past tense in the same clause: reads as a report")
        if RE_QUESTION.search(clause):
            pen += 0.5
            notes.append("imperative sits inside a question")
        if hypo_applies and RE_HYPOTHETICAL.search(clause):
            pen += 0.5
            notes.append("'close' used non-imperatively in this clause")
        if RE_POSSESSIVE_BE.search(clause):
            pen += 0.4
            notes.append("possessive before breakeven: reads as a report")
        if RE_PROMO.search(clause):
            pen += 0.4
            notes.append("management verb sits in a promotional clause")
        if RE_CONDITIONAL.search(clause) and not self.follow_conditional:
            # Only penalise when the condition PRECEDES the verb
            # ("You can close ..." / "If you want, close ...").
            # A trailing carve-out ("Close now, if you wish to hold set BE")
            # is still an instruction.
            cond = RE_CONDITIONAL.search(clause)
            verb = re.search(r"\b(close|cancel|delete|move|set|take|secure|exit)\b",
                             clause, re.I)
            if verb and cond.start() < verb.start():
                pen += 0.35
                notes.append("condition precedes the verb ('you can ...' / 'if you want ...')")
        return pen
    # Instruction pairs that genuinely co-occur in one message. Only these are
    # looked for as a follow-up, so an unrelated keyword elsewhere in a long
    # post cannot manufacture a second action.
    _FOLLOW_UP_PAIRS = {
        Intent.CLOSE_PARTIAL: (Intent.MOVE_SL_BE, RE_BE),
        Intent.CLOSE_ALL:     (Intent.MOVE_SL_BE, RE_BE),
    }

    def _attach_follow_up(self, res: ParsedMessage, t: str, sender_id) -> None:
        """Carry a second instruction from the same message, when there is one.

        "Take some profits now and set breakeven ok" is two orders. So is
        "Let's CLOSE our trade now and set breakeven if you wish to hold".
        The parser matches intents in priority order and returned only the
        winner, so the breakeven half was dropped every time.
        """
        pair = self._FOLLOW_UP_PAIRS.get(res.intent)
        if not pair or res.rejected_reasons:
            return
        intent2, pattern = pair
        m = pattern.search(t)
        if not m:
            return
        notes2: list[str] = []
        conf2 = max(0.0, 0.88 - self._guard_penalty(
            self._clause_at(t, m.start()), notes2, m.group(0)))
        if conf2 < self.min_conf:
            res.notes.append(
                f"secondary {intent2.value} seen but scored {conf2:.2f}; not acted on")
            return
        fu = ParsedMessage(intent=intent2, confidence=conf2,
                           action=Action(scope=self.close_scope),
                           channel_id=self.channel_id, msg_id=res.msg_id,
                           notes=notes2 + ["follow-up instruction from the same message"])
        self._check_sender(fu, sender_id)
        res.follow_up = fu
        res.notes.append(f"message also carries {intent2.value}")

    def _try_management(self, t: str):
        notes: list[str] = []
        def emit(intent, action, base, m):
            pen = self._guard_penalty(self._clause_at(t, m.start()), notes,
                                      m.group(0))
            return (intent, action, max(0.0, base - pen), notes)
        # Order matters: the more specific intents are tested first, because
        # CLOSE_PARTIAL and MOVE_SL_BE messages almost always also contain the
        # substring that CLOSE_ALL matches.
        m = RE_MOVE_SL_PRICE.search(t)
        if m:
            # Two alternatives, so the price is in whichever group matched.
            v = float(m.group(1) or m.group(2))
            if self.price_min <= v <= self.price_max:
                return emit(Intent.MOVE_SL_PRICE,
                            Action(price=v, scope=self.close_scope), 0.9, m)
        m = RE_CANCEL.search(t)
        if m and RE_CANCEL_OBJECT_EXCLUDE.search(self._clause_at(t, m.start())):
            m = None
        if m:
            d = None
            if re.search(r"\ball\s+buys?\b|\bbuy\s+orders?\b", t, re.I):
                d = "BUY"
            elif re.search(r"\ball\s+sells?\b|\bsell\s+orders?\b", t, re.I):
                d = "SELL"
            return emit(Intent.CANCEL_PENDING,
                        Action(scope=self.close_scope, direction=d), 0.9, m)
        m = RE_CLOSE_PARTIAL.search(t)
        if m:
            frac = 0.5
            fm = re.search(r"\b(\d{1,3})\s*%", t)
            if fm:
                frac = min(1.0, float(fm.group(1)) / 100.0)
            return emit(Intent.CLOSE_PARTIAL,
                        Action(fraction=frac, scope=self.close_scope), 0.88, m)
        m = RE_CLOSE_ALL.search(t)
        if m:
            return emit(Intent.CLOSE_ALL, Action(scope=self.close_scope), 0.9, m)
        # Tried AFTER the undirected form on purpose: "close all buy trades"
        # must read as a plain close-all, not as a directional one that then
        # filters the book down to one side.
        m = RE_CLOSE_DIRECTIONAL.search(t)
        if m:
            side = (m.group(1) or m.group(2) or "").upper()
            return emit(Intent.CLOSE_ALL,
                        Action(scope=self.close_scope, direction=side or None),
                        0.85, m)
        m = RE_BE.search(t)
        if m:
            return emit(Intent.MOVE_SL_BE, Action(scope=self.close_scope), 0.88, m)
        return None
    # ── validation ────────────────────────────────────────────────────────────
    def _check_freshness(self, res: ParsedMessage, msg_dt: Optional[datetime],
                          now: Optional[datetime]) -> None:
        """Age gate. Lifted out of _validate so PRE_SIGNAL can reuse it.

        A stale bare call is the worst kind: acting on "gold buy now" from
        twenty minutes ago means buying a move that already happened.
        """
        r = res.rejected_reasons
        if msg_dt is not None:
            ref = now or datetime.now(timezone.utc)
            # Both sides are treated as UTC. A naive datetime from an export is
            # assumed UTC; the live listener must pass tz-aware values.
            if msg_dt.tzinfo is None:
                msg_dt = msg_dt.replace(tzinfo=timezone.utc)
            if ref.tzinfo is None:
                ref = ref.replace(tzinfo=timezone.utc)
            age = (ref - msg_dt).total_seconds()
            if age > self.max_age_sec:
                r.append(f"message is {age:.0f}s old, max {self.max_age_sec}s")
            elif age < -self.future_tolerance_sec:
                # A message dated in the future means the clocks disagree. The
                # usual cause is a naive local timestamp being compared against
                # UTC: at UTC+3 every message looks 3 h early, age goes negative,
                # and the staleness gate silently stops rejecting anything.
                # Fail closed instead of trusting a clock we cannot verify.
                r.append(f"message is dated {-age:.0f}s in the future; "
                         f"timestamp is not tz-aware UTC")

    def _validate(self, res: ParsedMessage, msg_dt: Optional[datetime],
                  market_price: Optional[float], now: Optional[datetime]):
        s = res.signal
        r = res.rejected_reasons
        # A bare directional call has no levels by definition, so every gate
        # that reasons about levels — requires_sl, the SL distance band, TP
        # geometry, entry deviation — would refuse all of them. Only the
        # checks that still mean something are run: is it fresh, is it this
        # instrument, and is the sender allowed to speak for the channel.
        # Freshness matters MORE here, not less: acting on a stale "buy now"
        # means entering a move that has already happened.
        if res.intent == Intent.PRE_SIGNAL:
            self._check_freshness(res, msg_dt, now)
            if self.refuse_foreign and getattr(s, "foreign_symbol", None):
                r.append(f"message is about {s.foreign_symbol} but this channel "
                         f"trades {self.symbol}")
            if res.confidence < self.pre_signal_min_conf:
                r.append(f"confidence {res.confidence:.2f} < "
                         f"pre_signal_min_confidence {self.pre_signal_min_conf:.2f}")
            return
        if res.confidence < self.min_conf:
            r.append(f"confidence {res.confidence:.2f} < min_confidence {self.min_conf:.2f}")
        if s.entry is None:
            if market_price is not None:
                s.entry = market_price
                res.notes.append("entry defaulted to market price")
            elif s.order_type == OrderType.MARKET:
                # Legitimate: "GOLD SELL now / SL 4050 / TP 3900" has no entry
                # because the entry IS the fill. Geometry cannot be checked yet,
                # so flag it and force the executor through finalize_market().
                s.needs_market_entry = True
                res.notes.append("MARKET order with no posted entry: geometry "
                                 "checks deferred to finalize_market()")
            else:
                r.append("no entry price and no market price to fall back on")
        # Any MARKET order fills at whatever the book gives, not at the posted
        # level. Sizing and the SL-side check must be redone against the real
        # fill, so force the executor through finalize_market() even when the
        # operator did post a reference price.
        if s.order_type == OrderType.MARKET and not res.rejected_reasons:
            s.needs_market_entry = True
        # --- wrong instrument ------------------------------------------------
        # Every price gate here is calibrated for ONE instrument: price_min /
        # price_max, pip_value, and the 3-to-5-digit price pattern. Fed a
        # EURUSD post, "1.0850" is read as 850.0 and the whole signal is
        # quietly mis-scaled. Refuse rather than route another instrument's
        # numbers to this channel's symbol.
        if self.refuse_foreign and getattr(s, "foreign_symbol", None):
            r.append(f"message is about {s.foreign_symbol} but this channel "
                     f"trades {s.symbol}; refusing cross-instrument signal")

        # --- stop loss -------------------------------------------------------
        # A stop that parsed but cannot be true is the same problem as a stop
        # that did not parse: we know where to enter and not where to exit.
        # Dropping the whole signal for one mistyped number wastes a call the
        # operator got right in every other respect, so hand those to the same
        # fallback rather than refusing them.
        #
        # Only for a stop that is IMPLAUSIBLE, never one that is merely
        # inconvenient, and only when the rest of the message is self-
        # consistent - every target on the correct side of the entry. Without
        # that check a wholly mis-parsed message would be rescued into a trade,
        # which is the opposite of what this is for.
        #
        # AND ONLY WHEN THE ENTRY CAME FROM THE MESSAGE. If the entry is a
        # market fill we supplied, a stop on the wrong side of it usually means
        # THE MARKET MOVED PAST THE OPERATOR'S LEVELS, not that he mistyped:
        #   "GOLD SELL a now / SL 4050 / TP 3900" filled at 4060
        # His stop and target are both coherent for an entry near 4000-4040.
        # The signal is stale, and a stale signal must be refused, not given a
        # synthetic stop and traded at a price he never called.
        if (s.sl is not None and s.entry is not None and self.default_sl_pips
                and not getattr(s, "needs_market_entry", False)
                and self._sl_is_implausible(s)
                and self._tps_agree_with_direction(s)):
            why = ("on the wrong side" if self._sl_wrong_side(s)
                   else f"{abs(s.entry - s.sl):.2f} away, above max "
                        f"{self.max_sl_dist}")
            res.notes.append(
                f"posted stop {s.sl} is unusable against entry {s.entry} "
                f"({why}); every target is on the correct side, so it is "
                f"treated as a typo and synthesised")
            s.sl = None

        if s.sl is None:
            if self.requires_sl and not self.default_sl_pips:
                r.append("no stop loss in message and requires_sl=true")
            elif self.default_sl_pips and (s.entry or s.entry_low or s.entry_high):
                d = float(self.default_sl_pips) * self.pip_value
                # Measure from the PROTECTIVE edge of the zone, not from the
                # fill. On a sell zone 4580-4585 the stop belongs above 4585;
                # measuring from entry (4580, the worst fill) put it at 4588,
                # three dollars above the zone instead of eight, so it sat
                # inside the operator's own entry band and any wick through
                # the zone would take it.
                if s.direction == "BUY":
                    ref = s.entry_low if s.entry_low is not None else s.entry
                    s.sl = round(float(ref) - d, 3)
                else:
                    ref = s.entry_high if s.entry_high is not None else s.entry
                    s.sl = round(float(ref) + d, 3)
                res.notes.append(
                    f"SL synthesised at {self.default_sl_pips} pips from "
                    f"{float(ref)}: the operator withheld it")
        if s.sl is not None and s.entry is not None:
            wrong = (s.direction == "BUY"  and s.sl >= s.entry) or \
                    (s.direction == "SELL" and s.sl <= s.entry)
            if wrong:
                r.append(f"SL {s.sl} is on the wrong side of entry {s.entry} "
                         f"for a {s.direction}")
            dist = abs(s.entry - s.sl)
            if dist < self.min_sl_dist:
                r.append(f"SL distance {dist:.2f} below min {self.min_sl_dist}")
            if dist > self.max_sl_dist:
                r.append(f"SL distance {dist:.2f} above max {self.max_sl_dist}")
        # --- geometry consensus ----------------------------------------------
        # MUST run BEFORE the wrong-side TP purge, on the TPs as posted. Run
        # after, the purge has already emptied s.tps and this check silently
        # no-ops on exactly the messages it exists to catch.
        if s.sl is not None and s.entry is not None and s.tps:
            implied = "SELL" if s.sl > s.entry else "BUY"
            above = sum(1 for v in s.tps if v > s.entry)
            below = len(s.tps) - above
            tp_side = "BUY" if above > below else ("SELL" if below > above else None)
            if tp_side and implied == tp_side and implied != s.direction:
                r.append(f"text says {s.direction} but SL/TP geometry implies "
                         f"{implied}; refusing ambiguous signal")
        # --- take profits ----------------------------------------------------
        if s.entry is not None and s.tps:
            bad = [v for v in s.tps
                   if (s.direction == "BUY" and v <= s.entry)
                   or (s.direction == "SELL" and v >= s.entry)]
            if bad:
                s.tps = [v for v in s.tps if v not in bad]
                res.notes.append(f"dropped {len(bad)} TP(s) on the wrong side of entry")
            if not s.tps:
                # An open runner does NOT excuse this. A message that posted
                # explicit targets and had every one of them fail the side check
                # is internally inconsistent; executing the runner leg alone
                # would open an unbounded position off a message we cannot read.
                r.append("every posted take profit was on the wrong side of "
                         "entry; message is internally inconsistent")
            else:
                s.tps.sort(reverse=(s.direction == "SELL"))
        self._check_freshness(res, msg_dt, now)

        # --- slippage gate ---------------------------------------------------
        # Resolve the order type FIRST. max_entry_deviation is a slippage
        # defence, and slippage only exists for an order that fills NOW. A
        # pending limit is supposed to sit away from the market: that distance
        # is the trade, not an error. Checking deviation before resolving the
        # type refused every legitimate limit order the moment price moved more
        # than max_entry_deviation away from it, which for a limit is normal
        # within seconds of the post.
        if market_price is not None and s.entry is not None:
            if s.order_type == OrderType.AUTO:
                lo, hi = s.entry_low, s.entry_high
                if lo is not None and hi is not None and lo <= market_price <= hi:
                    # The market is INSIDE the posted zone, so the entry
                    # condition is already satisfied and this is a fill at
                    # market. Comparing the resolved single edge against the
                    # market instead turned most ordinary zone signals into
                    # stop orders the moment price ticked past the near edge.
                    s.order_type = OrderType.MARKET
                    s.needs_market_entry = True
                    res.notes.append(
                        f"market {market_price} is inside the entry zone "
                        f"{lo}-{hi}: filling at market")
                elif s.direction == "BUY":
                    s.order_type = (OrderType.BUY_LIMIT if s.entry < market_price
                                    else OrderType.BUY_STOP)
                else:
                    s.order_type = (OrderType.SELL_LIMIT if s.entry > market_price
                                    else OrderType.SELL_STOP)

            fills_now = (s.order_type == OrderType.MARKET or s.needs_market_entry)
            dev = abs(market_price - s.entry)

            if fills_now:
                # Filling at market against a stale quote: this is the real
                # slippage case and the only one max_entry_deviation governs.
                if dev > self.max_entry_dev:
                    r.append(f"market {market_price} is {dev:.2f} from entry "
                             f"{s.entry}, max deviation {self.max_entry_dev} "
                             f"(order fills at market)")
            else:
                # A resting order. Distance from the market is expected, but it
                # must be on the side it can actually rest on. A buy limit above
                # the market, or a sell limit below it, would fill instantly at a
                # worse price than posted, so it is a market order in disguise.
                wrong_side = (
                    (s.order_type == OrderType.BUY_LIMIT and s.entry > market_price)
                    or (s.order_type == OrderType.SELL_LIMIT and s.entry < market_price)
                    or (s.order_type == OrderType.BUY_STOP and s.entry < market_price)
                    or (s.order_type == OrderType.SELL_STOP and s.entry > market_price))
                if wrong_side:
                    r.append(f"{s.order_type.value} at {s.entry} cannot rest with "
                             f"the market at {market_price}: it would fill "
                             f"immediately at a worse price than posted")
                elif self.max_limit_dist and dev > self.max_limit_dist:
                    # Off by default. A resting order far from the market is
                    # usually legitimate; this only exists to catch an entry that
                    # was mis-parsed into a wildly wrong price.
                    r.append(f"pending order at {s.entry} is {dev:.2f} from "
                             f"market {market_price}, max_limit_distance "
                             f"{self.max_limit_dist}")
                else:
                    res.notes.append(
                        f"{s.order_type.value} resting {dev:.2f} from market "
                        f"{market_price}: slippage gate does not apply")
    # ── execution-time finalisation ───────────────────────────────────────────
    def finalize_market(self, res: ParsedMessage, fill_price: float) -> ParsedMessage:
        """Call this immediately before sending a MARKET order.
        A market signal has no posted entry, so the SL side check, the SL
        distance check and the TP side check could not run at parse time. They
        run here against the real fill price. If the operator posted a stop that
        is on the wrong side of where the market actually is, this is the only
        place that gets caught.
        """
        if res.signal is None:
            return res
        res.signal.entry = fill_price
        res.signal.entry_low = res.signal.entry_high = fill_price
        res.signal.needs_market_entry = False
        # Rebuild from empty rather than filtering. Filtering left every earlier
        # reason in place and _validate then appended its own copy, so calling
        # this twice produced duplicate reasons and an unreadable audit log.
        res.rejected_reasons = []
        self._validate(res, msg_dt=None, market_price=None, now=None)
        res.signal.needs_market_entry = False
        return res
# ══════════════════════════════════════════════════════════════════════════════
# Multi-message assembly
# ══════════════════════════════════════════════════════════════════════════════
class SignalAssembler:
    """
    Some operators split one trade across consecutive posts:
        "Gold sell now"          <- direction, no levels
        "SL place at 4437"       <- stop
        "TP1 at 4407 / TP: 4405" <- targets
    Wrap SignalParser in this when `parser.assemble_window_sec > 0`. Feed every
    message in arrival order; it emits a ParsedMessage only when the fragment
    set is complete or the window expires.
    """
    def __init__(self, parser: SignalParser):
        self.parser = parser
        self.window = int(parser.p.get("assemble_window_sec", 0))
        self._pending: Optional[dict] = None

    @staticmethod
    def _complete(sig: Optional[Signal]) -> bool:
        """A fragment set is only tradeable once it has a stop AND a target."""
        return bool(sig and sig.sl is not None
                    and (sig.tps or sig.has_open_runner))

    def feed(self, text: str, msg_id: int, msg_dt: datetime,
             market_price: Optional[float] = None,
             now: Optional[datetime] = None,
             sender_id=None) -> Optional[ParsedMessage]:
        if self.window <= 0:
            return self.parser.parse(text, msg_id, msg_dt, market_price, now,
                                     sender_id)
        t = normalize(text)
        self._expire(msg_dt)
        direct = self.parser.parse(text, msg_id, msg_dt, market_price, now,
                                   sender_id)
        # A standalone signal that is already complete wins and discards any
        # half-built fragment.
        if direct.intent == Intent.NEW_SIGNAL and self._complete(direct.signal):
            self._pending = None
            return direct
        # A bare directional call opens a fragment AND is emitted immediately.
        # Emitting it is the entire point: the operator is in the trade now and
        # the levels arrive a minute later, by which time the price has moved.
        # The buffer stays open so the levels still assemble into the full
        # signal, and the executor's _upgrade_bare_trades converts the bare
        # position rather than opening a second one.
        if direct.intent == Intent.PRE_SIGNAL:
            if len(t) <= self.parser.assemble_max_chars:
                self._pending = {"parts": [text], "dt": msg_dt, "id": msg_id}
            return direct
        if direct.intent not in (Intent.NEW_SIGNAL, Intent.CHATTER, Intent.UNKNOWN):
            return direct   # management instructions pass straight through
        d = self.parser._direction(t)
        has_sl = self.parser.has_sl_token(t)
        has_tp = self.parser.has_tp_token(t)
        # Open a fragment only on a short, order-shaped header ("Gold sell now",
        # "XAUUSD buy limit"). The original condition was `_prices(t) is not
        # None`, which is a list and therefore never None, so ANY prose carrying
        # the word "bullish" or "sell" opened a buffer and was swallowed.
        if d and not has_sl and not has_tp:
            order_shaped = (RE_MARKET.search(t) or RE_LIMIT.search(t)
                            or RE_STOPORD.search(t))
            if order_shaped and len(t) <= self.parser.assemble_max_chars:
                self._pending = {"parts": [text], "dt": msg_dt, "id": msg_id}
                return None
            return direct
        if self._pending and (has_sl or has_tp):
            parts = self._pending["parts"] + [text]
            if len(parts) > self.parser.assemble_max_parts:
                self._pending = None
                return direct
            merged = "\n".join(parts)
            out = self.parser.parse(merged, self._pending["id"],
                                    self._pending["dt"], market_price, now,
                                    sender_id)
            if out.intent == Intent.NEW_SIGNAL and self._complete(out.signal):
                # Complete: emit once, then close the buffer so the next
                # fragment cannot re-emit the same trade a second time.
                self._pending = None
                out.notes.append(f"assembled from {len(parts)} messages")
                return out
            # Still incomplete. Keep buffering and emit NOTHING. The previous
            # version returned the half-built signal here while leaving the
            # buffer open, so one trade could fire twice: once on the SL-only
            # fragment (as a market order with no take profit) and again when
            # the targets arrived.
            self._pending["parts"] = parts
            return None
        return direct if direct.intent != Intent.NEW_SIGNAL else None

    def _expire(self, now_dt: datetime):
        # An expired fragment set is dropped, never emitted. Half a trade is
        # not a trade.
        if self._pending and (now_dt - self._pending["dt"]) > timedelta(seconds=self.window):
            self._pending = None
class DuplicateGuard:
    """Suppress re-posts of the same trade.
    Several of these operators re-send an identical block minutes later (a
    correction, or just a repost). Without this the system opens the position
    twice and doubles the risk on a trade the operator only intended once.
    Keyed on (symbol, direction, entry, sl) rounded to 2dp.
    """
    def __init__(self, window_sec: int = 3600):
        self.window = window_sec
        self._seen: dict[tuple, datetime] = {}
    def is_duplicate(self, sig: Optional[Signal], when: datetime) -> bool:
        # Never let a bookkeeping helper take down the listener.
        if sig is None:
            return False
        # The first TP is part of the key. Without it two genuinely different
        # market orders that share a stop (entry is None until fill) collide,
        # and the second real trade is silently discarded as a "duplicate".
        key = (sig.symbol, sig.direction,
               round(sig.entry, 2) if sig.entry is not None else None,
               round(sig.sl, 2) if sig.sl is not None else None,
               round(sig.tps[0], 2) if sig.tps else None)
        prev = self._seen.get(key)
        self._seen = {k: v for k, v in self._seen.items()
                      if (when - v).total_seconds() <= self.window}
        self._seen[key] = when
        return prev is not None and (when - prev).total_seconds() <= self.window
# ══════════════════════════════════════════════════════════════════════════════
# Factory
# ══════════════════════════════════════════════════════════════════════════════
def merge_channel_defaults(channels_json: dict) -> dict:
    """Fold defaults.parser into every channel's parser block.

    The tools did this by hand and build_parsers did not, so calling
    build_parsers(json.load(open("channels.json"))) directly built parsers that
    silently ignored the whole defaults block: no symbol_aliases, no sender
    allowlist, no assembler limits. The live listener would then behave
    differently from anything the replay harness or the test suite exercised.
    """
    dflt = channels_json.get("defaults", {}).get("parser", {})
    for ch in channels_json.get("channels", []):
        merged = dict(dflt)
        merged.update(ch.get("parser", {}))
        ch["parser"] = merged
    return channels_json


def build_parsers(channels_json: dict) -> dict:
    """channels.json dict -> {channel_id: SignalAssembler}"""
    channels_json = merge_channel_defaults(channels_json)
    out = {}
    for ch in channels_json.get("channels", []):
        if not ch.get("enabled", True):
            continue
        p = SignalParser(ch)
        out[str(ch["id"])] = SignalAssembler(p)
    return out
