#!/usr/bin/env python3
"""
tests/test_integration_wiring.py
================================
Verifies the seams between the new deterministic parser and the existing
executor. These are the failures that would not show up in test_signal_parser.py
because they live in the translation layer, not the parser.

Run: python tests/test_integration_wiring.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

# Windows consoles default to cp1252. A failure detail containing an emoji from
# a parsed message would otherwise raise UnicodeEncodeError and hide the real
# failure. Never let the reporter crash louder than the thing it is reporting.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Telethon is not installable in every environment and channel_listener only
# needs its names at import time. Stub before importing.
if "telethon" not in sys.modules:
    tl = types.ModuleType("telethon")
    tl.TelegramClient = object
    tl.events = types.SimpleNamespace(
        NewMessage=type("NewMessage", (), {"Event": object}),
        MessageEdited=type("MessageEdited", (), {"Event": object}))
    tt = types.ModuleType("telethon.tl")
    tty = types.ModuleType("telethon.tl.types")
    tty.Message = object
    sys.modules.update({"telethon": tl, "telethon.tl": tt,
                        "telethon.tl.types": tty})

import config                                                    # noqa: E402
from core.ai_parser import ParsedSignal                          # noqa: E402
from core.signal_adapter import adapt, should_fallback           # noqa: E402
from core.signal_parser import (Intent, OrderType, SignalParser,
                                SignalAssembler)  # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        FAILS.append(label)


CFG = json.load(open(os.path.join(ROOT, "channels.json"), encoding="utf-8"))


def parser_for(cid, symbol=None, **overrides):
    """A parser wired the way config.py wires one, for channel `cid`.

    `cid` need not be in channels.json. The operator adds and removes channels
    freely, and a test that dies with IndexError because an id was deleted is
    testing the operator's config, not the code. When the id is absent we fall
    back to defaults.parser, which is exactly what a channel with no tuned
    block gets at runtime.
    """
    ch = next((c for c in CFG["channels"] if str(c["id"]) == cid), None)
    m = dict(CFG["defaults"]["parser"])
    m.update((ch or {}).get("parser", {}))
    m.update(overrides)
    return SignalParser({"id": cid,
                         "symbol": symbol or (ch or {}).get("symbol", "XAUUSDm"),
                         "parser": m})


# ═══════════════════════════════════════════════════════════════════════════
print("config.py loads the parser block")
# ═══════════════════════════════════════════════════════════════════════════
# These assert MECHANISM, never a tuned value. risk_pct, enabled and symbol
# are the operator's to change; a test that pins them turns every config edit
# into a red build.
chans = config._load_channels()
raw = {str(c["id"]): c for c in CFG["channels"]}
check("every channel in the file is loaded", len(chans) == len(raw),
      f"{len(chans)} vs {len(raw)}")
gold = [c for c in chans if c.id == "-1002070727316"][0]
check("risk_pct round-trips from the file",
      gold.risk_pct == float(raw[gold.id]["risk_pct"]), str(gold.risk_pct))
check("starting_balance round-trips from the file",
      gold.starting_balance == float(raw[gold.id].get("starting_balance", 0.0)),
      str(gold.starting_balance))
check("enabled round-trips from the file",
      [c.id for c in chans if c.enabled]
      == [k for k, v in raw.items() if v.get("enabled")],
      str([c.name for c in chans if c.enabled]))
check("parser block reaches ChannelConfig", bool(gold.parser))
check("defaults are merged in (symbol_aliases from defaults)",
      "symbol_aliases" in gold.parser)
_ovr = (raw[gold.id].get("parser") or {}).get("min_confidence")
check("per-channel override wins over defaults",
      _ovr is None or gold.parser.get("min_confidence") == _ovr,
      f"{gold.parser.get('min_confidence')} vs override {_ovr}")
check("defaults reach a channel that overrides nothing",
      all(c.parser.get("price_min") is not None for c in chans))

# ═══════════════════════════════════════════════════════════════════════════
print("\nsave_channels() round-trip does not eat the parser block")
# ═══════════════════════════════════════════════════════════════════════════
orig = config.CHANNELS_FILE
with tempfile.TemporaryDirectory() as td:
    tmp = os.path.join(td, "channels.json")
    open(tmp, "w", encoding="utf-8").write(json.dumps(CFG, indent=2))
    config.CHANNELS_FILE = type(orig)(tmp)
    loaded = config._load_channels()
    target = [c for c in loaded if c.id == "-1002070727316"][0]
    target.risk_pct = 3.0                       # simulate a dashboard edit
    config.save_channels(loaded)
    after = json.loads(open(tmp, encoding="utf-8").read())
    a_by_id = {str(c["id"]): c for c in after["channels"]}
    row = a_by_id["-1002070727316"]
    check("defaults block survives a dashboard save", "defaults" in after)
    check("per-channel parser block survives",
          "parser" in row and row["parser"] == (raw[target.id].get("parser") or {}),
          str(row.get("parser"))[:80])
    check("the edit itself was applied", row["risk_pct"] == 3.0)
    check("defaults are NOT baked into each channel",
          "symbol_aliases" not in row.get("parser", {}))
    check("no channel is dropped by a save",
          len(after["channels"]) == len(loaded))
config.CHANNELS_FILE = orig

# ═══════════════════════════════════════════════════════════════════════════
print("\nadapter: direction casing (the silent TP inversion)")
# ═══════════════════════════════════════════════════════════════════════════
p = parser_for("-1002070727316")
r = p.parse("XAUUSD Buy 4420/4425\nTP1: 4430\nTP 2:4435\nStop Loss 4410")
sig = adapt(r, "XAUUSD")
check("BUY becomes buy", sig and sig.direction == "buy",
      str(sig.direction if sig else None))
check("_tp_passed reads the buy branch after adaptation",
      sig and sig.direction in ("buy", "sell"))
check("signal_type is 'entry'", sig and sig.signal_type == "entry")
check("SL carried across", sig and sig.stop_loss == 4410.0)
check("TPs carried across", sig and sig.take_profits == [4430.0, 4435.0],
      str(sig.take_profits if sig else None))

# ═══════════════════════════════════════════════════════════════════════════
print("\nadapter: every intent maps to a handler the executor has")
# ═══════════════════════════════════════════════════════════════════════════
import re                                                        # noqa: E402
exec_src = open(os.path.join(ROOT, "core", "signal_executor.py"),
                encoding="utf-8").read()
dispatch = set(re.findall(r't == "(\w+)"', exec_src)) | \
           set(re.findall(r't in \("(\w+)", "(\w+)"\)', exec_src)[0]
               if re.findall(r't in \("(\w+)", "(\w+)"\)', exec_src) else [])
from core.signal_adapter import _INTENT_MAP                      # noqa: E402
missing = sorted(set(_INTENT_MAP.values()) - dispatch)
check("no adapter output lacks an executor branch", not missing, str(missing))
check("cancel_pending handler exists",
      "_handle_cancel_pending" in exec_src)
check("close_partial handler exists", "_handle_close_partial" in exec_src)
check("unhandled types are logged, not silently dropped",
      "no handler for signal_type" in exec_src)

# ═══════════════════════════════════════════════════════════════════════════
print("\nadapter: management intents")
# ═══════════════════════════════════════════════════════════════════════════
pg = parser_for("-1001643924999")
s = adapt(pg.parse("Last adjustment of SL to 4054"), "XAUUSD")
check("MOVE_SL_PRICE -> sl_correction with new_sl",
      s and s.signal_type == "sl_correction" and s.new_sl == 4054.0,
      str((s.signal_type, s.new_sl) if s else None))
s = adapt(pg.parse("Close 50% of the position now"), "XAUUSD")
check("CLOSE_PARTIAL -> close_partial with a fraction",
      s and s.signal_type == "close_partial" and s.close_fraction == 0.5,
      str((s.signal_type, s.close_fraction) if s else None))
s = adapt(pg.parse("Cancel all buys orders . H1 bearish"), "XAUUSD")
check("CANCEL_PENDING -> cancel_pending, direction lowercased",
      s and s.signal_type == "cancel_pending" and s.direction == "buy",
      str((s.signal_type, s.direction) if s else None))
s = adapt(pg.parse("Move SL to BE TAKE profits"), "XAUUSD")
check("MOVE_SL_BE -> breakeven", s and s.signal_type == "breakeven")
# Was "Sell 4420/4425 / TP1 4416 / Stop Loss 4334". Since 2026-08-30 that one
# is REPAIRED rather than refused - both targets sit on the correct side, so
# the stop is read as a typo and synthesised. Use a message that is internally
# inconsistent instead: a SELL whose targets are also above the entry cannot be
# rescued by any rule and must still reach the adapter as nothing.
check("refused signals adapt to None",
      adapt(p.parse("XAUUSD Sell 4420/4425\nTP1: 4460\nTP2: 4480\n"
                    "Stop Loss 4334"), "XAUUSD") is None)
check("chatter adapts to None",
      adapt(pg.parse("Good morning everyone"), "XAUUSD") is None)

# ═══════════════════════════════════════════════════════════════════════════
print("\nadapter: order types the MT5 bridge cannot place")
# ═══════════════════════════════════════════════════════════════════════════
rstop = pg.parse("Gold buy stop 4420\nSL 4405\nTP 4450")
check("parser produced a stop order",
      rstop.signal and rstop.signal.order_type == OrderType.BUY_STOP,
      str(rstop.signal.order_type if rstop.signal else None))
check("adapter refuses it rather than placing a limit on the wrong side",
      adapt(rstop, "XAUUSD") is None)
rlim = pg.parse("Gold sell limit 4415\nSL 4425\nTP 4400")
sl_ = adapt(rlim, "XAUUSD")
check("limit order keeps its entry price",
      sl_ and sl_.entry_type == "limit" and sl_.entry_price == 4415.0,
      str((sl_.entry_type, sl_.entry_price) if sl_ else None))
rmkt = pg.parse("Gold sell now\nSL 4425\nTP 4400")
sm = adapt(rmkt, "XAUUSD")
check("market order leaves entry_price None so the executor uses live price",
      sm and sm.entry_type == "market" and sm.entry_price is None,
      str((sm.entry_type, sm.entry_price) if sm else None))

# ═══════════════════════════════════════════════════════════════════════════
print("\nbroker symbol suffix is not rewritten by the alias map")
# ═══════════════════════════════════════════════════════════════════════════
pc = parser_for("-1003894833035", symbol="XAUUSDc")
rc = pc.parse("GOLD Sell 4420/4425\nTP1: 4416\nStop Loss 4434")
check("XAUUSDc channel keeps its broker suffix",
      rc.signal and rc.signal.symbol == "XAUUSDc",
      str(rc.signal.symbol if rc.signal else None))
sc = adapt(rc, "XAUUSDc")
check("adapter passes the channel symbol through",
      sc and sc.symbol == "XAUUSDc", str(sc.symbol if sc else None))

# ═══════════════════════════════════════════════════════════════════════════
print("\nlistener path end to end (stubbed bridge + executor)")
# ═══════════════════════════════════════════════════════════════════════════
from channels.channel_listener import ChannelListener              # noqa: E402


class FakeBridge:
    def __init__(self, price): self.price = price; self.calls = []

    async def get_price(self, symbol, direction):
        self.calls.append((symbol, direction)); return self.price


class FakeExecutor:
    def __init__(self, bridge): self.bridge = bridge; self.executed = []

    async def execute(self, signal, channel, message_id=0):
        self.executed.append(signal)


class FakeMsg:
    def __init__(self, text, mid=1):
        self.text = text; self.id = mid
        self.date = datetime.now(timezone.utc)
        self.reply_to_msg_id = None
        self.sender_id = 42


def run_listener(text, price, channel):
    bridge = FakeBridge(price)
    ex = FakeExecutor(bridge)
    lis = ChannelListener(channel, None, None, ex, None)
    asyncio.run(lis._process(FakeMsg(text)))
    return ex.executed, bridge


gold_ch = [c for c in config._load_channels() if c.id == "-1002070727316"][0]
done, br = run_listener(
    "XAUUSD Sell 4420/4425\nTP1: 4416\nTP 2:4412\nStop Loss 4434", 4421.0, gold_ch)
check("a clean signal reaches the executor", len(done) == 1, str(len(done)))
check("executor receives lowercase direction",
      done and done[0].direction == "sell", str(done[0].direction if done else None))
check("live price was fetched using the channel's symbol",
      br.calls == [(gold_ch.symbol, "sell")], str(br.calls))

# A MARKET order whose quote has run away must still be refused. Use "now" so
# the order fills immediately; a limit is SUPPOSED to sit away from the market.
done2, _ = run_listener(
    "XAUUSD Sell now 4420\nTP1: 4416\nTP 2:4412\nStop Loss 4434", 4300.0, gold_ch)
check("MARKET order far from its quote is refused",
      len(done2) == 0, str([d.signal_type for d in done2]))

done3, _ = run_listener("How much profits you closeddd ?", 4421.0, gold_ch)
check("member question does not reach the executor", len(done3) == 0)

# ═══════════════════════════════════════════════════════════════════════════
print("\nthe AI path is fenced too (sanity band + per-channel off switch)")
# ═══════════════════════════════════════════════════════════════════════════
# Until now max_entry_deviation lived only in the deterministic parser, and the
# AI path did not go through it, so NOTHING checked an AI-parsed price. That
# matters on mixed feeds: -1003353497021 posts Indian index options
# ("BUY DIXON 14500 CE ABOVE 391") alongside gold, the deterministic layer
# correctly abstains, and the AI is then handed the message with
# default_symbol=XAUUSDm.
from core.ai_parser import ParsedSignal as _PS                       # noqa: E402


class FakeAI:
    """Stands in for the model: returns whatever trade it was told to."""
    def __init__(self, sig): self.sig = sig; self.calls = 0

    async def parse(self, text, **kw):
        self.calls += 1
        return self.sig


class FakeDB:
    def __init__(self): self.skipped = []

    def log_skipped_signal(self, cid, mid, kind, why):
        self.skipped.append((cid, mid, kind, why))


def run_ai_listener(text, price, channel, sig):
    bridge = FakeBridge(price)
    ex = FakeExecutor(bridge)
    ex.db = FakeDB()
    ai = FakeAI(sig)
    lis = ChannelListener(channel, None, ai, ex, None)
    asyncio.run(lis._process(FakeMsg(text)))
    return ex.executed, ai, ex.db


def _mk_channel(base, **parser_over):
    ch = copy.deepcopy(base)
    ch.parser = dict(base.parser); ch.parser.update(parser_over)
    return ch


import copy                                                          # noqa: E402

# Both of these are real messages from -1003353497021, and both come out of the
# deterministic parser as UNKNOWN, which is the only intent that reaches the AI.
# Using a text that parses to CHATTER or CLOSE_PARTIAL would make these tests
# pass without the AI path ever running.
OPTION_MSG = "BUY DIXON 14500 CE ABOVE 391"       # Indian equity option
REMARK_MSG = "Gold buy support 4307 k pas"        # Hinglish commentary
for _t in (OPTION_MSG, REMARK_MSG):
    assert should_fallback(parser_for("-1002070727316").parse(_t, 1, None)), \
        f"test precondition broken: {_t!r} no longer routes to the AI"

_option = _PS(signal_type="entry", raw_text="", symbol="XAUUSDm",
              direction="buy", entry_price=391.0, entry_type="market",
              stop_loss=385.0, take_profits=[425.0, 545.0], confidence=0.9)
_real = _PS(signal_type="entry", raw_text="", symbol="XAUUSDm",
            direction="buy", entry_price=4420.0, entry_type="market",
            stop_loss=4412.0, take_profits=[4428.0], confidence=0.9)

band_ch = _mk_channel(gold_ch, ai_sanity_band_pct=5.0, ai_fallback=True)
d, ai, db = run_ai_listener(OPTION_MSG, 4421.0, band_ch, _option)
check("AI was consulted on a message the parser abstained on", ai.calls == 1)
check("an AI trade priced like a different instrument is refused",
      len(d) == 0, str([x.entry_price for x in d]))
check("...and the refusal is recorded for the scorecard",
      len(db.skipped) == 1 and "different instrument" in db.skipped[0][3],
      str(db.skipped))

d, ai, db = run_ai_listener(REMARK_MSG, 4421.0, band_ch, _real)
check("an AI trade priced like gold still executes",
      ai.calls == 1 and len(d) == 1, f"calls={ai.calls} executed={len(d)}")
check("...and nothing is recorded as skipped", not db.skipped, str(db.skipped))

# The band cannot save you from commentary that quotes REAL gold levels: the
# line above is a remark, not an order, and 4307 is a perfectly plausible
# price. For those channels the fallback is switched off entirely.
off_ch = _mk_channel(gold_ch, ai_fallback=False)
d, ai, db = run_ai_listener(REMARK_MSG, 4310.0, off_ch, _real)
check("ai_fallback=false never calls the model", ai.calls == 0)
check("...and nothing is executed", len(d) == 0, str(len(d)))

d, ai, _ = run_ai_listener(REMARK_MSG, 4310.0, _mk_channel(gold_ch), _real)
check("the default is unchanged: fallback stays ON when unset", ai.calls == 1)

check("a band of 0 disables the check rather than refusing everything",
      len(run_ai_listener(OPTION_MSG, 4421.0,
                          _mk_channel(gold_ch, ai_sanity_band_pct=0),
                          _option)[0]) == 1)
check("no quote means the band fails open, like the slippage gate",
      len(run_ai_listener(OPTION_MSG, None, band_ch, _option)[0]) == 1)
check("management intents are not price-checked",
      len(run_ai_listener(OPTION_MSG, 4421.0, band_ch,
                          _PS(signal_type="breakeven", raw_text="",
                              symbol="XAUUSDm", direction="buy",
                              confidence=0.9))[0]) == 1)

# ═══════════════════════════════════════════════════════════════════════════
print("\nAI fallback gets channel-specific context")
# ═══════════════════════════════════════════════════════════════════════════
from core.ai_parser import (AIParser, build_channel_hint,          # noqa: E402
                            _CLASSIFY_PROMPT, _DESTRUCTIVE_TYPES)

gh = [c for c in chans if c.id == "-1002070727316"][0]
hint = build_channel_hint(gh.name, gh.parser)
check("hint names the channel", gh.name in hint)
check("hint carries the template", "zone_slash" in hint, hint[:80])
check("hint carries a real example", "4574/4578" in hint)
check("hint carries the per-channel ai_hint",
      "NEVER posts close or cancel" in hint)
check("hint states the conditional-close rule",
      "conditional or optional phrasing" in hint)
check("maintainer-facing _risk_notes are NOT sent to the model",
      "normalize()" not in hint and "geometry gate" not in hint)

wsb = [c for c in chans if "Wall Street" in c.name][0]
hint_wsb = build_channel_hint(wsb.name, wsb.parser)
check("follow_conditional_close flips the rule for Wall Street Ben",
      "IS a genuine close" in hint_wsb)
gt = [c for c in chans if "GTMO" in c.name][0]
check("assembler channels are told posts may be fragments",
      "splits one trade across several" in build_channel_hint(gt.name, gt.parser))
check("a channel with no profile still produces a usable hint",
      bool(build_channel_hint("Some New Channel", {}).strip()))

filled = _CLASSIFY_PROMPT.format(text="Close half now", channel_hint=hint,
                                 default_symbol="XAUUSDc",
                                 destructive_floor=0.90)
check("prompt renders with no leftover placeholders",
      "{" not in filled.replace('{"type"', "X").replace("}", ""),
      filled[-160:])
check("prompt carries the channel hint", "zone_slash" in filled)
check("prompt uses the channel's broker symbol", "XAUUSDc" in filled)
check("prompt teaches the new intents",
      "close_partial" in filled and "cancel_pending" in filled)
check("prompt tells the model prices are not its job",
      "never used to set an entry" in filled)

# ═══════════════════════════════════════════════════════════════════════════
print("\nAI destructive-intent confidence floor")
# ═══════════════════════════════════════════════════════════════════════════
check("close_all is treated as destructive", "close_all" in _DESTRUCTIVE_TYPES)
check("cancel_pending is treated as destructive",
      "cancel_pending" in _DESTRUCTIVE_TYPES)
check("entry is NOT (it has its own SL/TP/lot gates)",
      "entry" not in _DESTRUCTIVE_TYPES)


class StubAI(AIParser):
    def __init__(self, payload):
        super().__init__("ollama", "", "", "", "phi3")
        self._active = "ollama"
        self.payload = payload
        self.seen_prompt = None

    async def _call_provider(self, provider, prompt):
        self.seen_prompt = prompt
        return json.dumps(self.payload)


def ai_parse(payload, text="ambiguous wording the regex missed", hint=""):
    ai = StubAI(payload)
    sig = asyncio.run(ai.parse(text, default_symbol="XAUUSD", channel_hint=hint))
    return sig, ai


s, _ = ai_parse({"type": "close_all", "direction": None, "confidence": 0.62})
check("low-confidence AI close_all is downgraded to unknown",
      s.signal_type == "unknown", s.signal_type)
s, _ = ai_parse({"type": "close_all", "direction": None, "confidence": 0.95})
check("high-confidence AI close_all survives", s.signal_type == "close_all",
      s.signal_type)
s, _ = ai_parse({"type": "cancel_pending", "direction": "buy", "confidence": 0.80})
check("low-confidence AI cancel_pending is downgraded",
      s.signal_type == "unknown", s.signal_type)
_, ai = ai_parse({"type": "unknown", "confidence": 0.5}, hint=hint)
check("the channel hint actually reaches the provider call",
      ai.seen_prompt and "zone_slash" in ai.seen_prompt)

# ═══════════════════════════════════════════════════════════════════════════
print("\nclock skew detection (live log 2026-08-18: exactly 7h)")
# ═══════════════════════════════════════════════════════════════════════════
from datetime import timedelta                                     # noqa: E402
from core.clock_sync import ClockSkew, humanise                    # noqa: E402

U = timezone.utc
skew = ClockSkew()
# Reproduces the real incident: machine's UTC is 7h ahead of Telegram's.
for wall, mtime in [("11:56:54", "11:50:34"), ("12:00:13", "11:59:28")]:
    hh, mm, ss = map(int, wall.split(":"))
    machine = datetime(2026, 8, 18, hh, mm, ss, tzinfo=U) + timedelta(hours=7)
    h2, m2, s2 = map(int, mtime.split(":"))
    skew.observe(datetime(2026, 8, 18, h2, m2, s2, tzinfo=U), now=machine)

check("skew is detected", skew.is_significant, str(skew.estimate))
check("estimate lands on 7h, not the raw per-message age",
      abs(skew.estimate - 25200) < 60, f"{skew.estimate:.0f}")
d = skew.diagnosis()
check("diagnosis names the size", "7h00m ahead" in d, d)
check("diagnosis says TIMEZONE, not drift", "TIMEZONE configuration fault" in d, d)
check("diagnosis says why nothing is trading",
      "refused as stale" in d, d)

healthy = ClockSkew()
base = datetime(2026, 8, 18, 12, 0, 0, tzinfo=U)
for lag in (0.4, 1.2, 0.3, 8.0):
    healthy.observe(base, now=base + timedelta(seconds=lag))
check("normal delivery lag is NOT flagged", not healthy.is_significant,
      str(healthy.estimate))
check("healthy clock produces no diagnosis", healthy.diagnosis() is None)
check("healthy clock never shifts time", healthy.adjusted_now() is None)

drift = ClockSkew()
drift.observe(base, now=base + timedelta(seconds=437))
check("a non-whole-hour offset is reported as drift, not timezone",
      "genuine clock drift" in drift.diagnosis(), drift.diagnosis())

check("compensation is OFF by default", ClockSkew().compensate is False)
comp = ClockSkew(compensate=True)
t_msg = datetime(2026, 8, 18, 11, 50, 34, tzinfo=U)
t_machine = datetime(2026, 8, 18, 18, 56, 54, tzinfo=U)
comp.observe(t_msg, now=t_machine)
adj = comp.adjusted_now(now=t_machine)
check("with compensation on, age collapses to the real delivery lag",
      adj is not None and abs((adj - t_msg).total_seconds()) < 2,
      str(adj))
comp_ok = ClockSkew(compensate=True)
comp_ok.observe(base, now=base + timedelta(seconds=0.5))
check("compensation still does nothing when the clock is fine",
      comp_ok.adjusted_now() is None)
check("humanise reads naturally", humanise(-25200).startswith("7h00m behind"),
      humanise(-25200))

# The listener must survive a bad clock and say why, not just refuse.
lis_src = open(os.path.join(ROOT, "channels", "channel_listener.py"),
               encoding="utf-8").read()
check("listener calibrates before judging staleness",
      "self.clock.observe(msg_dt)" in lis_src)
check("listener passes the adjusted reference into the parser",
      "now=now_ref" in lis_src)
check("listener escalates a staleness refusal to the clock",
      "most likely NOT the signal" in lis_src)

bad_ch = [c for c in chans if c.id == "-1002070727316"][0]
lis = ChannelListener(bad_ch, None, None, FakeExecutor(FakeBridge(4421.0)), None)
lis.clock.observe(datetime(2026, 8, 18, 11, 50, 34, tzinfo=U),
                  now=datetime(2026, 8, 18, 18, 56, 54, tzinfo=U))
check("a listener on a bad clock reports it", lis.clock.is_significant)
check("...and does NOT silently compensate", lis.clock.adjusted_now() is None)

# ═══════════════════════════════════════════════════════════════════════════
print("\nbroker-visible attribution (order comment)")
# ═══════════════════════════════════════════════════════════════════════════
from core.signal_executor import order_comment                    # noqa: E402

c1 = order_comment("-1001643924999", "SIG-77270-A3F9C1", "t2")
check("comment carries the channel id", "1643924999" in c1, c1)
check("comment carries the signal fragment", "A3F9C1" in c1, c1)
check("comment carries the leg", c1.endswith("t2"), c1)
check("comment fits the MT5 31-char limit",
      all(len(order_comment(c["id"], "SIG-999999999-0A1B2C", "run")) <= 31
          for c in CFG["channels"]),
      max((order_comment(c["id"], "SIG-999999999-0A1B2C", "run")
           for c in CFG["channels"]), key=len))
ids = {order_comment(c["id"], "", "") for c in CFG["channels"]}
check("every configured channel gets a distinct tag",
      len(ids) == len(CFG["channels"]), str(sorted(ids)))
check("the tag does not depend on the channel NAME",
      order_comment("-1001643924999", "SIG-1-ABC", "t1")
      == order_comment("-1001643924999", "SIG-1-ABC", "t1"))
# Match CALL SITES only. The old form is quoted in order_comment's docstring
# to explain why it was replaced, so a whole-file grep matches the explanation.
_comment_args = re.findall(r"comment=([^\n)]+)", exec_src)
check("every order comment is built by order_comment()",
      _comment_args and all("order_comment(" in a for a in _comment_args),
      str([a for a in _comment_args if "order_comment(" not in a]))
check("no call site still tags by channel name",
      not any("channel.name" in a for a in _comment_args),
      str(_comment_args))

# ═══════════════════════════════════════════════════════════════════════════
print("\nDB already attributes every signal and position to a channel")
# ═══════════════════════════════════════════════════════════════════════════
db_src = open(os.path.join(ROOT, "db", "database.py"), encoding="utf-8").read()
_sig_ddl = db_src.split("CREATE TABLE IF NOT EXISTS signals")[1].split(");")[0]
check("signals table stores channel_id and channel_name",
      "channel_id" in _sig_ddl and "channel_name" in _sig_ddl)
check("positions table stores channel_id",
      "channel_id" in db_src.split("CREATE TABLE IF NOT EXISTS positions")[1]
      .split(");")[0])
_pos_ddl = db_src.split("CREATE TABLE IF NOT EXISTS positions")[1].split(");")[0]
check("positions table stores pnl for scoring", "pnl" in _pos_ddl)
check("positions table stores lot_size and status for scoring",
      "lot_size" in _pos_ddl and "status" in _pos_ddl)
check("executor writes channel_id on the signal",
      "channel_id=channel.id" in exec_src)

# ═══════════════════════════════════════════════════════════════════════════
print("\nrunner trailing (the leg that opens with no take profit)")
# ═══════════════════════════════════════════════════════════════════════════
import core.position_monitor as PM                                 # noqa: E402


class _Row(dict):
    def __getitem__(self, k): return dict.get(self, k)


class FakeDB:
    def __init__(self, sig, positions):
        self.sig, self.positions = sig, positions
        self.sl_writes = []
    def get_signal(self, sid): return self.sig
    def get_positions_by_signal(self, sid): return self.positions
    def update_position_sl(self, ticket, sl): self.sl_writes.append((ticket, sl))


class TrailBridge:
    def __init__(self, ok=True): self.ok, self.calls = ok, []
    async def modify_position(self, ticket, sl, tp):
        self.calls.append((ticket, sl, tp)); return self.ok


def run_trail(direction, cur, runner_sl, tps_closed, entry=4400.0, orig_sl=4390.0):
    sig = _Row(direction=direction, stop_loss=orig_sl, entry_price=entry)
    legs = [_Row(tp_price=4405.0, status="closed" if i < tps_closed else "open",
                 ticket=100 + i) for i in range(2)]
    runner = _Row(tp_price=0.0, status="open", ticket=999, signal_id="S1",
                  channel_id="c", entry_price=entry, stop_loss=runner_sl)
    db = FakeDB(sig, legs + [runner])
    br = TrailBridge()
    mon = PM.PositionMonitor(br, db, None, None)
    mt5 = {"ticket": 999, "current_price": cur, "sl": runner_sl}
    asyncio.run(mon._trail_one(runner, mt5))
    return br.calls, db.sl_writes


calls, _ = run_trail("buy", 4430.0, 4390.0, tps_closed=0)
check("runner is NOT trailed while TP legs are still open", calls == [], str(calls))

calls, writes = run_trail("buy", 4430.0, 4390.0, tps_closed=2)
check("runner trails once every TP leg has closed", len(calls) == 1, str(calls))
check("...to price minus the signal's own stop distance",
      calls and abs(calls[0][1] - 4420.0) < 1e-6, str(calls))
check("...with tp=0 so the runner stays uncapped",
      calls and calls[0][2] == 0.0, str(calls))
check("...and the new stop is recorded in the DB", writes == [(999, 4420.0)],
      str(writes))

calls, _ = run_trail("buy", 4430.0, 4425.0, tps_closed=2)
check("stop is never widened (4425 already better than 4420)", calls == [],
      str(calls))
calls, _ = run_trail("buy", 4392.0, 4390.0, tps_closed=2)
check("stop below the step threshold is left alone", calls == [], str(calls))
calls, _ = run_trail("sell", 4370.0, 4410.0, tps_closed=2, entry=4400.0,
                     orig_sl=4410.0)
check("sell runner trails downward", len(calls) == 1 and calls[0][1] == 4380.0,
      str(calls))
calls, _ = run_trail("sell", 4370.0, 4375.0, tps_closed=2, entry=4400.0,
                     orig_sl=4410.0)
check("sell stop is never widened either", calls == [], str(calls))

check("trailing is on by default", PM.RUNNER_TRAIL_ENABLED)
check("a signal with no TP ladder is not trailed",
      run_trail("buy", 4430.0, 4390.0, tps_closed=0)[0] == [])

# ═══════════════════════════════════════════════════════════════════════════
print("\nstop orders: gated until the EA is recompiled")
# ═══════════════════════════════════════════════════════════════════════════
import core.signal_adapter as SA                                   # noqa: E402
pstop = parser_for("-1001643924999")
rstop = pstop.parse("Gold buy stop 4420\nSL 4405\nTP 4450")
check("parser still classifies it as a stop order",
      rstop.signal.order_type == OrderType.BUY_STOP)
check("adapter maps stop orders to entry_type='stop'",
      SA._entry_type(rstop.signal) == "stop")
_prev = SA.ALLOW_STOP_ORDERS
SA.ALLOW_STOP_ORDERS = False
check("refused while ALLOW_STOP_ORDERS is off",
      SA.adapt(rstop, "XAUUSDm") is None)
SA.ALLOW_STOP_ORDERS = True
_s = SA.adapt(rstop, "XAUUSDm")
check("placed once the flag is on",
      _s is not None and _s.entry_type == "stop" and _s.entry_price == 4420.0,
      str((_s.entry_type, _s.entry_price) if _s else None))
SA.ALLOW_STOP_ORDERS = _prev
check("executor can place a stop order", "place_stop_order" in exec_src)
check("bridge exposes place_stop_order",
      "place_stop_order" in open(os.path.join(ROOT, "bridge", "mt5_bridge.py"),
                                 encoding="utf-8").read())

# ═══════════════════════════════════════════════════════════════════════════
print("\nEA contract (PythonFileBridge.mq5)")
# ═══════════════════════════════════════════════════════════════════════════
ea_path = os.path.join(ROOT, "PythonFileBridge.mq5.txt")
if os.path.exists(ea_path):
    ea = open(ea_path, encoding="utf-8", errors="replace").read()
    check("EA honours lot_size on close_position (partial close)",
          "PositionClosePartial" in ea and 'GetJsonNumber(command, "lot_size")' in ea)
    check("EA refuses to leave an untradeable remainder",
          "SYMBOL_VOLUME_MIN" in ea)
    check("EA reports how much it actually closed",
          "closed_volume" in ea and "remaining_volume" in ea)
    check("EA implements buy stop orders", "ORDER_TYPE_BUY_STOP" in ea)
    check("EA implements sell stop orders", "ORDER_TYPE_SELL_STOP" in ea)
    for verb in ["place_order", "close_position", "cancel_order",
                 "modify_position", "get_all_orders", "get_all_positions",
                 "get_position", "get_deal_history"]:
        check(f"EA implements action {verb!r}", f'"{verb}"' in ea)
else:
    check("EA file present for validation", False, "PythonFileBridge.mq5.txt missing")

# ═══════════════════════════════════════════════════════════════════════════
print("\ntrailing moved to the EA (latency)")
# ═══════════════════════════════════════════════════════════════════════════
import core.position_monitor as PM2                                # noqa: E402


class ArmBridge:
    def __init__(self, supported=True):
        self.supported, self.armed, self.modifies = supported, [], []
    async def set_trailing(self, ticket, distance, step=0.0):
        if not self.supported:
            return {"status": "error", "error": "Unknown action: set_trailing"}
        self.armed.append((ticket, distance, step))
        return {"status": "success", "ticket": ticket}
    async def modify_position(self, ticket, sl, tp):
        self.modifies.append((ticket, sl, tp)); return True


def arm_run(bridge, cur=4430.0, runner_sl=4390.0, tps_closed=2):
    sig = _Row(direction="buy", stop_loss=4390.0, entry_price=4400.0)
    legs = [_Row(tp_price=4405.0, status="closed" if i < tps_closed else "open",
                 ticket=100 + i) for i in range(2)]
    runner = _Row(tp_price=0.0, status="open", ticket=999, signal_id="S1",
                  channel_id="c", entry_price=4400.0, stop_loss=runner_sl)
    db = FakeDB(sig, legs + [runner])
    mon = PM2.PositionMonitor(bridge, db, None, None)
    for _ in range(3):          # three monitor cycles
        asyncio.run(mon._trail_one(runner, {"ticket": 999, "current_price": cur,
                                            "sl": runner_sl}))
    return mon


br = ArmBridge(supported=True)
mon = arm_run(br)
check("runner is handed to the EA, not trailed from Python",
      len(br.armed) == 1 and not br.modifies, f"{br.armed} {br.modifies}")
check("...with the signal's own stop distance",
      br.armed and br.armed[0][1] == 10.0, str(br.armed))
check("...and is armed once, not every 5s cycle", len(br.armed) == 1,
      str(len(br.armed)))
check("bridge exposes set_trailing / clear_trailing",
      hasattr(PM2.MT5FileBridge, "set_trailing")
      and hasattr(PM2.MT5FileBridge, "clear_trailing"))

old = ArmBridge(supported=False)
mon_old = arm_run(old)
check("an EA without set_trailing falls back to Python trailing",
      old.armed == [] and len(old.modifies) >= 1, str(old.modifies))
check("...and stops re-asking the old EA", mon_old._ea_trailing is False)
check("fallback still sends tp=0 to keep the runner uncapped",
      old.modifies and old.modifies[0][2] == 0.0, str(old.modifies))

# ═══════════════════════════════════════════════════════════════════════════
print("\na pending order is not a closed position")
# ═══════════════════════════════════════════════════════════════════════════
# The bug this pins: _cycle() compared the DB's open rows against
# get_all_positions() alone. An unfilled limit lives in MT5's ORDERS pool, not
# the positions pool, so it vanished from live_tickets on the very next tick
# and was written off as "closed externally pnl=0.00" one second after being
# placed. On 2026-08-24 that hit nine of the day's fifteen signals.


class CycleDB:
    def __init__(self, rows):
        self.rows = rows
        self.closed, self.pnl_writes = [], []

    def get_all_open_positions(self, channel_id=None):
        return [r for r in self.rows if r["status"] in ("open", "pending")]

    def get_position_by_ticket(self, t):
        return next((r for r in self.rows if r["ticket"] == t), None)

    def update_position_closed(self, ticket, pnl, reason, **kw):
        self.closed.append((ticket, pnl, reason))

    def update_position_pnl(self, ticket, pnl, mfe=0.0, mae=0.0):
        self.pnl_writes.append((ticket, pnl))

    def get_signal(self, sid): return None
    def get_positions_by_signal(self, sid): return []
    def update_system_balance(self, *a, **k): pass


_SENTINEL = object()


class CycleBridge:
    def __init__(self, positions, orders, deals=_SENTINEL):
        self.positions, self.orders = positions, orders
        self.deals = ({"status": "success", "net_profit": -1.0,
                       "exit_reason": "sl"} if deals is _SENTINEL else deals)
        self.order_calls = 0

    async def get_all_positions(self): return self.positions
    async def get_all_orders(self):
        self.order_calls += 1
        return self.orders
    async def get_equity(self):        return 1000.0
    async def get_deal_history(self, t): return self.deals


def _pm_src_for_orders():
    return open(os.path.join(ROOT, "core", "position_monitor.py"),
                encoding="utf-8").read()


def run_cycle(positions, orders, deals=_SENTINEL):
    rows = [_Row(ticket=555, signal_id="S1", channel_id="c", status="open",
                 tp_index=1, tp_price=4660.0, entry_price=4643.0,
                 stop_loss=4628.0, lot_size=0.01, mfe=0.0, mae=0.0)]
    db = CycleDB(rows)
    mon = PM2.PositionMonitor(CycleBridge(positions, orders, deals),
                              db, None, None)
    asyncio.run(mon._cycle())
    return db


db_ = run_cycle(positions=[], orders=[{"ticket": 555}])
check("an unfilled limit order is NOT recorded as closed",
      db_.closed == [], str(db_.closed))
check("...and no P&L is invented for it", db_.pnl_writes == [], str(db_.pnl_writes))

db_ = run_cycle(positions=[{"ticket": 555, "profit": 12.0, "current_price": 4650.0}],
                orders=[])
check("once it fills it is tracked as a live position",
      db_.pnl_writes == [(555, 12.0)] and db_.closed == [],
      f"{db_.pnl_writes} {db_.closed}")

db_ = run_cycle(positions=[], orders=[])
check("gone from both pools still means closed",
      len(db_.closed) == 1, str(db_.closed))

# ── the orders pool is not always available ────────────────────────────────
# 2026-08-25: the EA on the chart never answered get_all_orders. The call timed
# out after a full 30s, 777 times in one session, and the first version of this
# fix responded by skipping the close pass entirely — so nothing could ever be
# recorded as closed. Both halves are tested here: do not close on a guess, but
# do not go blind either.
db_ = run_cycle(positions=[], orders=None, deals=None)
check("an unanswerable orders query closes nothing on a guess",
      db_.closed == [], str(db_.closed))

db_ = run_cycle(positions=[], orders=None,
                deals={"status": "success", "net_profit": -3.5,
                       "exit_reason": "sl"})
check("...but a per-ticket deal history still detects the real close",
      len(db_.closed) == 1 and db_.closed[0][1] == -3.5, str(db_.closed))

bridge = CycleBridge([], None)
rows = [_Row(ticket=555, signal_id="S1", channel_id="c", status="open",
             tp_index=1, tp_price=4660.0, entry_price=4643.0,
             stop_loss=4628.0, lot_size=0.01, mfe=0.0, mae=0.0)]
mon_ = PM2.PositionMonitor(bridge, CycleDB(rows), None, None)
for _ in range(5):
    asyncio.run(mon_._cycle())
check("a bridge that never answers get_all_orders is asked twice, not forever",
      bridge.order_calls == 2, f"{bridge.order_calls} calls in 5 cycles")
check("...and the monitor says to recompile the EA",
      mon_._ea_orders is False)
check("the ladder pass uses the same off-switch",
      "self._pending_tickets()" in _pm_src_for_orders()
      and "self.bridge.get_all_orders()" not in
      _pm_src_for_orders().split("_pending_tickets")[-1])

_pm = _pm_src_for_orders()
check("the monitor consults the orders pool inside _cycle",
      "_pending_tickets()" in _pm.split("async def _pending_tickets")[0],
      "_cycle never asks which tickets are still pending")
check("the orders pool is queried in exactly one place",
      _pm.count("self.bridge.get_all_orders()") == 1,
      f"{_pm.count('self.bridge.get_all_orders()')} call sites")

check("EA runs the trailing engine on its own timer",
      "ManageTrailingStops();" in ea and "EventSetMillisecondTimer(100)" in ea)
check("EA trailing never widens the stop",
      "newSL >= curSL + step" in ea and "newSL <= curSL - step" in ea)
check("EA trailing respects the broker minimum stop distance",
      "SYMBOL_TRADE_STOPS_LEVEL" in ea)
check("EA trailing preserves the take profit (0 stays 0)",
      "PositionModify(ticket, newSL, curTP)" in ea)
check("EA drops entries whose position is gone",
      "TrailRemoveAt(i);" in ea)
check("EA implements set_trailing / clear_trailing",
      '"set_trailing"' in ea and '"clear_trailing"' in ea)

# ═══════════════════════════════════════════════════════════════════════════
print("\nclosed-position P&L and one notification per fill")
# ═══════════════════════════════════════════════════════════════════════════
check("EA returns net_profit, not just total_profit", '\\"net_profit\\"' in ea)
check("EA nets commission and swap into it",
      "DEAL_COMMISSION" in ea and "DEAL_SWAP" in ea)
check("EA reports why the position closed",
      "DEAL_REASON" in ea and "DealReasonToText" in ea)
check("EA distinguishes tp from sl",
      'case DEAL_REASON_TP:       return "tp";' in ea
      and 'case DEAL_REASON_SL:       return "sl";' in ea)

pm_src = open(os.path.join(ROOT, "core", "position_monitor.py"),
              encoding="utf-8").read()
check("monitor reads total_profit as well as net_profit",
      '"net_profit", "total_profit", "profit"' in pm_src)
check("monitor can sum the individual deals if no total is given",
      'd.get("commission")' in pm_src and 'd.get("swap")' in pm_src)
check("monitor labels the notification by close reason",
      '"tp":' in pm_src and '"sl":' in pm_src)
check("executor no longer notifies on an operator TP-hit post",
      "_handle_tp_hit" in exec_src
      and "TP{signal.tp_number or '?'} Hit" not in exec_src)
_tp_body = exec_src.split("async def _handle_tp_hit")[1].split("async def")[0]
check("...and sends nothing at all from that handler",
      "_notify" not in _tp_body, _tp_body[:120])

check("EA exposes the broker symbol spec", '"get_symbol_info"' in ea
      and "SYMBOL_TRADE_CONTRACT_SIZE" in ea)
check("executor verifies CONTRACT_SIZE against it",
      "_verify_symbol_spec" in exec_src and "CONTRACT_SIZE mismatch" in exec_src)
check("...once per symbol, not once per signal", "_spec_checked" in exec_src)
check("...and degrades quietly on an older EA",
      "unavailable" in exec_src)
check("SIGNAL_MAGIC is documented as unused",
      "never sent" in exec_src)

# ═══════════════════════════════════════════════════════════════════════════
print("\nduplicate execution (live incident 2026-08-21, GTMO msg 44294/44295)")
# ═══════════════════════════════════════════════════════════════════════════
from core.signal_executor import SignalExecutor                    # noqa: E402


class DupDB:
    def __init__(self): self.saved, self.positions = [], []
    def get_signals_by_message(self, cid, mid, any_status=False):
        return [s for s in self.saved
                if s["channel_id"] == cid and s["message_id"] == mid
                and (any_status or s["status"] in ("pending", "open"))]
    def save_signal(self, **kw):
        self.saved.append(dict(kw, is_bare=kw.get("is_bare", 0),
                               channel_id=kw["channel_id"],
                               message_id=kw["message_id"],
                               status=kw.get("status", "open")))
        return kw["signal_id"]
    def get_system_balance(self, cid): return None
    def init_system_balance(self, cid, bal): pass
    def save_position(self, **kw): self.positions.append(kw); return len(self.positions)
    def update_position_opened(self, *a): pass
    def get_open_signals(self, *a, **k): return []
    def get_open_positions(self, *a, **k): return []
    def get_bare_signals(self, *a, **k): return []
    def update_signal_status(self, *a, **k): pass
    def update_position_closed(self, *a, **k): pass
    def get_signal(self, sid):
        return next((x for x in self.saved if x["signal_id"] == sid), None)

    # The two state-based guards. Implemented for real here rather than
    # stubbed out: the executor wraps both calls in try/except so that an
    # older database degrades gracefully, which means a double missing these
    # methods would make every test below pass without exercising anything.
    def _live_signal_ids(self):
        return {p["signal_id"] for p in self.positions
                if p.get("status", "pending") in ("pending", "open")}

    def find_live_duplicate(self, cid, direction, sl, tps,
                            exclude_message_id=None):
        live = self._live_signal_ids()
        want = sorted(float(t) for t in (tps or []))
        for s in reversed(self.saved):
            if s["channel_id"] != cid or s.get("is_bare"):
                continue
            if str(s["direction"]).lower() != str(direction).lower():
                continue
            if exclude_message_id is not None \
                    and s["message_id"] == exclude_message_id:
                continue
            if s["signal_id"] not in live:
                continue
            if abs(float(s["stop_loss"] or 0) - float(sl or 0)) > 1e-6:
                continue
            if sorted(float(t) for t in (s.get("take_profits") or [])) == want:
                return _Row(**s)
        return None

    def find_mirror_signals(self, cid, direction, sl, tps, within_sec=900):
        want = sorted(float(t) for t in (tps or []))
        out = []
        for s in self.saved:
            if s["channel_id"] == cid or s.get("is_bare"):
                continue
            if str(s["direction"]).lower() != str(direction).lower():
                continue
            if abs(float(s["stop_loss"] or 0) - float(sl or 0)) > 1e-6:
                continue
            if sorted(float(t) for t in (s.get("take_profits") or [])) == want:
                out.append(_Row(**s))
        return out

    def close_everything(self):
        """Simulate the trades finishing, so the live guard stops matching."""
        for p in self.positions:
            p["status"] = "closed"


class DupBridge:
    def __init__(self): self.orders = 0
    async def get_price(self, s, d): return 4589.0
    async def get_equity(self): return 1000.0
    async def get_symbol_info(self, s):
        return {"status": "success", "contract_size": 100.0,
                "volume_min": 0.01, "volume_step": 0.01}
    async def place_market_order(self, *a, **k):
        self.orders += 1; return {"ticket": 900000 + self.orders, "price": 4589.0}
    async def place_limit_order(self, *a, **k):
        self.orders += 1; return {"ticket": 900000 + self.orders, "price": 4589.0}


def mk_signal(sl=4579.0, tps=(4591.0, 4593.0)):
    s = ParsedSignal(signal_type="entry", symbol="XAUUSDm", direction="buy",
                     stop_loss=sl, take_profits=list(tps), entry_type="market")
    return s


def fresh():
    ch = [c for c in chans if c.id == "-1002495224665"][0]
    br = DupBridge(); db = DupDB()
    ex = SignalExecutor(br, db, None)
    return ex, br, db, ch


# same message twice, sequentially (the edit case)
ex, br, db, ch = fresh()
asyncio.run(ex.execute(mk_signal(), ch, message_id=44295))
n1 = br.orders
asyncio.run(ex.execute(mk_signal(), ch, message_id=44295))
check("an edit of an already-traded message does not trade again",
      br.orders == n1 and n1 > 0, f"{n1} then {br.orders}")

# same message twice CONCURRENTLY (the 56ms race)
ex, br, db, ch = fresh()
async def both():
    await asyncio.gather(ex.execute(mk_signal(), ch, message_id=44295),
                         ex.execute(mk_signal(), ch, message_id=44295))
asyncio.run(both())
check("two concurrent deliveries of one message trade only once",
      br.orders > 0 and len(db.saved) == 1,
      f"orders={br.orders} signals={len(db.saved)}")

# the SAME trade arriving as two DIFFERENT message ids (the repost)
ex, br, db, ch = fresh()
asyncio.run(ex.execute(mk_signal(), ch, message_id=44294))
n1 = br.orders
asyncio.run(ex.execute(mk_signal(), ch, message_id=44295))
check("an identical trade reposted under a new message id is suppressed",
      br.orders == n1, f"{n1} then {br.orders}")

# a genuinely different trade on the same channel must still go through
ex, br, db, ch = fresh()
asyncio.run(ex.execute(mk_signal(sl=4579.0), ch, message_id=44294))
n1 = br.orders
# TPs must sit ABOVE the 4589 test price or the executor correctly skips them
asyncio.run(ex.execute(mk_signal(sl=4560.0, tps=(4600.0, 4610.0)), ch,
                       message_id=44296))
check("a different setup is NOT suppressed", br.orders > n1,
      f"{n1} then {br.orders}")

# the window expires AND the first trade has finished: a genuine second trade
ex, br, db, ch = fresh()
asyncio.run(ex.execute(mk_signal(), ch, message_id=44294))
n1 = br.orders
ex._recent_trades = {k: v - (ex.duplicate_window_sec + 5)
                     for k, v in ex._recent_trades.items()}
db.close_everything()
asyncio.run(ex.execute(mk_signal(), ch, message_id=44299))
check("the same setup is allowed again once the first trade is done",
      br.orders > n1, f"{n1} then {br.orders}")

# ── layer 4: the window expires but the first order is STILL LIVE ───────────
# Gold Hunter, 2026-08-24: buy limit @4643 posted at 14:01 and again at 14:50,
# and buy limit @4621.74 at 08:32 and again at 19:40. Both reposts cleared the
# 600s fingerprint window and placed a second order on top of one that had
# never filled. The clock is the wrong question; "is it still live" is the
# right one.
ex, br, db, ch = fresh()
asyncio.run(ex.execute(mk_signal(), ch, message_id=77711))
n1 = br.orders
ex._recent_trades.clear()                      # 49 minutes later
asyncio.run(ex.execute(mk_signal(), ch, message_id=77721))
check("a repost is refused while the original order is still unfilled",
      br.orders == n1 and len(db.saved) == 1,
      f"orders {n1} then {br.orders}, signals {len(db.saved)}")

ex, br, db, ch = fresh()
asyncio.run(ex.execute(mk_signal(), ch, message_id=77703))
n1 = br.orders
ex._recent_trades.clear()                      # eleven hours later
asyncio.run(ex.execute(mk_signal(sl=4560.0, tps=(4600.0, 4610.0)), ch,
                       message_id=77768))
check("...but a genuinely different setup still goes through while it is live",
      br.orders > n1, f"{n1} then {br.orders}")

check("DUPLICATE_WINDOW_SEC is configurable", ex.duplicate_window_sec == 600)

# ═══════════════════════════════════════════════════════════════════════════
print("\nthe bridge can read a reply containing our own delimiter")
# ═══════════════════════════════════════════════════════════════════════════
# get_all_orders is the ONE action whose reply echoes text we wrote:
# ORDER_COMMENT. order_comment() used '|' as its field separator, and the
# bridge treated '|' as a record separator and split on the first one — so
# every orders reply was shredded, json.loads raised, the response file had
# ALREADY been deleted, and the poll spun to a full 30s "timeout" while the EA
# was answering correctly every single time. 777 of them on 2026-08-25.
# Recompiling the EA could never have fixed it.
from bridge.mt5_bridge import MT5FileBridge as _MB                   # noqa: E402

_rid = "signal_d7635c2c_10"
_piped = ('{"request_id":"%s","status":"success","count":2,"orders":['
          '{"ticket":3139506385,"comment":"c1643924999|5E15A7|t2"},'
          '{"ticket":3139506401,"comment":"c1643924999|5E15A7|t3"}]}' % _rid)
_r = _MB._parse_response(_piped, _rid)
check("an orders reply whose comments contain '|' now parses",
      _r is not None and _r.get("status") == "success", str(_r)[:120])
check("...with both orders intact",
      _r and len(_r["orders"]) == 2, str(_r)[:120])
check("...and the comment itself is not mangled",
      _r and _r["orders"][0]["comment"] == "c1643924999|5E15A7|t2",
      str(_r["orders"][0] if _r else None))
check("a legacy '{id}|{json}' tagged line is still understood",
      (_MB._parse_response('%s|{"status":"success","orders":[]}' % _rid, _rid)
       or {}).get("status") == "success")
check("...even when ITS payload contains a pipe",
      (_MB._parse_response(
          '%s|{"status":"success","orders":[{"comment":"a|b|c"}]}' % _rid, _rid)
       or {}).get("orders", [{}])[0].get("comment") == "a|b|c")
check("genuine garbage is reported unreadable, not parsed",
      _MB._parse_response("<html>500</html>", _rid) is None)
check("a half-written file is unreadable, so the poll looks again",
      _MB._parse_response('{"request_id":"x","stat', _rid) is None)

_br_src = open(os.path.join(ROOT, "bridge", "mt5_bridge.py"),
               encoding="utf-8").read()
check("the response file is no longer deleted before it parses",
      _br_src.index("parsed = self._parse_response")
      < _br_src.index("resp_file.unlink", _br_src.index("parsed = self._parse_response")))
check("an unparseable reply gives up instead of waiting out the timeout",
      "unparseable response" in _br_src)
check("order comments no longer embed a delimiter character",
      "|" not in order_comment("-1001643924999", "SIG-77703-5E15A7", "t2"),
      order_comment("-1001643924999", "SIG-77703-5E15A7", "t2"))
check("...and still fit MT5's 31-char comment limit",
      len(order_comment("-1001643924999", "SIG-77703-5E15A7", "t2")) <= 31)

# ═══════════════════════════════════════════════════════════════════════════
print("\nbare directional calls are taken, not binned")
# ═══════════════════════════════════════════════════════════════════════════
# 2026-08-25: GTMO posted "Gold buy now" at 09:32:22Z and price moved 100 pips
# inside a minute; the assembler buffered it and it was never emitted. Lion
# Trading posted "Sell gold" at 07:36:16Z, 87 seconds before the full signal;
# it was binned as chatter by the len(t) < 25 rule. Both head starts were lost,
# and by the time the levels arrived the fill was worse than the operator's.
_pp = parser_for("-1002495224665")
for _t in ("Gold buy now", "Sell gold", "GOLD SELL NOW", "gold buy now guys",
           "Lets scalping SELL gold slowly high risk (SCALPING SETUP)"):
    _r = _pp.parse(_t, 1, None)
    check(f"bare call {_t[:34]!r} is a PRE_SIGNAL",
          _r.intent == Intent.PRE_SIGNAL and not _r.rejected_reasons,
          f"{_r.intent.value} {_r.rejected_reasons}")

# Precision matters more than recall here: a false positive opens a real
# position with no levels.
for _t in ("when you say buy 1 minutes next flyyyyying",
           "Did u all make profits", "I made profit bro",
           "How much profits you closeddd ?", "Incredible entry bro wow",
           "my sl 4630 got hit", "Zero floating MO 3 Tp's Done"):
    check(f"chatter {_t[:34]!r} is NOT a pre-signal",
          _pp.parse(_t, 1, None).intent != Intent.PRE_SIGNAL)
for _t in ("Gold buy now 4633 SL 4627 TP 4636",
           "XAUUSD Sell 4420/4425\nTP1: 4416\nStop Loss 4434"):
    check(f"a full signal stays a full signal, not a pre-signal",
          _pp.parse(_t, 1, None).intent == Intent.NEW_SIGNAL)

check("a stale bare call is refused — the move already happened",
      bool(parser_for("-1002495224665", max_signal_age_sec=300).parse(
          "Gold buy now", 1,
          datetime.now(timezone.utc) - timedelta(seconds=1200)).rejected_reasons))

check("the adapter routes it to the pre-announcement handler",
      adapt(_pp.parse("Gold buy now", 1, None), "XAUUSDm").signal_type
      == "pre_announcement")
check("...carrying the direction",
      adapt(_pp.parse("Sell gold", 1, None), "XAUUSDm").direction == "sell")

# The assembler must emit it AND keep buffering, so the levels still assemble.
_asm = SignalAssembler(parser_for("-1002495224665", assemble_window_sec=900))
_t0 = datetime(2026, 8, 25, 9, 32, 22, tzinfo=timezone.utc)
_first = _asm.feed("Gold buy now", 1, _t0)
check("the assembler emits the bare call instead of swallowing it",
      _first is not None and _first.intent == Intent.PRE_SIGNAL,
      str(_first.intent.value if _first else None))
check("...and still holds the fragment open", _asm._pending is not None)
_full = _asm.feed("SL 4600\nTP 4650", 2, _t0 + timedelta(seconds=60))
check("...so the levels still assemble into the full signal",
      _full is not None and _full.intent == Intent.NEW_SIGNAL,
      str(_full.intent.value if _full else None))

# The executor must give it a real stop and refuse a repeat.
class PreDB(DupDB):
    def get_bare_signals(self, channel_id=None):
        return [s for s in self.saved
                if s.get("is_bare") and s["status"] in ("pending", "open")
                and (channel_id is None or s["channel_id"] == channel_id)]
    def update_signal_status(self, sid, status, notes=""):
        for s in self.saved:
            if s["signal_id"] == sid:
                s["status"] = status


class PreBridge(DupBridge):
    def __init__(self, price=4589.0):
        super().__init__(); self.price = price; self.market = []
    async def get_price(self, s, d): return self.price
    async def place_market_order(self, symbol, direction, lot, sl=0.0, tp=0.0,
                                 comment=""):
        self.orders += 1
        self.market.append({"direction": direction, "lot": lot, "sl": sl})
        return {"ticket": 900000 + self.orders, "price": self.price}


def pre_run(direction="buy", price=4589.0, chan=None):
    br = PreBridge(price); db = PreDB()
    ex = SignalExecutor(br, db, None)
    ch = chan or [c for c in chans if c.id == "-1002495224665"][0]
    sig = ParsedSignal(signal_type="pre_announcement", symbol="XAUUSDm",
                       direction=direction, raw_text="Gold buy now")
    asyncio.run(ex.execute(sig, ch, message_id=44495))
    return ex, br, db, ch, sig


ex, br, db, ch, sig = pre_run("buy", 4589.0)
check("a pre-signal opens a position", br.orders >= 1, str(br.orders))
check("...at minimum size, not risk size",
      all(o["lot"] == ex.min_lot for o in br.market), str(br.market))
check("...with a real protective stop, not sl=0",
      br.market and br.market[0]["sl"] > 0, str(br.market))
_pips = float(ch.parser.get("pre_signal_sl_pips", 100))
_pv = float(ch.parser.get("pip_value", 0.1))
check(f"...{_pips:.0f} pips below a buy",
      br.market and abs(br.market[0]["sl"] - (4589.0 - _pips * _pv)) < 1e-6,
      str(br.market[0]["sl"] if br.market else None))
_, br2, _, _, _ = pre_run("sell", 4589.0)
check("...and above a sell",
      br2.market and abs(br2.market[0]["sl"] - (4589.0 + _pips * _pv)) < 1e-6,
      str(br2.market[0]["sl"] if br2.market else None))

n = br.orders
asyncio.run(ex.execute(sig, ch, message_id=44496))
check("a repeat of the same bare call while it is open opens nothing more",
      br.orders == n, f"{n} then {br.orders}")


class NoPriceBridge(PreBridge):
    async def get_price(self, s, d): return None


_ex2 = SignalExecutor(NoPriceBridge(), PreDB(), None)
asyncio.run(_ex2.execute(sig, ch, message_id=44497))
check("no quote means no blind entry at all",
      _ex2.bridge.orders == 0, str(_ex2.bridge.orders))

# ── breakeven that survives noise ───────────────────────────────────────────
_be_src = exec_src if False else open(
    os.path.join(ROOT, "core", "signal_executor.py"), encoding="utf-8").read()
check("breakeven honours slack_pips", "slack_pips" in _be_src)
check("breakeven honours min_profit_pips", "min_profit_pips" in _be_src)
check("both default to 0 so an unset channel is unchanged",
      'be.get("slack_pips", 0)' in _be_src
      and 'be.get("min_profit_pips", 0)' in _be_src)
_gt = [c for c in chans if c.id == "-1002495224665"][0]
check("channels.json ships a non-zero default for both",
      float(_gt.breakeven.get("slack_pips", 0)) > 0
      and float(_gt.breakeven.get("min_profit_pips", 0)) > 0,
      str({k: v for k, v in _gt.breakeven.items() if not k.startswith("_")}))

# ── the bare watcher scales out instead of cutting at 15 min ────────────────
_bw = open(os.path.join(ROOT, "core", "bare_trade_watcher.py"),
           encoding="utf-8").read()
check("the 15-minute hard close is gone", "BARE_TIMEOUT_SECONDS" not in _bw)
check("it scales out on a timer", "BARE_PARTIAL_MINUTES" in _bw)
import core.bare_trade_watcher as BW                                # noqa: E402
check("...every 39 minutes by default", BW.BARE_PARTIAL_MINUTES == 39.0,
      str(BW.BARE_PARTIAL_MINUTES))
check("...and goes flat after a bounded number of steps",
      BW.BARE_MAX_STEPS >= 2 and "_close_remaining" in _bw)
check("a partial records what is LEFT, not the original size",
      "update_position_lot" in _bw
      and "def update_position_lot" in open(
          os.path.join(ROOT, "db", "database.py"), encoding="utf-8").read())
check("a leg too small to halve is closed rather than silently skipped",
      "cut < min_lot" in _bw)

# ── a withheld stop can be synthesised, on the correct side of the zone ─────
# Lucas Gold Legend hides the stop behind his paywall on 42% of his calls
# ("SL : VIP"). Everything else in those messages parses: direction, zone, all
# targets, the "TP4 : open" runner. default_sl_pips fills the gap — but it used
# to measure from s.entry, which for a sell zone is the LOW edge, putting the
# stop inside the operator's own entry band.
_lz = parser_for("-1002393457678", default_sl_pips=80)
_sig = ("GOLD SELL NOW\n\nGOLD SELL ZONE 4580-85\n\nTP1 : 4575\nTP2 : 4570\n"
        "TP3 : 4565\nTP4 : open\n\nSL : VIP")
_r = _lz.parse(_sig, 1, None)
check("a withheld stop no longer refuses the whole signal",
      _r.executable, str(_r.rejected_reasons))
check("...the stop goes ABOVE the zone high on a sell, not above the fill",
      abs(_r.signal.sl - 4593.0) < 1e-6,
      f"sl={_r.signal.sl} zone_high={_r.signal.entry_high} (4585+8 = 4593)")
check("...and the trade is tagged as inferred, not presented as posted",
      any("withheld" in n for n in _r.notes), str(_r.notes))
_rb = parser_for("-1002393457678", default_sl_pips=80).parse(
    "GOLD BUY NOW\n\nGOLD BUY ZONE 4580-85\n\nTP1 : 4595\nTP2 : 4600\n\nSL : VIP",
    1, None)
check("...mirrored for a buy: below the zone LOW",
      abs(_rb.signal.sl - 4572.0) < 1e-6,
      f"sl={_rb.signal.sl} zone_low={_rb.signal.entry_low} (4580-8 = 4572)")
check("a channel without default_sl_pips still refuses a withheld stop",
      not parser_for("-1002393457678", default_sl_pips=0).parse(
          _sig, 1, None).executable,
      "0 must mean refuse, so the setting stays reversible with one key")

# ── upgrading a bare position must send a VALID stop and target ─────────────
# 2026-08-28, Forex Expert Team: "Gold sell now" opened a bare at 4590.94, then
# the full signal arrived with TPs [4605,4600,4595,4580] — the first three
# already behind the market. The upgrade took tps[2]=4595 blindly and asked the
# broker for a take profit ABOVE a sell fill. Rejected; the bare kept its wide
# protective stop and stopped out. Twice in three minutes, -19.70.
_ub_src = open(os.path.join(ROOT, "core", "signal_executor.py"),
               encoding="utf-8").read()
_ub = _ub_src.split("async def _upgrade_bare_trades")[1].split("async def ")[0]
check("the upgrade no longer picks tps[2] blindly",
      "tps[min(2, len(tps) - 1)]" not in _ub)
check("...it skips targets the fill has already passed",
      "_tp_passed(direction, fill, t)" in _ub)
check("...aims at the nearest target still ahead", "live[0]" in _ub)
check("...and refuses a stop already through the fill",
      "_tp_passed(direction, fill, sl)" in _ub)
check("...leaving the protective stop it opened with, rather than none",
      "keeping the" in _ub and "protective stop" in _ub)

# ── cross-channel: ALWAYS execute, no exceptions ────────────────────────────
# Each channel carries its own system_balance and is being graded against the
# others, so the same setup relayed by four channels is four independent
# results. Nothing may ever refuse a trade because another channel took it —
# that would silently delete the data the whole exercise exists to gather.
# Four channels relayed the same SELL 4636 on 2026-08-25 and all four traded.
ex, br, db, ch = fresh()
other = [c for c in chans if c.id == "-1001765226347"][0]
notes = []
ex._notify = lambda m: notes.append(m) or asyncio.sleep(0)
asyncio.run(ex.execute(mk_signal(), other, message_id=41134))
n1 = br.orders
asyncio.run(ex.execute(mk_signal(), ch, message_id=3470))
check("the same trade from a DIFFERENT channel is still taken",
      br.orders > n1, f"{n1} then {br.orders}")
check("...and both channels get their own signal row",
      len(db.saved) == 2, str(len(db.saved)))
check("the mirror notice is OFF by default", ex.mirror_window_sec == 0,
      str(ex.mirror_window_sec))
check("...so nothing is said about mirroring at all",
      not any("Mirror" in str(m) for m in notes), str(notes)[-160:])

# A third and fourth relay, each on its own distinct channel, must also go
# through. Distinct is the point: reusing a channel here would trip the
# same-channel guard and the test would pass for the wrong reason.
_relays = [c for c in chans
           if c.enabled and c.id not in (ch.id, other.id)][:2]
check("the test has two more genuinely distinct channels",
      len({c.id for c in _relays}) == 2, str([c.name for c in _relays]))
for i, (relay, mid) in enumerate(zip(_relays, (3476, 17899))):
    n = br.orders
    asyncio.run(ex.execute(mk_signal(), relay, message_id=mid))
    check(f"relay #{i + 3} ({relay.name[:20]}) is taken too", br.orders > n,
          f"{n} then {br.orders}")

# Turning the notice on must still not block.
ex, br, db, ch = fresh()
ex.mirror_window_sec = 900
notes = []
ex._notify = lambda m: notes.append(m) or asyncio.sleep(0)
asyncio.run(ex.execute(mk_signal(), other, message_id=41134))
n1 = br.orders
asyncio.run(ex.execute(mk_signal(), ch, message_id=3470))
check("with the notice enabled the trade STILL executes",
      br.orders > n1, f"{n1} then {br.orders}")
check("...and it is only a heads-up",
      any("Mirror" in str(m) for m in notes), str(notes)[-160:])

# Both same-channel guards must be scoped to one channel and nothing wider.
_db_src = open(os.path.join(ROOT, "db", "database.py"), encoding="utf-8").read()
check("the live-duplicate guard is scoped to a single channel",
      "WHERE s.channel_id=?" in _db_src.split("def find_live_duplicate")[1]
      .split("def ")[0])
check("the fingerprint guard keys on channel_id",
      "str(channel_id)" in exec_src.split("_trade_fingerprint")[1][:400])
check("idempotency query can span every status",
      "any_status" in open(os.path.join(ROOT, "db", "database.py"),
                           encoding="utf-8").read())

# ═══════════════════════════════════════════════════════════════════════════
print("\nzone ladder entries")
# ═══════════════════════════════════════════════════════════════════════════
_L = SignalExecutor.__new__(SignalExecutor)


class _Ch:
    def __init__(self, parser=None, be=None):
        self.id = "-1"; self.name = "T"
        self.parser = parser or {}
        self.breakeven = be or {}


def ladder(direction, lo, hi, ntp, parser=None, is_zone=True, runner=False):
    sig = ParsedSignal(direction=direction, entry_low=lo, entry_high=hi,
                       is_zone=is_zone)
    cls = [(i + 1, 4000.0 + i) for i in range(ntp)]
    if runner:
        cls.append((ntp + 1, None))
    p = {"zone_fill": "ladder", "min_zone_width": 1.0, "max_zone_width": 30.0}
    p.update(parser or {})
    return _L._ladder_prices(sig, _Ch(p), direction, cls, 4340.0)

L = ladder("buy", 4333.0, 4339.0, 3)
check("buy zone 4333-4339 spreads three legs", sorted(L.values()) == [4333.0, 4336.0, 4339.0],
      str(L))
check("nearest leg to market (4339) carries TP1", L[1] == 4339.0, str(L))
check("deepest leg (4333) carries TP3", L[3] == 4333.0, str(L))

S = ladder("sell", 4333.0, 4339.0, 3)
check("sell zone: nearest leg (4333) carries TP1", S[1] == 4333.0, str(S))
check("sell zone: deepest leg (4339) carries TP3", S[3] == 4339.0, str(S))

R = ladder("buy", 4333.0, 4339.0, 2, runner=True)
check("a runner rides the near edge", R[3] == 4339.0, str(R))

check("no ladder unless the channel asks for it",
      ladder("buy", 4333.0, 4339.0, 3, parser={"zone_fill": "worst"}) == {})
check("no ladder when the message was not a range",
      ladder("buy", 4333.0, 4339.0, 3, is_zone=False) == {})
check("no ladder on a single take profit", ladder("buy", 4333.0, 4339.0, 1) == {})
check("no ladder on a hair-thin zone",
      ladder("buy", 4338.6, 4339.0, 3) == {})
check("no ladder on an implausibly wide zone (mis-parse guard)",
      ladder("buy", 4100.0, 4339.0, 3) == {})
check("zone width limits are per channel",
      ladder("buy", 4100.0, 4339.0, 3, parser={"max_zone_width": 300.0}) != {})
check("executor places laddered legs as limits", "zone ladder" in exec_src
      and "_ladder_prices" in exec_src)

# ═══════════════════════════════════════════════════════════════════════════
print("\nladder cancel + per-channel breakeven")
# ═══════════════════════════════════════════════════════════════════════════
pm_src = open(os.path.join(ROOT, "core", "position_monitor.py"),
              encoding="utf-8").read()
check("monitor cancels unfilled legs after a run", "_manage_ladders" in pm_src)
check("...only orders MT5 still reports as unfilled",
      "get_all_orders()" in pm_src and "ladder_cancelled" in pm_src)
check("...distance is per channel (ladder_cancel_pips)",
      "ladder_cancel_pips" in pm_src)
# ── "breakeven after TP2" must mean TP2, not TP1 ────────────────────────────
# The old test was `any leg with tp_index <= n closed at tp`, so on_tp_index=2
# armed as soon as TP1 hit and then logged "TP2 reached". The setting did the
# wrong thing AND misreported it.
import core.position_monitor as PM3                                  # noqa: E402


class BeDB:
    def __init__(self, closed):
        # closed: list of (tp_index, close_reason)
        self.closed = closed
        self.sl_writes = []
    def get_signal(self, sid):
        return _Row(signal_id=sid, direction="buy", channel_id="c",
                    channel_name="Mr David FX", stop_loss=4600.0)
    def get_positions_by_signal(self, sid):
        return [_Row(tp_index=i, status="closed", close_reason=r)
                for i, r in self.closed]
    def update_position_sl(self, t, sl): self.sl_writes.append((t, sl))


class BeBridge:
    def __init__(self): self.mods = []
    async def modify_position(self, ticket, sl, tp):
        self.mods.append((ticket, sl)); return True
    async def get_equity(self): return 1000.0


def be_run(closed, tp_idx=2, slack=0, entry=4620.0, cur=4640.0):
    ch = types.SimpleNamespace(
        id="c", name="Mr David FX", parser={"pip_value": 0.1},
        enabled=False, starting_balance=0.0,
        breakeven={"on_tp_index": tp_idx, "on_pips": 0,
                   "buffer_pips": 0, "slack_pips": slack})
    br = BeBridge()
    mon = PM3.PositionMonitor(br, BeDB(closed),
                              types.SimpleNamespace(channels=[ch]), None)
    runner = _Row(ticket=777, signal_id="S1", channel_id="c", status="open",
                  tp_index=3, tp_price=4700.0, entry_price=entry,
                  stop_loss=4600.0, mfe=0.0, mae=0.0)
    live = {777: {"ticket": 777, "current_price": cur, "sl": 4600.0}}
    asyncio.run(mon._auto_breakeven([runner], live))
    return br.mods


check("on_tp_index=2 does NOT arm when only TP1 has hit",
      be_run([(1, "tp")]) == [], str(be_run([(1, "tp")])))
check("...and DOES arm once TP2 closes at target",
      len(be_run([(1, "tp"), (2, "tp")])) == 1,
      str(be_run([(1, "tp"), (2, "tp")])))
check("a stopped-out leg does not count as a banked target",
      be_run([(1, "tp"), (2, "sl")]) == [], str(be_run([(1, "tp"), (2, "sl")])))
check("a trimmed ladder still arms once enough targets are banked",
      len(be_run([(3, "tp"), (4, "tp")])) == 1,
      "leg 2 never opened, but two targets paid")
check("on_tp_index=0 leaves the auto path off entirely",
      be_run([(1, "tp"), (2, "tp")], tp_idx=0) == [])
_m = be_run([(1, "tp"), (2, "tp")], slack=0)
check("with no slack the stop goes exactly to entry",
      _m and abs(_m[0][1] - 4620.0) < 1e-6, str(_m))
_m = be_run([(1, "tp"), (2, "tp")], slack=5)
check("slack_pips moves it BELOW entry on a buy, not above",
      _m and abs(_m[0][1] - (4620.0 - 0.5)) < 1e-6, str(_m))
check("the auto path honours slack_pips like the operator path does",
      "slack_px" in pm_src or "slack_pips" in open(
          os.path.join(ROOT, "core", "position_monitor.py"),
          encoding="utf-8").read())

check("per-channel breakeven policy exists", "_auto_breakeven" in pm_src)
check("...on TP index", "on_tp_index" in pm_src)
check("...on pips in profit", "on_pips" in pm_src)
check("...with an optional buffer", "buffer_pips" in pm_src)
check("...and never widens or crosses the market",
      "live_sl >= want" in pm_src and "want >= cur" in pm_src)
check("ChannelConfig carries a breakeven block",
      "breakeven" in open(os.path.join(ROOT, "config.py"), encoding="utf-8").read())

_cfgsrc = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
check("breakeven merges defaults with the per-channel override",
      "defaults" in _cfgsrc and "ch.get('breakeven')" in _cfgsrc)
chans2 = config._load_channels()
check("every channel loads a breakeven dict",
      all(isinstance(c.breakeven, dict) for c in chans2))
# MECHANISM, not the operator's tuning: a channel that declares no breakeven
# block must come out inert, so adding the block to one channel is opt-in and
# adding it never changes any other channel. Asserting that NO channel in
# channels.json has a policy would just red-build the moment the operator
# turns one on, which is a supported thing to do.
_inert = config._merge_breakeven({}) if hasattr(config, "_merge_breakeven") \
    else next((c.breakeven for c in chans2 if not c.breakeven.get("on_tp_index")
               and not c.breakeven.get("on_pips")), {})
check("a channel with no breakeven block stays on operator-message only",
      not _inert.get("on_tp_index") and not _inert.get("on_pips"))

# ═══════════════════════════════════════════════════════════════════════════
print("\ndaily channel scorecard")
# ═══════════════════════════════════════════════════════════════════════════
from core import daily_report as DR                                # noqa: E402
from core.daily_report import DailyReport, session_window          # noqa: E402

U2 = timezone.utc
for probe, want_start, want_end in [
    ("2026-08-21 23:30", "08-21 00:00", "08-21 21:00"),   # after NY close
    ("2026-08-22 00:30", "08-21 00:00", "08-21 21:00"),   # after Tokyo open
    ("2026-08-21 12:00", "08-20 00:00", "08-20 21:00"),   # mid-session
]:
    n = datetime.strptime(probe, "%Y-%m-%d %H:%M").replace(tzinfo=U2)
    s_, e_ = session_window(n)
    check(f"window at {probe} UTC is the last CLOSED session",
          f"{s_:%m-%d %H:%M}" == want_start and f"{e_:%m-%d %H:%M}" == want_end,
          f"{s_:%m-%d %H:%M}..{e_:%m-%d %H:%M}")

check("session runs Asian open to NY close, not midnight to midnight",
      DR.ASIAN_OPEN_UTC == 0 and DR.NY_CLOSE_UTC == 21)
check("report fires between NY close and Tokyo open",
      21 <= int(DR.REPORT_AT_UTC.split(":")[0]) <= 23, DR.REPORT_AT_UTC)


class RptDB:
    def __init__(self, rows, only=None):
        self.rows, self.only = rows, only
    class _C:
        def __init__(self, outer): self.o = outer
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args):
            class R(dict):
                def __getitem__(self, k): return dict.get(self, k)
            # Honour the channel id. A double that returns the same activity
            # for every channel cannot tell an active channel from a silent
            # one, which is precisely what the silent-channel tests check.
            if self.o.only is not None and args and args[0] != self.o.only:
                if "skipped_signals" in sql and "reason" in sql and "COUNT" not in sql:
                    return _One(None, rows=[])
                return _One(R(n=0))
            if "FROM signals" in sql:   return _One(R(n=3))
            if "skipped_signals" in sql:
                # Two shapes hit this table: the count, and the raw reasons
                # that get grouped in Python. Returning the count row for both
                # is how the whole channel row silently disappeared when the
                # reason breakdown was added.
                if "reason" in sql and "COUNT" not in sql:
                    return _One(None, rows=[
                        R(reason="message is 2806s old, max 600s"),
                        R(reason="message is 2811s old, max 600s")])
                return _One(R(n=2))
            if "status='open'" in sql:  return _One(R(n=1))
            return _One(R(n=4, wins=3, losses=1, net=42.0, gross_win=60.0,
                          gross_loss=18.0, worst=-18.0, best=30.0))
    def _conn(self): return RptDB._C(self)
    def get_system_balance(self, cid):
        return {"starting_balance": 1000.0, "system_balance": 1042.0}


class _One:
    def __init__(self, row, rows=None): self.row, self.rows = row, rows or []
    def fetchone(self): return self.row
    def fetchall(self): return self.rows


_ch = [c for c in chans if c.id == "-1002495224665"][0]
rpt = DailyReport(RptDB(None), types.SimpleNamespace(channels=[_ch]), None)
st = datetime(2026, 8, 21, 0, 0, tzinfo=U2)
en = datetime(2026, 8, 21, 21, 0, tzinfo=U2)
rows = rpt.build(st, en)
check("a channel row is produced", len(rows) == 1, str(len(rows)))
row = rows[0]
for f in ("rank", "name", "id", "signals", "wins", "losses", "flat", "net",
          "pf", "expectancy", "start_balance", "system_balance", "growth_pct",
          "still_open", "skipped"):
    check(f"row carries {f}", f in row, str(sorted(row)))
check("profit factor computed", abs(row["pf"] - (60.0 / 18.0)) < 1e-9, str(row["pf"]))
check("expectancy is per position", abs(row["expectancy"] - 10.5) < 1e-9,
      str(row["expectancy"]))
check("growth measured against the channel's own starting balance",
      abs(row["growth_pct"] - 4.2) < 1e-9, str(row["growth_pct"]))
check("flat closes are counted separately", row["flat"] == 0, str(row["flat"]))

txt = rpt.render(rows, st, en)
for must in ("Daily channel scorecard", "Asian open", _ch.name, _ch.id,
             "signals", "PF", "balance"):
    check(f"rendered report mentions {must!r}", must in txt)
check("report is ranked", "🥇" in txt)
check("empty session renders without crashing",
      "No channel activity" in rpt.render([], st, en))
check("long reports are split under the Telegram 4096 limit",
      all(len(c) <= 3900 for c in DR._split("x\n" * 5000, 3900)))
check("report can be disabled", "DAILY_REPORT_ENABLED" in
      open(os.path.join(ROOT, "core", "daily_report.py"), encoding="utf-8").read())

# ── every enabled channel is accounted for ─────────────────────────────────
# 2026-08-24 reported "12 channel(s)" with sixteen enabled, and said nothing
# about the missing four. A channel that produced nothing is a fact about the
# channel; dropping it silently is also the only way a channel the account can
# no longer read would stay invisible.
_quiet = [c for c in chans if c.enabled and c.id != _ch.id][:3]
rpt2 = DailyReport(RptDB(None, only=_ch.id),
                   types.SimpleNamespace(channels=[_ch] + _quiet), None)
rows2 = rpt2.build(st, en)
check("only channels with activity get a ranked row", len(rows2) == 1,
      str(len(rows2)))
check("...but the silent enabled ones are still identified",
      {c.id for c in rpt2.silent(rows2)} == {c.id for c in _quiet},
      str([c.name for c in rpt2.silent(rows2)]))
txt2 = rpt2.render(rows2, st, en)
check("the header accounts for every enabled channel",
      f"1 of {1 + len(_quiet)} enabled" in txt2, txt2[:200])
check("silent channels are named in the report",
      all(c.name in txt2 for c in _quiet))
check("...and the report says why that might be",
      "can no longer read" in txt2)
check("a session with no activity at all still names them",
      all(c.name in rpt2.render([], st, en) for c in _quiet))

check("skip reasons group after the numbers are normalised",
      DR._group_reasons([{"reason": "message is 2806s old, max 600s"},
                         {"reason": "message is 2811s old, max 600s"},
                         {"reason": "no stop loss in message"}])
      == [("message is Ns old, max Ns", 2), ("no stop loss in message", 1)],
      str(DR._group_reasons([{"reason": "message is 2806s old, max 600s"},
                             {"reason": "message is 2811s old, max 600s"},
                             {"reason": "no stop loss in message"}])))

# ═══════════════════════════════════════════════════════════════════════════
print("\nunreadable channels are found at startup, not a day later")
# ═══════════════════════════════════════════════════════════════════════════
# events.NewMessage(chats=<id>) accepts an id it cannot resolve and then never
# fires, so "Registered handler" is not evidence of anything. A channel that
# was left, renamed or entered with a wrong id is indistinguishable from a
# quiet one until someone reads the scorecard.
from channels.channel_manager import ChannelManager                  # noqa: E402


class StubTG:
    def __init__(self, bad=(), newest=None):
        self.bad, self.newest = set(bad), newest or {}

    async def get_entity(self, target):
        if str(target) in self.bad:
            raise ValueError(f"Cannot find any entity corresponding to {target}")
        return object()

    def iter_messages(self, ent, limit=1):
        newest = self._pending
        async def gen():
            if newest is not None:
                yield types.SimpleNamespace(date=newest)
        return gen()


def reach(channels, bad=(), ages=None):
    from datetime import timedelta as _td
    mgr = ChannelManager(types.SimpleNamespace(channels=channels), None, None)
    tg = StubTG(bad)
    mgr._client = tg
    out = {}
    for ch in channels:
        age = (ages or {}).get(ch.id, 60)
        tg._pending = (None if age is None
                       else datetime.now(timezone.utc) - _td(seconds=age))
        out.update(asyncio.run(mgr._check_reachable([ch])))
    return out


_a, _b = chans[0], chans[1]
res = reach([_a, _b], bad=(_b.id,))
check("a readable channel reports its last post",
      res[_a.id][0] == "✅" and "last post" in res[_a.id][1], str(res.get(_a.id)))
check("an unresolvable channel is flagged, not skipped",
      res[_b.id][0] == "❌" and "unreadable" in res[_b.id][1], str(res.get(_b.id)))
check("one bad channel does not stop the others being checked", len(res) == 2)
res = reach([_a], ages={_a.id: 5 * 86400})
check("a long-silent but readable channel is marked as dozing, not broken",
      res[_a.id][0] == "💤", str(res.get(_a.id)))
res = reach([_a], ages={_a.id: None})
check("a readable channel with no messages is called out too",
      res[_a.id][0] == "⚠️", str(res.get(_a.id)))
_cm = open(os.path.join(ROOT, "channels", "channel_manager.py"),
           encoding="utf-8").read()
check("the reachability check runs before run_until_disconnected",
      _cm.index("_check_reachable(channels)") < _cm.index("run_until_disconnected"))

# ── a missed session is caught up, once ─────────────────────────────────────
# The scheduler is one long asyncio.sleep to the next REPORT_AT_UTC, which is
# useless the moment the process is not running. On 2026-08-25 it was stopped
# before 21:30 UTC and restarted after the next Asian open; that session's
# report was skipped and nothing would ever have sent it.
import tempfile as _tf                                            # noqa: E402
from db.database import Database as _DB                            # noqa: E402


class _Notif:
    def __init__(self): self.sent = []
    async def send(self, m): self.sent.append(m)


with _tf.TemporaryDirectory() as _td:
    _dbp = os.path.join(_td, "t.db")
    _db = _DB(_dbp)
    _NOW = datetime(2026, 8, 26, 3, 7, tzinfo=U2)

    _n = _Notif()
    _r = DailyReport(_db, types.SimpleNamespace(channels=[_ch]), _n)
    check("an empty install does not open with a report for a day it missed",
          asyncio.run(_r.catch_up(_NOW)) is False and not _n.sent,
          str(_n.sent)[:120])

    # Now with activity in that session. A fresh marker store: the empty-install
    # case above deliberately marks the session done, and reusing it here would
    # make this test pass by never running.
    _db2 = _DB(os.path.join(_td, "v.db"))
    _n2 = _Notif()
    _r2 = DailyReport(RptDB(None, only=_ch.id),
                      types.SimpleNamespace(channels=[_ch]), _n2)
    _r2.db.get_meta = _db2.get_meta           # real durable marker
    _r2.db.set_meta = _db2.set_meta
    check("a session that was never reported is sent on the next startup",
          asyncio.run(_r2.catch_up(_NOW)) is True and _n2.sent, str(len(_n2.sent)))
    check("...labelled as a catch-up, not as tonight's report",
          any("Catch-up" in m for m in _n2.sent), str(_n2.sent)[:160])
    check("...for the session that actually ended",
          any("2026-08-25 00:00" in m for m in _n2.sent), str(_n2.sent)[:200])
    _n2.sent.clear()
    check("and it is not sent twice",
          asyncio.run(_r2.catch_up(_NOW)) is False and not _n2.sent,
          str(_n2.sent)[:120])
    check("the marker is durable, not in-memory",
          _db2.get_meta("daily_report_last_session_end") == "2026-08-25T21:00:00",
          str(_db2.get_meta("daily_report_last_session_end")))

    # A send that fails must be retried, not written off.
    class _Broken:
        async def send(self, m): raise RuntimeError("telegram down")
    _db3 = _DB(os.path.join(_td, "u.db"))
    _r3 = DailyReport(RptDB(None, only=_ch.id),
                      types.SimpleNamespace(channels=[_ch]), _Broken())
    _r3.db.get_meta = _db3.get_meta; _r3.db.set_meta = _db3.set_meta
    asyncio.run(_r3.catch_up(_NOW))
    check("a failed send leaves the session unmarked so it retries",
          _db3.get_meta("daily_report_last_session_end") is None,
          str(_db3.get_meta("daily_report_last_session_end")))

_dr_src = open(os.path.join(ROOT, "core", "daily_report.py"),
               encoding="utf-8").read()
check("the scheduler runs the catch-up before its first sleep",
      _dr_src.index("await self.catch_up()") < _dr_src.index("_seconds_until_report()",
                                                             _dr_src.index("async def start")))
check("a manual re-read tool exists for older sessions",
      os.path.exists(os.path.join(ROOT, "tools", "daily_report.py")))

main_src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
check("main starts the report task", "DailyReport(db, cfg, notifier)" in main_src)
check("main cancels it on shutdown", "report_task.cancel()" in main_src)
check("terminal output is captured to a file", "_tee_stdio" in main_src
      and "terminal.log" in main_src)
check("...and stdout still reaches the terminal", "self._stream.write" in main_src)
check("skipped signals are recorded for the scorecard",
      "log_skipped_signal" in open(os.path.join(ROOT, "db", "database.py"),
                                   encoding="utf-8").read())

# ═══════════════════════════════════════════════════════════════════════════
print("\ncross-instrument signals are refused")
# ═══════════════════════════════════════════════════════════════════════════
_d = dict(CFG["defaults"]["parser"])
_d["symbol_aliases"] = dict(_d.get("symbol_aliases", {}), EURUSD="EURUSD")
_pf = SignalParser({"id": "x", "symbol": "XAUUSDm", "parser": _d})
_r = _pf.parse("EURUSD BUY 1.0850\nSL 1.0820\nTP 1.0890\nTP 1.0920")
check("a EURUSD post on a gold channel is refused", not _r.executable,
      str(_r.rejected_reasons))
check("...naming both instruments in the reason",
      any("EURUSD" in x and "XAUUSDm" in x for x in _r.rejected_reasons),
      str(_r.rejected_reasons))
check("gold itself still trades",
      _pf.parse("XAUUSD Sell 4420/4425\nTP1: 4416\nStop Loss 4434").executable)
check("the GOLD alias still resolves to the channel symbol",
      _pf.parse("GOLD Sell 4420/4425\nTP1: 4416\nStop Loss 4434"
                ).signal.symbol == "XAUUSDm")

check("a wrong clock alerts Telegram, not just the log",
      "System clock is wrong" in lis_src and "notifier.send" in lis_src)
check("...rate-limited so it cannot spam", "_last_clock_warn" in lis_src)
_pf = open(os.path.join(ROOT, "tools", "preflight.py"), encoding="utf-8").read()
check("preflight measures the clock against Telegram, not w32tm",
      "time_offset" in _pf and "newest message" in _pf.lower())
check("preflight verifies CONTRACT_SIZE against the broker",
      "CONTRACT_SIZE mismatch" in _pf)
check("preflight checks the EA supports partial close",
      "PositionClosePartial" in _pf)
_pf = open(os.path.join(ROOT, "tools", "preflight.py"), encoding="utf-8").read()
check("preflight names channels it cannot read instead of skipping them",
      "cannot be read" in _pf and "unreadable.append" in _pf)
check("preflight exits non-zero when something blocks",
      "return 1 if _problems else 0" in _pf)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
