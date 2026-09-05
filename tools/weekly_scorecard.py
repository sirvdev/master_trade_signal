#!/usr/bin/env python3
"""tools/weekly_scorecard.py - per-channel results for one date range.

R-multiple is the only fair cross-channel measure here: every channel runs its
own notional book and its own stop distances, so raw P&L rewards whoever
happened to trade biggest. R = realised P&L / dollars risked at entry, where
the risk uses the signal's ORIGINAL stop, not a stop that was later moved to
breakeven - otherwise moving a stop would flatter the R of the channel that
did it.
"""
import argparse, json, sqlite3, sys
from collections import defaultdict

CONTRACT = 100.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db"); ap.add_argument("channels")
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    a = ap.parse_args()

    c = sqlite3.connect(a.db); c.row_factory = sqlite3.Row
    cfg = json.load(open(a.channels, encoding="utf-8"))
    NAME = {x["id"]: x["name"] for x in cfg["channels"]}

    rows = c.execute(
        "SELECT p.channel_id cid, p.signal_id sid, p.status st, p.close_reason cr, "
        "       p.pnl pnl, p.lot_size lot, p.entry_price ep, p.opened_at oa, "
        "       s.stop_loss orig_sl, s.notes notes, s.is_bare bare, s.created_at ca "
        "FROM positions p JOIN signals s ON s.signal_id = p.signal_id "
        "WHERE substr(s.created_at,1,10) BETWEEN ? AND ?", (a.start, a.end)).fetchall()

    ch = defaultdict(lambda: dict(sigs=set(), legs=0, filled=0, pending=0, tp=0, sl=0,
                                  other=0, pnl=0.0, risk=0.0, bare=set(), synth=set()))
    for r in rows:
        d = ch[r["cid"]]
        d["sigs"].add(r["sid"])
        d["legs"] += 1
        if r["bare"]:
            d["bare"].add(r["sid"])
        if r["notes"] and "synthesised" in str(r["notes"]):
            d["synth"].add(r["sid"])
        if r["oa"] is None:
            d["pending"] += 1
            continue
        d["filled"] += 1
        if r["st"] != "closed":
            continue
        cr = (r["cr"] or "").lower()
        if cr == "tp":
            d["tp"] += 1
        elif cr == "sl":
            d["sl"] += 1
        else:
            d["other"] += 1
        d["pnl"] += float(r["pnl"] or 0.0)
        ep, osl, lot = r["ep"], r["orig_sl"], r["lot"]
        if ep and osl and lot:
            d["risk"] += float(lot) * abs(float(ep) - float(osl)) * CONTRACT

    print(f"Week {a.start} to {a.end}\n")
    hdr = (f"{'channel':<32}{'sig':>4}{'legs':>5}{'fill%':>6}{'TP':>4}{'SL':>4}"
           f"{'win%':>6}{'P&L':>9}{'risk':>8}{'R':>7}{'bare':>5}{'synth':>6}")
    print(hdr); print("-" * len(hdr))
    tot = dict(sigs=0, legs=0, filled=0, pending=0, tp=0, sl=0, pnl=0.0, risk=0.0,
               bare=0, synth=0)
    out = []
    for cid, d in ch.items():
        res = d["tp"] + d["sl"]
        out.append((
            d["pnl"] / d["risk"] if d["risk"] else 0.0, NAME.get(cid, cid), cid,
            len(d["sigs"]), d["legs"],
            100 * d["filled"] / d["legs"] if d["legs"] else 0,
            d["tp"], d["sl"], 100 * d["tp"] / res if res else 0,
            d["pnl"], d["risk"], len(d["bare"]), len(d["synth"])))
        for k in ("legs", "filled", "pending", "tp", "sl", "pnl", "risk"):
            tot[k] += d[k]
        tot["sigs"] += len(d["sigs"]); tot["bare"] += len(d["bare"])
        tot["synth"] += len(d["synth"])
    for R, name, cid, ns, legs, fill, tp, sl, win, pnl, risk, bare, syn in \
            sorted(out, key=lambda x: -x[0]):
        print(f"{name:<32}{ns:>4}{legs:>5}{fill:>5.0f}%{tp:>4}{sl:>4}{win:>5.0f}%"
              f"{pnl:>9.2f}{risk:>8.0f}{R:>7.2f}{bare:>5}{syn:>6}")
    print("-" * len(hdr))
    R = tot["pnl"] / tot["risk"] if tot["risk"] else 0
    res = tot["tp"] + tot["sl"]
    print(f"{'TOTAL':<32}{tot['sigs']:>4}{tot['legs']:>5}"
          f"{100*tot['filled']/tot['legs'] if tot['legs'] else 0:>5.0f}%"
          f"{tot['tp']:>4}{tot['sl']:>4}{100*tot['tp']/res if res else 0:>5.0f}%"
          f"{tot['pnl']:>9.2f}{tot['risk']:>8.0f}{R:>7.2f}{tot['bare']:>5}"
          f"{tot['synth']:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
