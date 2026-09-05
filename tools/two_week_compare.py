#!/usr/bin/env python3
"""tools/two_week_compare.py - the same channels, two periods, R side by side.

Only channels present in BOTH periods are shown. R per CALL (per signal, not per
leg) is the unit, because a channel that fires a five-leg ladder and one that
fires two legs are otherwise not comparable, and because risk_pct changed
between the periods and R normalises it.
"""
import argparse, json, math, sqlite3, sys
from collections import defaultdict
C = 100.0


def calls(db, start, end):
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    per = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    legs = defaultdict(int); name = {}
    for r in c.execute("""SELECT p.channel_id cid, s.channel_name cn, p.signal_id sid,
          p.pnl, p.lot_size lot, p.entry_price ep, s.stop_loss osl
        FROM positions p JOIN signals s ON s.signal_id = p.signal_id
        WHERE p.status='closed' AND substr(p.opened_at,1,10) BETWEEN ? AND ?
          AND p.entry_price>0 AND p.lot_size>0 AND s.stop_loss>0""", (start, end)):
        risk = float(r["lot"]) * abs(float(r["ep"]) - float(r["osl"])) * C
        if risk <= 0:
            continue
        name[r["cid"]] = r["cn"]
        legs[r["cid"]] += 1
        per[r["cid"]][r["sid"]][0] += float(r["pnl"] or 0)
        per[r["cid"]][r["sid"]][1] += risk
    out = {}
    for cid, sigs in per.items():
        rs = [p / rk for p, rk in sigs.values() if rk > 0]
        if not rs:
            continue
        m = sum(rs) / len(rs)
        sd = (sum((x - m) ** 2 for x in rs) / (len(rs) - 1)) ** 0.5 if len(rs) > 1 else None
        out[cid] = dict(name=name[cid], n=len(rs), legs=legs[cid], mean=m,
                        se=sd / math.sqrt(len(rs)) if sd else None,
                        pnl=sum(p for p, _ in sigs.values()),
                        risk=sum(rk for _, rk in sigs.values()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before-db", required=True)
    ap.add_argument("--before", nargs=2, required=True, metavar=("START", "END"))
    ap.add_argument("--after-db", required=True)
    ap.add_argument("--after", nargs=2, required=True, metavar=("START", "END"))
    a = ap.parse_args()
    A = calls(a.before_db, *a.before)
    B = calls(a.after_db, *a.after)
    both = sorted(set(A) & set(B), key=lambda k: -(B[k]["mean"] - A[k]["mean"]))

    print(f"BEFORE {a.before[0]} .. {a.before[1]}    AFTER {a.after[0]} .. {a.after[1]}")
    print(f"Channels traded in both periods: {len(both)}"
          f"   before-only: {len(set(A)-set(B))}   after-only: {len(set(B)-set(A))}\n")
    hdr = (f"{'channel':<32}{'n':>4}{'R':>7}{'  →':>4}{'n':>5}{'R':>7}{'ΔR':>8}"
           f"{'  separable?':>14}")
    print(hdr); print("-" * len(hdr))
    for k in both:
        x, y = A[k], B[k]
        d = y["mean"] - x["mean"]
        # Two-sample: is the change bigger than the noise in either estimate?
        if x["se"] and y["se"]:
            se = math.sqrt(x["se"] ** 2 + y["se"] ** 2)
            sep = "yes" if abs(d) > 1.96 * se else f"no (±{1.96*se:.2f})"
        else:
            sep = "n too small"
        print(f"{x['name']:<32}{x['n']:>4}{x['mean']:>7.2f}{'  →':>4}"
              f"{y['n']:>5}{y['mean']:>7.2f}{d:>+8.2f}{sep:>14}")
    print("-" * len(hdr))
    for lbl, S in (("before only", set(A) - set(B)), ("after only", set(B) - set(A))):
        for k in sorted(S):
            src = A if k in A else B
            print(f"  {lbl}: {src[k]['name']} ({src[k]['n']} calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
