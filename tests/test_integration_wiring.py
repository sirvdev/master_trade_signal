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
from datetime import datetime, timezone

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
from core.signal_parser import (Intent, OrderType, SignalParser)  # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        FAILS.append(label)


CFG = json.load(open(os.path.join(ROOT, "channels.json"), encoding="utf-8"))


def parser_for(cid, symbol=None):
    ch = [c for c in CFG["channels"] if str(c["id"]) == cid][0]
    m = dict(CFG["defaults"]["parser"]); m.update(ch.get("parser", {}))
    return SignalParser({"id": cid, "symbol": symbol or ch["symbol"],
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
check("refused signals adapt to None",
      adapt(p.parse("XAUUSD Sell 4420/4425\nTP1: 4416\nStop Loss 4334"),
            "XAUUSD") is None)
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
m5 = [c for c in chans if c.symbol == "XAUUSDc"][0]
check("a channel with no profile still produces a usable hint",
      bool(build_channel_hint(m5.name, m5.parser).strip()))

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

# and the window expires
ex, br, db, ch = fresh()
asyncio.run(ex.execute(mk_signal(), ch, message_id=44294))
n1 = br.orders
ex._recent_trades = {k: v - (ex.duplicate_window_sec + 5)
                     for k, v in ex._recent_trades.items()}
asyncio.run(ex.execute(mk_signal(), ch, message_id=44299))
check("the same setup is allowed again after the window", br.orders > n1,
      f"{n1} then {br.orders}")

check("DUPLICATE_WINDOW_SEC is configurable", ex.duplicate_window_sec == 600)
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
check("empty breakeven config keeps the old operator-message behaviour",
      all(not c.breakeven.get("on_tp_index") and not c.breakeven.get("on_pips")
          for c in chans2))

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
