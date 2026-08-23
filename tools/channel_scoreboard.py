#!/usr/bin/env python3
"""
tools/channel_scoreboard.py
===========================
Ranks signal sources by realised performance, so "which channel is actually
worth following" is answered from your own fills rather than from the
operator's screenshots.

Everything it needs is already recorded: `signals` carries channel_id and
channel_name, `positions` carries channel_id, lot_size, pnl, status and
close_reason. This only reads.

Usage
-----
    python tools/channel_scoreboard.py
    python tools/channel_scoreboard.py --since 2026-08-01
    python tools/channel_scoreboard.py --by close-reason
    python tools/channel_scoreboard.py --csv scoreboard.csv

Reading it
----------
    net       realised P/L, sum of closed position pnl
    PF        profit factor: gross win / gross loss. Below 1.0 loses money.
    exp       expectancy per closed position, in account currency
    win%      share of closed positions with pnl > 0

A channel with a high win% and a PF below 1 is taking many small wins and a few
large losses, which is the usual shape of a signal service that looks good in
screenshots. Judge on PF and expectancy, not win%.

`n` counts POSITIONS, not signals: one signal with four TPs opens four
positions, so a channel posting more TP legs accumulates n faster. `sigs` is the
signal count if you want per-signal figures.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def rows(db: str, since: str | None):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    where, args = "p.status='closed' AND p.pnl IS NOT NULL", []
    if since:
        where += " AND COALESCE(p.closed_at, p.opened_at) >= ?"
        args.append(since)
    sql = f"""
        SELECT p.channel_id, p.pnl, p.lot_size, p.close_reason, p.signal_id,
               COALESCE(s.channel_name, p.channel_id) AS channel_name
        FROM positions p
        LEFT JOIN signals s ON s.signal_id = p.signal_id
        WHERE {where}
    """
    out = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return out


def summarise(recs, key):
    acc = {}
    for r in recs:
        k = r[key] or "(unattributed)"
        a = acc.setdefault(k, {"name": r["channel_name"], "n": 0, "wins": 0,
                               "gw": 0.0, "gl": 0.0, "lots": 0.0,
                               "sigs": set()})
        pnl = float(r["pnl"] or 0.0)
        a["n"] += 1
        a["lots"] += float(r["lot_size"] or 0.0)
        a["sigs"].add(r["signal_id"])
        if pnl > 0:
            a["wins"] += 1
            a["gw"] += pnl
        else:
            a["gl"] += -pnl
    for k, a in acc.items():
        a["key"] = k
        a["net"] = a["gw"] - a["gl"]
        a["pf"] = (a["gw"] / a["gl"]) if a["gl"] > 0 else float("inf")
        a["exp"] = a["net"] / a["n"] if a["n"] else 0.0
        a["winpct"] = 100.0 * a["wins"] / a["n"] if a["n"] else 0.0
        a["sigs"] = len(a["sigs"])
    return sorted(acc.values(), key=lambda a: a["net"], reverse=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("DB_PATH", "data/signals.db"))
    ap.add_argument("--since", help="ISO date, e.g. 2026-08-01")
    ap.add_argument("--by", default="channel",
                    choices=["channel", "close-reason"])
    ap.add_argument("--csv", help="also write the table to this file")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        print(f"No database at {a.db}. Set --db or DB_PATH.")
        return 2

    recs = rows(a.db, a.since)
    if not recs:
        print(f"No closed positions with a recorded P/L in {a.db}"
              + (f" since {a.since}" if a.since else "")
              + ".\nNothing has round-tripped yet, so there is nothing to rank.")
        return 0

    if a.by == "close-reason":
        for r in recs:
            r["channel_id"] = r["close_reason"] or "(none)"
            r["channel_name"] = r["channel_id"]

    table = summarise(recs, "channel_id")
    hdr = f"{'source':32}{'n':>5}{'sigs':>6}{'win%':>7}{'net':>12}{'PF':>8}{'exp':>9}{'lots':>8}"
    print(f"\nclosed positions{' since ' + a.since if a.since else ''}: "
          f"{len(recs)}\n")
    print(hdr)
    print("-" * len(hdr))
    for t in table:
        pf = "inf" if t["pf"] == float("inf") else f"{t['pf']:.2f}"
        label = f"{t['name']}"[:30]
        print(f"{label:32}{t['n']:>5}{t['sigs']:>6}{t['winpct']:>7.1f}"
              f"{t['net']:>12.2f}{pf:>8}{t['exp']:>9.2f}{t['lots']:>8.2f}")
    tot_net = sum(t["net"] for t in table)
    tot_n = sum(t["n"] for t in table)
    print("-" * len(hdr))
    print(f"{'TOTAL':32}{tot_n:>5}{'':>6}{'':>7}{tot_net:>12.2f}")

    losers = [t for t in table if t["pf"] < 1.0 and t["n"] >= 10]
    if losers:
        print("\nNegative expectancy over 10+ closed positions:")
        for t in losers:
            print(f"  {t['name']}  net {t['net']:.2f}  PF {t['pf']:.2f}  "
                  f"exp {t['exp']:.2f}/position")
        print("  Sample size is still small. Judge on PF and expectancy, "
              "not win%.")

    thin = [t for t in table if t["n"] < 10]
    if thin:
        print(f"\n{len(thin)} source(s) have fewer than 10 closed positions. "
              f"Their numbers are noise, not evidence.")

    if a.csv:
        cols = ["key", "name", "n", "sigs", "winpct", "net", "pf", "exp", "lots"]
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(table)
        print(f"\nwrote {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
