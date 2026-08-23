#!/usr/bin/env python3
"""
tools/test_signal_parser.py
===========================
Regression tests. Every case here is a real message from the channel exports,
and most of them encode a bug that was live at some point during development.
Run with: python tools/test_signal_parser.py
"""
import json
import os
import sys
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Windows consoles default to cp1252. A failure detail containing an emoji from
# a parsed message would otherwise raise UnicodeEncodeError and hide the real
# failure. Never let the reporter crash louder than the thing it is reporting.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core.signal_parser import (  # noqa: E402
    Intent, SignalParser, DuplicateGuard, normalize)
CFG = json.load(open(os.path.join(os.path.dirname(__file__), "..", "channels.json"),
                    encoding="utf-8"))
DFLT = CFG["defaults"]["parser"]
def parser_for(cid):
    ch = dict([c for c in CFG["channels"] if c["id"] == cid][0])
    m = dict(DFLT); m.update(ch.get("parser", {})); ch["parser"] = m
    return SignalParser(ch)
FAILS = []
def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        FAILS.append(label)
print("normalisation")
check("mathematical-bold folds to ASCII",
      "GOLD" in normalize("\U0001d406\U0001d40e\U0001d40b\U0001d403 Sell"))
check("homoglyph HlT repaired", "HIT" in normalize("TP5 HlT 200+ PlPS"))
check("variation selector stripped between label and price",
      normalize("SL ️4220").replace(" ", "") == "SL4220")
print("\nsignal extraction")
p = parser_for("-1002070727316")
r = p.parse("**XAUUSD Sell ****4420/4425****\n\nTP1: 4416\nTP 2:4412\nTP 3.4408\n"
            "TP 4:4404\nTP 5:4400\n\nStop Loss 4434**")
check("zone + mixed TP separators", r.intent == Intent.NEW_SIGNAL
      and r.signal.tps == [4416.0, 4412.0, 4408.0, 4404.0, 4400.0],
      str(r.signal.tps if r.signal else r.intent))
check("period-separated TP index does not eat the price",
      r.signal and 4408.0 in r.signal.tps)
p2 = parser_for("-1002215701365")
r2 = p2.parse("XAUUSD BUY NOW 4357\n\nSL 4347\n\nTP 4360\nTP 4363\nTP 4366\nTP 4372")
check("unindexed 'TP 4360' keeps all four digits",
      r2.signal and r2.signal.tps == [4360.0, 4363.0, 4366.0, 4372.0],
      str(r2.signal.tps if r2.signal else None))
p3 = parser_for("-1002193412690")
r3 = p3.parse("Gold(XAUUSD)\nSELL\U0001f6d14405-4415\n\nTP1 \U0001f3af4400\nTP2 \U0001f3af4398\n"
              "TP3 \U0001f3af4396\nTP4 \U0001f3af4394\nTP5 \U0001f3afPOEN\n\nSL⛔️4420")
check("emoji-separated fields parse", r3.signal is not None)
check("'POEN' typo read as an open runner", r3.signal and r3.signal.has_open_runner)
check("zone detected", r3.signal and r3.signal.is_zone)
p4 = parser_for("-1001799443074")
r4 = p4.parse("GOLD BUY b/n 4385-4390\nSL 4365\nTP 4510\nTP every 100 pips")
check("'b/n' zone separator", r4.signal and r4.signal.is_zone)
check("'TP every 100 pips' expands to a ladder",
      r4.signal and len(r4.signal.tps) > 2, str(r4.signal.tps if r4.signal else None))
print("\nvalidation gates (these must REFUSE)")
r5 = p.parse("XAUUSD Sell 4420/4425\nTP1: 4416\nTP 5:4400\nStop Loss 4334")
check("SELL with SL below entry is refused", not r5.executable, str(r5.rejected_reasons))
p6 = parser_for("-1002284339674")
r6 = p6.parse("XAUUSD - BUY Signal SCALP TRADE\nEntry Zone: 4078 - 4083\n"
              "Stop Loss (SL): 4085\nTP1: 4073\nTP2: 4065")
check("BUY label contradicted by SL/TP geometry is refused",
      not r6.executable, str(r6.rejected_reasons))
r7 = p.parse("GOLD: SELL : 4665/4670\nTP. 4660\nTP. 4655\nSL. 46 75")
check("space-corrupted SL number is refused, not guessed",
      not r7.executable, str(r7.rejected_reasons))
r8 = p.parse("XAUUSD BUY 4692/4688\nTP1: 4696\nTP 5:4712\nSL. 4680")
check("SL with underscore/odd separator still parses",
      r8.signal and r8.signal.sl == 4680.0, str(r8.signal.sl if r8.signal else None))
print("\nmarket orders")
r9 = p4.parse("GOLD SELL a now\nSL 4050\nTP 3900\nTP every 100 pips")
check("market order with no entry is flagged, not silently passed",
      r9.signal and r9.signal.needs_market_entry)
p4.finalize_market(r9, 4060.0)
check("deferred geometry check fires on a wrong-side fill", not r9.executable,
      str(r9.rejected_reasons))
print("\nmanagement intents")
pg = parser_for("-1001643924999")
cases = [
    ("Cancel all buys orders . H1 bearish",           Intent.CANCEL_PENDING, True),
    ("Cancel this . Gold don't make pullbacks",       Intent.CANCEL_PENDING, True),
    ("All closed",                                    Intent.CLOSE_ALL,      True),
    ("Move SL to BE TAKE profits",                    None,                  True),
    ("Last adjustment of SL to 4054",                 Intent.MOVE_SL_PRICE,  True),
    ("90 Pips take partials now",                     Intent.CLOSE_PARTIAL,  True),
    ("I took it but SL little bit lower, now all closed", None,              False),
    ("How much profits you closeddd ?",               None,                  False),
    ("once 100 new members joined I will delete this message again!", None,  False),
    ("I will delete it in two minutes.",              None,                  False),
]
for text, want, should_exec in cases:
    r = pg.parse(text)
    ok = (r.executable == should_exec) and (want is None or r.intent == want)
    check(f"{'EXEC ' if should_exec else 'REFUSE'} {text[:44]!r}", ok,
          f"got {r.intent.value} exec={r.executable} conf={r.confidence:.2f}")
pb = parser_for("-1001765226347")
rb = pb.parse("**Round 3 of the day  TP2//130PIPS✅**\n\nLet's CLOSE our trade now "
              "and set breakeven if you wish to hold now!!")
check("trailing 'if you wish' carve-out does not suppress the close",
      rb.intent == Intent.CLOSE_ALL and rb.executable,
      f"{rb.intent.value} conf={rb.confidence:.2f} {rb.notes}")
rc = pg.parse("You can close half and leave the rest now")
check("leading 'you can' IS suppressed as optional", not rc.executable,
      f"conf={rc.confidence:.2f}")
print("\nduplicate guard")
g = DuplicateGuard(3600)
r10 = p2.parse("XAUUSD BUY NOW 4357\n\nSL 4347\n\nTP 4360")
t0 = datetime(2026, 8, 12, 10, 0, 0)
check("first post accepted", not g.is_duplicate(r10.signal, t0))
check("repost 20 min later suppressed",
      g.is_duplicate(r10.signal, datetime(2026, 8, 12, 10, 20, 0)))
check("same setup 3 h later allowed",
      not g.is_duplicate(r10.signal, datetime(2026, 8, 12, 13, 30, 0)))
# ═════════════════════════════════════════════════════════════════════════════
# Defects found by audit after the suite above already passed. Each of these
# failed on the version that reported "ALL PASS".
# ═════════════════════════════════════════════════════════════════════════════
from datetime import timedelta                                       # noqa: E402
from core.signal_parser import (OrderType, SignalAssembler,          # noqa: E402
                                RE_STOP_ORDER_PHRASE)

print("\nstop-ORDER vs stop-LOSS  (was: entry captured as SL -> 4x oversize)")
ps = parser_for("-1001643924999")
rs = ps.parse("Gold buy stop 4420-4425\nSL 4405\nTP 4450")
check("'buy stop <zone>' does not steal the SL label",
      rs.signal and rs.signal.sl == 4405.0, f"sl={rs.signal.sl if rs.signal else None}")
check("'buy stop <zone>' keeps the full entry zone",
      rs.signal and (rs.signal.entry_low, rs.signal.entry_high) == (4420.0, 4425.0),
      f"zone={rs.signal.entry_low}-{rs.signal.entry_high}" if rs.signal else "none")
rs2 = ps.parse("Gold Sell Stop 4400\nSL 4415\nTP 4380")
check("'Sell Stop <price>' reads price as entry, not stop",
      rs2.signal and rs2.signal.entry == 4400.0 and rs2.signal.sl == 4415.0,
      f"entry={rs2.signal.entry} sl={rs2.signal.sl}" if rs2.signal else "none")
check("stop-order phrase still sets the order type",
      rs2.signal and rs2.signal.order_type == OrderType.SELL_STOP)
check("plain 'Stop: 4393' is still a stop loss",
      ps.parse("Gold Short Zone: 4400\nStop: 4393\nTarget 1: 4380").signal.sl == 4393.0)

print("\ngeometry consensus runs BEFORE the wrong-side TP purge")
pwg = parser_for("-1002193412690")
rm = pwg.parse("Gold(XAUUSD)\nBUY 4400\nTP1 4390\nTP2 4385\nTP3 OPEN\nSL 4395")
check("mislabelled BUY with an open runner is refused, not executed naked",
      not rm.executable, str(rm.rejected_reasons))
check("...and it is not left with zero TPs and a live runner",
      not (rm.executable and not rm.signal.tps and rm.signal.has_open_runner))

print("\n'TP1 at 4407' (GTMO's documented split-post format)")
pgt = parser_for("-1002495224665")
rt = pgt.parse("Gold sell now 4415\nSL place at 4425\nTP1 at 4407\nTP2 at 4405")
check("'TP1 at <price>' parses as a target",
      rt.signal and rt.signal.tps == [4407.0, 4405.0],
      str(rt.signal.tps if rt.signal else None))
check("...so the TP price is not mistaken for the entry",
      rt.signal and rt.signal.entry == 4415.0,
      f"entry={rt.signal.entry}" if rt.signal else "none")
check("'TP2 hit at 4407' is NOT read as a new target",
      not pgt.parse("TP2 hit at 4407 nice one").executable)

print("\nSignalAssembler (GTMO, 900s window)")
base = datetime(2026, 8, 12, 12, 0, 0)
asm = SignalAssembler(pgt)
emitted = []
for i, txt in enumerate(["Gold sell now", "SL place at 4437",
                         "TP1 at 4407\nTP2 at 4405"]):
    o = asm.feed(txt, 100 + i, base + timedelta(minutes=i),
                 now=base + timedelta(minutes=i))
    if o is not None and o.intent == Intent.NEW_SIGNAL:
        emitted.append(o)
check("three-post trade emits exactly one signal", len(emitted) == 1,
      f"emitted {len(emitted)}")
check("...and only once it has both a stop and targets",
      len(emitted) == 1 and emitted[0].signal.sl == 4437.0
      and emitted[0].signal.tps == [4407.0, 4405.0],
      str(emitted[0].signal.as_dict()) if emitted else "nothing emitted")
check("...and the buffer is closed afterwards", asm._pending is None)

asm2 = SignalAssembler(pgt)
o = asm2.feed("Gold looks bullish above 4400 today, watch the H1", 1, base)
check("market commentary is not swallowed into a fragment buffer",
      o is not None and asm2._pending is None,
      f"returned={o!r} pending={asm2._pending is not None}")

asm3 = SignalAssembler(pgt)
asm3.feed("Gold sell now", 1, base, now=base)
o = asm3.feed("SL place at 4437", 2, base + timedelta(minutes=30),
              now=base + timedelta(minutes=30))
check("a fragment older than the window is dropped, not completed late",
      o is None or o.intent != Intent.NEW_SIGNAL,
      f"got {o.intent.value if o else None}")

print("\nMARKET orders are always re-checked against the fill")
rmk = pgt.parse("Gold sell now 4415\nSL 4425\nTP 4400")
check("MARKET order with a posted entry still demands finalize_market()",
      rmk.signal and rmk.signal.order_type == OrderType.MARKET
      and rmk.signal.needs_market_entry,
      f"needs={rmk.signal.needs_market_entry}" if rmk.signal else "none")
p4b = parser_for("-1001799443074")
rf = p4b.parse("GOLD SELL a now\nSL 4050\nTP 3900")
p4b.finalize_market(rf, 4060.0)
n1 = len(rf.rejected_reasons)
p4b.finalize_market(rf, 4060.0)
check("finalize_market is idempotent (no duplicated reasons)",
      len(rf.rejected_reasons) == n1, str(rf.rejected_reasons))

print("\nfreshness gate cannot be defeated by a bad clock")
rfut = ps.parse("Gold sell 4415\nSL 4425\nTP 4400",
                msg_dt=datetime(2026, 8, 12, 13, 0, 0),
                now=datetime(2026, 8, 12, 10, 0, 0))
check("future-dated message (naive local time vs UTC) is refused",
      not rfut.executable, str(rfut.rejected_reasons))
rok = ps.parse("Gold sell 4415\nSL 4425\nTP 4400",
               msg_dt=datetime(2026, 8, 12, 10, 0, 0),
               now=datetime(2026, 8, 12, 10, 0, 30))
check("a 30s-old message still passes", rok.executable, str(rok.rejected_reasons))

print("\nDuplicateGuard")
g2 = DuplicateGuard(3600)
a = pgt.parse("GOLD SELL now\nSL 4425\nTP 4380")
b = pgt.parse("GOLD SELL now\nSL 4425\nTP 4300")
g2.is_duplicate(a.signal, base)
check("two market orders sharing an SL but not a target are not merged",
      not g2.is_duplicate(b.signal, base + timedelta(minutes=1)))
check("a None signal does not raise", g2.is_duplicate(None, base) is False)

print("\npercent partials (was: 'Close 50% position' classified as CHATTER)")
pwl = parser_for("-1002193412690")
for txt, want_frac in [("Close 50% position", 0.5),
                       ("Close 50%", 0.5),
                       ("close 30% now", 0.3),
                       ("Close half and hold the rest", 0.5)]:
    rr = pwl.parse(txt)
    check(f"{txt!r} -> CLOSE_PARTIAL",
          rr.intent == Intent.CLOSE_PARTIAL and rr.executable,
          f"got {rr.intent.value} exec={rr.executable}")
    check(f"   ...fraction {want_frac}",
          rr.action is not None and abs((rr.action.fraction or 0) - want_frac) < 1e-9,
          str(rr.action.fraction if rr.action else None))

print("\nAUTO order type when the market sits inside the entry zone")
pz = parser_for("-1002070727316")
rz = pz.parse("XAUUSD Sell 4420/4425\nTP1: 4416\nTP 2:4412\nStop Loss 4434",
              market_price=4421.0)
check("market inside the zone resolves to MARKET, not a stop order",
      rz.signal and rz.signal.order_type == OrderType.MARKET,
      str(rz.signal.order_type if rz.signal else None))
check("...and is flagged for a fill-price re-check",
      rz.signal and rz.signal.needs_market_entry)
rz2 = pz.parse("XAUUSD Sell 4420/4425\nTP1: 4416\nStop Loss 4434",
               market_price=4423.5)
check("anywhere inside the zone is a market fill",
      rz2.signal and rz2.signal.order_type == OrderType.MARKET,
      str(rz2.signal.order_type if rz2.signal else None))
# Market already ABOVE a sell zone. zone_fill="worst" anchors the entry on the
# far edge (4420), which below the market reads as a SELL_STOP. That is a valid
# resting order, so the deviation gate correctly no longer fires. It is stopped
# one layer later instead: the MT5 bridge emits limit orders only, so the
# adapter refuses it. Assert the OUTCOME (nothing is placed), not the mechanism.
from core.signal_adapter import adapt as _adapt                      # noqa: E402
rz4 = pz.parse("XAUUSD Sell 4420/4425\nTP1: 4416\nStop Loss 4434",
               market_price=4430.0)
check("market past the zone does not become a market order",
      rz4.signal and rz4.signal.order_type != OrderType.MARKET,
      str(rz4.signal.order_type if rz4.signal else None))
check("...and no order reaches the executor",
      _adapt(rz4, "XAUUSDm") is None)
rz3 = pz.parse("XAUUSD Sell 4420/4425\nTP1: 4416\nStop Loss 4434",
               market_price=4418.0)
check("market below a sell zone is a SELL_LIMIT",
      rz3.signal and rz3.signal.order_type == OrderType.SELL_LIMIT,
      str(rz3.signal.order_type if rz3.signal else None))

print("\nbroker symbol suffix survives the alias map")
pcx = SignalParser({"id": "x", "symbol": "XAUUSDc", "parser": dict(DFLT)})
rcx = pcx.parse("GOLD Sell 4420/4425\nTP1: 4416\nStop Loss 4434")
check("channel symbol XAUUSDc is not rewritten to XAUUSD",
      rcx.signal and rcx.signal.symbol == "XAUUSDc",
      str(rcx.signal.symbol if rcx.signal else None))

print("\npending orders are not slippage (live refusal 2026-08-18 08:11)")
plim = parser_for("-1001643924999")
LIMIT = "GOLD BUY limit ! Medium Risk\nEntry 4350\nSL 4334\nTP 4360\nTP 4370"
rl = plim.parse(LIMIT, market_price=4367.2)
check("the exact refused signal now passes", rl.executable, str(rl.rejected_reasons))
check("...as a BUY_LIMIT",
      rl.signal and rl.signal.order_type == OrderType.BUY_LIMIT,
      str(rl.signal.order_type if rl.signal else None))
check("...at the posted price, not the market",
      rl.signal and rl.signal.entry == 4350.0)
check("...and says why the gate did not apply",
      any("slippage gate does not apply" in n for n in rl.notes), str(rl.notes))
rl2 = plim.parse(LIMIT, market_price=4900.0)
check("distance alone never refuses a resting order", rl2.executable,
      str(rl2.rejected_reasons))

check("a MARKET order past its quote is still refused",
      not plim.parse("GOLD BUY now 4350\nSL 4334\nTP 4360",
                     market_price=4367.2).executable)
check("a BUY_LIMIT above the market is refused (fills instantly)",
      not plim.parse("GOLD BUY limit ! Entry 4380\nSL 4360\nTP 4400",
                     market_price=4367.2).executable)
check("a SELL_LIMIT below the market is refused (fills instantly)",
      not plim.parse("GOLD SELL limit ! Entry 4350\nSL 4368\nTP 4330",
                     market_price=4367.2).executable)
check("a normal SELL_LIMIT above the market passes",
      plim.parse("GOLD SELL limit ! Entry 4380\nSL 4396\nTP 4360",
                 market_price=4367.2).executable)
check("market inside a zone is still treated as a market fill",
      plim.parse("Gold sell 4360-4375\nSL 4390\nTP 4340",
                 market_price=4367.2).signal.order_type == OrderType.MARKET)

pbound = SignalParser({"id": "x", "symbol": "XAUUSDm",
                       "parser": dict(DFLT, max_limit_distance=50.0,
                                      min_confidence=0.65)})
check("max_limit_distance bounds a resting order when set",
      not pbound.parse(LIMIT, market_price=4900.0).executable)
check("...and leaves a nearby one alone",
      pbound.parse(LIMIT, market_price=4367.2).executable)

# ═══════════════════════════════════════════════════════════════════════════
print("\nmanagement phrasings missed in the 2026-08 live logs")
# ═══════════════════════════════════════════════════════════════════════════
def mgmt(chan_name, text):
    ch = [c for c in CFG["channels"] if c["name"].startswith(chan_name)][0]
    m = dict(CFG["defaults"]["parser"]); m.update(ch.get("parser") or {})
    return SignalParser({"id": ch["id"], "symbol": ch["symbol"],
                         "parser": m}).parse(text)

MISSED = [
    ("Gold Hunter", "Full closed",                    Intent.CLOSE_ALL),
    ("Lion",  "Close with small loss it's not good anymore", Intent.CLOSE_ALL),
    ("Gold Hunter", "take profit now",                Intent.CLOSE_PARTIAL),
    ("GTMO",  "At the top of our range and close to TP4 lets take some more profits ok",
     Intent.CLOSE_PARTIAL),
    ("Lion",  "take partial 100 pips",                Intent.CLOSE_PARTIAL),
    ("Lion",  "Take partials now 100 pips",           Intent.CLOSE_PARTIAL),
]
for ch, txt, want in MISSED:
    r = mgmt(ch, txt)
    check(f"{txt[:44]!r} -> {want.value}",
          r.intent == want and r.executable,
          f"got {r.intent.value} exec={r.executable} conf={r.confidence:.2f}")

print("\n...and the reports/questions they must NOT be confused with")
SUPPRESS = [
    ("GTMO", "Closed full profit, this one PERFECT and yeah thx for explanation"),
    ("GTMO", "I closed some profits this time"),
    ("GTMO", "Or you closed your profits?"),
    ("GTMO", "How much profits you closed in total now?"),
    ("Wall Street", "4 for 4. All blue. Day closed. 140 pips on the last one"),
]
for ch, txt in SUPPRESS:
    r = mgmt(ch, txt)
    check(f"suppressed: {txt[:44]!r}", not r.executable,
          f"got {r.intent.value} exec={r.executable}")

print("\nthe hypothetical guard is scoped to the phrase it matched")
r = mgmt("GTMO", "At the top of our range and close to TP4 lets take some more profits ok")
check("'close to TP4' no longer penalises an unrelated partial",
      r.confidence >= 0.8, f"conf={r.confidence:.2f}")
r2 = mgmt("GTMO", "we are running close to 250 pips profit now")
check("...but 'close to N' still suppresses a close", not r2.executable,
      f"{r2.intent.value} conf={r2.confidence:.2f}")

print("\nambiguous management defers to the AI instead of being binned")
for txt in ["IF HAPPY CLOSE", "Breakeven hit gold now", "MAKE LOVE CLOSE"]:
    r = mgmt("Gold Hunter", txt)
    check(f"{txt!r} -> UNKNOWN (AI fallback)", r.intent == Intent.UNKNOWN,
          r.intent.value)
check("long prose with a management word stays CHATTER",
      mgmt("GTMO", "GOOD MORNING TEAM GTMO. You don't have to be great to start, "
                   "but you have to start to be great, close your eyes and go"
          ).intent == Intent.CHATTER)

print("\none message, two instructions")
r = mgmt("GTMO", "Take some profits now and set breakeven ok")
check("partial is the primary intent", r.intent == Intent.CLOSE_PARTIAL)
check("breakeven is carried as a follow-up",
      r.follow_up is not None and r.follow_up.intent == Intent.MOVE_SL_BE,
      str(r.follow_up.intent.value if r.follow_up else None))
rb = mgmt("Wall Street", "Let's CLOSE our trade now and set breakeven "
                         "if you wish to hold now!!")
check("Wall Street Ben's recurring exit now does both",
      rb.intent == Intent.CLOSE_ALL and rb.follow_up
      and rb.follow_up.intent == Intent.MOVE_SL_BE,
      f"{rb.intent.value} + {rb.follow_up.intent.value if rb.follow_up else None}")
check("a plain breakeven has no follow-up",
      mgmt("Gold Hunter", "Move SL to BE TAKE profits").follow_up is None)

print("\nsender allowlist for management intents")
import copy                                                          # noqa: E402
raw = [c for c in CFG["channels"] if c["id"] == "-1001643924999"][0]


def parser_with(extra):
    ch = copy.deepcopy(raw)
    m = dict(DFLT); m.update(ch.get("parser", {})); m.update(extra)
    ch["parser"] = m
    return SignalParser(ch)


pa = parser_with({"operator_sender_ids": [777001]})
check("operator close is accepted",
      pa.parse("Close all trades now", sender_id=777001).executable)
check("member close is refused",
      not pa.parse("Close all trades now", sender_id=999999).executable,
      str(pa.parse("Close all trades now", sender_id=999999).rejected_reasons))
check("allowlist does not gate NEW_SIGNAL",
      pa.parse("Gold sell 4415\nSL 4425\nTP 4400", sender_id=999999).signal is not None)
pn = parser_with({})
check("empty allowlist keeps current behaviour (replay-compatible)",
      pn.parse("Close all trades now").executable)
pr = parser_with({"require_sender_verification": True})
check("require_sender_verification with no sender refuses",
      not pr.parse("Close all trades now").executable)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
