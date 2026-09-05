#!/usr/bin/env python3
"""
tools/breakeven_study.py
========================
Should this channel move to breakeven after TP1, TP2, or not at all?

Answers it from the trade history rather than from taste. Re-run it whenever
there is more data; the whole point is that the answer changes as the sample
grows.

    python tools/breakeven_study.py                    # since the clean start
    python tools/breakeven_study.py --since 2026-08-30
    python tools/breakeven_study.py --slack 5          # match your slack_pips

How it decides
--------------
Arming breakeven at TP<n> has exactly two effects:

  SAVES   a signal where TP<n> paid and every later leg then hit the stop.
          Those legs would have exited at entry instead of at a full loss.

  COSTS   a signal where a later leg went on to pay, but price dipped back
          below entry first. The breakeven stop takes you out before the
          target lands.

The second one is the hard measurement, because a leg's recorded MAE covers its
whole life, including the drawdown before TP<n> was ever reached — when no
breakeven was armed. Using raw MAE overstates the risk enormously (53 winning
legs looked at risk on this book; 5 actually were).

The trick is that all legs of a signal open at the same moment and the same
price, and each stops recording MAE when it closes. So:

    leg2.MAE - leg1.MAE   (per lot)

is the drawdown that happened strictly AFTER TP1 closed — which is precisely
the window a breakeven armed at TP1 is exposed to, and nothing else.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CONTRACT = float(os.getenv("CONTRACT_SIZE", "100.0"))


def to_pips(mae, lot, pip):
    return float(mae) / ((float(lot or 0.01)) * CONTRACT) / pip


def study(db_path: str, since: str, slack: float, pip: float, min_sig: int):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")

    ch = collections.defaultdict(lambda: dict(
        sig=0, tps=collections.Counter(), legs=collections.Counter(),
        arm={}, be_posts=0))

    for s in con.execute("SELECT * FROM signals WHERE created_at>=? AND is_bare=0",
                         (since,)):
        d = ch[s["channel_name"]]
        d["sig"] += 1
        d["tps"][len(json.loads(s["take_profits"] or "[]"))] += 1
        legs = {r["tp_index"]: r for r in con.execute(
            "SELECT tp_index,close_reason,pnl,mae,lot_size,status "
            "FROM positions WHERE signal_id=?", (s["signal_id"],))}
        d["legs"][len(legs)] += 1

        def reason(k):
            return str((legs.get(k) or {"close_reason": ""})["close_reason"]).lower()

        for n in (1, 2, 3):
            a = d["arm"].setdefault(n, dict(saves=0.0, costs=0.0, n_save=0,
                                            n_cost=0, dips=[]))
            if not legs.get(n) or reason(n) != "tp":
                continue
            later = [legs[k] for k in sorted(legs)
                     if k > n and legs[k]["status"] == "closed"]
            if not later:
                continue
            if all(str(r["close_reason"]).lower() == "sl" for r in later):
                a["saves"] += sum(float(r["pnl"] or 0) for r in later)
                a["n_save"] += 1
                continue
            nxt = legs.get(n + 1)
            if nxt and str(nxt["close_reason"]).lower() == "tp" \
                    and legs[n]["mae"] is not None and nxt["mae"] is not None:
                dip = (to_pips(nxt["mae"], nxt["lot_size"], pip)
                       - to_pips(legs[n]["mae"], legs[n]["lot_size"], pip))
                a["dips"].append(dip)
                if dip < -slack:
                    a["costs"] += sum(float(r["pnl"] or 0) for r in later
                                      if str(r["close_reason"]).lower() == "tp")
                    a["n_cost"] += 1
    return ch


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-23")
    ap.add_argument("--slack", type=float, default=5.0,
                    help="pips below entry the stop sits; match slack_pips")
    ap.add_argument("--pip", type=float, default=0.1)
    ap.add_argument("--min-signals", type=int, default=8,
                    help="below this, report the row but recommend nothing")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()

    db = a.db
    if db is None:
        from config import load_config
        db = load_config().db_path

    ch = study(db, a.since, a.slack, a.pip, a.min_signals)
    if not ch:
        print(f"no signals since {a.since}")
        return 0

    print(f"Breakeven study — signals since {a.since}, stop {a.slack:g} pips "
          f"below entry\n")
    print(f"{'channel':<26}{'sig':>4}{'TPs':>5}{'legs':>5}"
          f"{'BE@TP1 net':>12}{'BE@TP2 net':>12}   verdict")
    print("-" * 96)

    for name, d in sorted(ch.items(), key=lambda kv: -kv[1]["sig"]):
        tp = d["tps"].most_common(1)[0][0] if d["tps"] else 0
        lg = d["legs"].most_common(1)[0][0] if d["legs"] else 0
        a1, a2 = d["arm"].get(1, {}), d["arm"].get(2, {})
        n1 = a1.get("saves", 0) * -1 - a1.get("costs", 0)
        n2 = a2.get("saves", 0) * -1 - a2.get("costs", 0)
        dips1 = a1.get("dips", [])
        whip = sum(1 for x in dips1 if x < -a.slack)

        if d["sig"] < a.min_signals:
            v = f"only {d['sig']} signals — not enough to judge"
        elif lg < 2:
            v = "single leg, nothing to protect"
        elif n1 <= 0 and n2 <= 0:
            v = "leave OFF: later legs do not stop out after a target"
        elif whip and n2 > n1:
            v = (f"on_tp_index=2 (retraces past entry after TP1 in "
                 f"{whip}/{len(dips1)})")
        elif n1 >= n2:
            v = "on_tp_index=1"
        else:
            v = "on_tp_index=2"
        print(f"{name[:25]:<26}{d['sig']:>4}{tp:>5}{lg:>5}"
              f"{n1:>+12.2f}{n2:>+12.2f}   {v}")

    print("\npost-TP1 retrace — how far price came back below entry AFTER TP1:")
    for name, d in sorted(ch.items()):
        dips = d["arm"].get(1, {}).get("dips", [])
        if not dips:
            continue
        back = [x for x in dips if x < -a.slack]
        print(f"  {name[:25]:<26} n={len(dips):<3} took the stop: {len(back)}"
              f"   worst {min(dips):+.1f} pips"
              + ("   <-- whipsaws, do not arm at TP1" if back else ""))
    print("\nnet = money the setting would have saved, minus profit it would "
          "have cut short.\nPositive means it pays. One week is not enough; "
          "re-run this as the sample grows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
