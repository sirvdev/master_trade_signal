#!/usr/bin/env python3
"""
tools/breakeven_geometry.py
===========================
Where a channel's breakeven should arm, measured from the ladders it posts.

The 2026-08-27 study answered this for the original roster using OUR OWN fills:
leg2.MAE minus leg1.MAE per lot isolates how far price retraced after TP1. That
method needs live trades and the eleven channels added on 2026-08-30 have none.

What their exports do contain is every ladder they ever posted, and a ladder is
a statement about geometry. Two numbers decide whether a TP1 breakeven helps or
churns:

  R1 = distance(entry -> TP1) / distance(entry -> SL)
       How far price must travel before TP1 banks anything. A TP1 at 0.3R sits
       inside normal noise: price reaches it and comes back through entry
       routinely, so a stop moved to entry there is a coin-flip on the rest of
       the ladder.

  banked1 = R1 / n_legs
       What TP1 actually secures, because the position is split n ways. Banking
       0.5R on one of five legs recovers 0.1R. If a breakeven stop then takes
       the other four out flat, the trade paid 0.1R for its entire upside.

Rule applied below, and it is deliberately conservative:
  - arm at TP1 when R1 >= 0.8 and banked1 >= 0.20
  - else arm at TP2 when R2 >= 0.8
  - else do not arm on a target at all; leave it to the operator's own message

Run: python tools/breakeven_geometry.py <corpus_dir>
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.signal_parser import Intent, SignalParser   # noqa: E402

CORPUS = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/work/new11"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = json.load(open(os.path.join(ROOT, "channels.json"), encoding="utf-8"))
DFLT = RAW["defaults"]["parser"]
BY = {c["id"]: c for c in RAW["channels"]}


def med(a):
    if not a:
        return None
    a = sorted(a)
    return a[len(a) // 2]


def main() -> int:
    print(f"{'channel':<22} {'n':>4} {'legs':>5} {'R1':>6} {'R2':>6} {'R3':>6} "
          f"{'bank1':>6} {'bank2':>6}  verdict")
    print("-" * 92)
    for fp in sorted(glob.glob(os.path.join(CORPUS, "*.json"))):
        cid = os.path.basename(fp)[:-5]
        if cid not in BY:
            continue
        ch = dict(BY[cid])
        m = dict(DFLT)
        m.update(ch.get("parser", {}))
        m["max_signal_age_sec"] = 10 ** 9
        ch["parser"] = m
        P = SignalParser(ch)

        r1, r2, r3, legs = [], [], [], []
        for msg in json.load(open(fp, encoding="utf-8")):
            t = (msg.get("text") or "").strip()
            if not t:
                continue
            res = P.parse(t, msg_id=msg.get("id"))
            if res.intent is not Intent.NEW_SIGNAL or res.rejected_reasons:
                continue
            s = res.signal
            if not s.sl or not s.tps:
                continue
            # Entry the executor would actually take: the worst edge of the zone.
            if s.direction == "BUY":
                e = s.entry_high if s.entry_high is not None else s.entry
            else:
                e = s.entry_low if s.entry_low is not None else s.entry
            if e is None:
                continue
            risk = abs(float(e) - float(s.sl))
            if risk <= 0:
                continue
            legs.append(len(s.tps))
            for i, bucket in ((0, r1), (1, r2), (2, r3)):
                if len(s.tps) > i:
                    bucket.append(abs(float(s.tps[i]) - float(e)) / risk)

        if not r1:
            print(f"{BY[cid]['name']:<22} {'-':>4}  no complete ladders")
            continue

        n_legs = med(legs) or 1
        R1, R2, R3 = med(r1), med(r2), med(r3)
        b1 = R1 / n_legs
        b2 = ((R1 + R2) / n_legs) if R2 else None

        if R1 >= 0.8 and b1 >= 0.20:
            v = "arm at TP1"
        elif R2 and R2 >= 0.8:
            v = "arm at TP2"
        else:
            v = "operator message only"
        print(f"{BY[cid]['name']:<22} {len(r1):>4} {n_legs:>5} {R1:>6.2f} "
              f"{(R2 if R2 else 0):>6.2f} {(R3 if R3 else 0):>6.2f} "
              f"{b1:>6.2f} {(b2 if b2 else 0):>6.2f}  {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
