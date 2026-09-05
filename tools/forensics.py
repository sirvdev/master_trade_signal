#!/usr/bin/env python3
"""
tools/forensics.py
==================
The post-mortem that the weekly scorecard does not do. The scorecard grades the
channels; this asks why the account lost money regardless of who was right.

Every section here was written because it found something on 2026-09-05:

  mfe        85% of stop-outs were in profit first, and the breakeven backstop
             was configured at 1.5R when the median stop-out peaked at 0.43R.
  drawdown   the per-channel breaker is a DAILY measure, so a channel bleeding
             6% a day never trips a 30% limit while ending the week down a third.
  edits      the listener re-processes Telegram edits and the idempotency guard
             refuses them, so 16 operator price corrections were discarded.
  fills      opened_at is stamped when an order is PLACED, not filled, so the
             scorecard's fill rate counted unfilled resting limits as filled.
  labels     22 profitable trailing exits are recorded with close_reason='sl',
             which understates every win rate the system reports.
  daily      decomposes the loss curve into count, size, win rate and direction.

Usage:  python tools/forensics.py <signals.db> <channels.json> [--log <system.log>]
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict

CONTRACT = 100.0
TRADED = {"tp", "sl", "expert", "bare_scaleout"}


def pct(a, q):
    if not a:
        return 0.0
    a = sorted(a)
    return a[min(len(a) - 1, int(q * (len(a) - 1)))]


def section(t):
    print(f"\n{'═' * 78}\n{t}\n{'═' * 78}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db"); ap.add_argument("channels")
    ap.add_argument("--log")
    a = ap.parse_args()
    c = sqlite3.connect(a.db); c.row_factory = sqlite3.Row
    NAME = {x["id"]: x["name"] for x in
            json.load(open(a.channels, encoding="utf-8"))["channels"]}

    # ── 1. how the loss accumulated ──────────────────────────────────────────
    section("1. THE LOSS CURVE — count, size, win rate, direction")
    print(f"{'day':<12}{'legs':>6}{'avg risk':>10}{'win':>6}{'R':>7}"
          f"{'BUY P&L':>10}{'SELL P&L':>10}{'total':>10}")
    for r in c.execute("""
        SELECT substr(p.closed_at,1,10) d, count(*) legs,
          round(avg(p.lot_size*abs(p.entry_price-s.stop_loss)*100),2) avgrisk,
          sum(case when lower(p.close_reason)='tp' then 1 else 0 end) tp,
          sum(case when lower(p.close_reason)='sl' then 1 else 0 end) sl,
          round(sum(p.lot_size*abs(p.entry_price-s.stop_loss)*100),0) risk,
          round(sum(case when lower(s.direction)='buy'  then p.pnl else 0 end),2) bp,
          round(sum(case when lower(s.direction)='sell' then p.pnl else 0 end),2) sp,
          round(sum(p.pnl),2) pnl
        FROM positions p JOIN signals s ON s.signal_id=p.signal_id
        WHERE p.status='closed' AND p.entry_price>0 AND s.stop_loss>0
        GROUP BY d ORDER BY d"""):
        res = r["tp"] + r["sl"]
        print(f"{r['d']:<12}{r['legs']:>6}{r['avgrisk']:>10.2f}"
              f"{100*r['tp']/res if res else 0:>5.0f}%"
              f"{r['pnl']/r['risk'] if r['risk'] else 0:>7.2f}"
              f"{r['bp']:>10.2f}{r['sp']:>10.2f}{r['pnl']:>10.2f}")
    print("\nFlat average risk with a worsening R means the loss is NOT sizing.")

    # ── 2. what the stop-outs gave back ──────────────────────────────────────
    section("2. STOP-OUTS — how much profit existed before they reversed")
    legs = []
    for r in c.execute("""SELECT p.pnl, p.mfe, p.lot_size, p.entry_price,
          p.close_reason cr, s.stop_loss osl
        FROM positions p JOIN signals s ON s.signal_id=p.signal_id
        WHERE p.status='closed' AND p.entry_price>0 AND p.lot_size>0
          AND s.stop_loss>0"""):
        risk = float(r["lot_size"]) * abs(float(r["entry_price"]) - float(r["osl"])) * CONTRACT
        if risk > 0:
            legs.append(dict(pnl=float(r["pnl"] or 0), risk=risk,
                             mfe=float(r["mfe"] or 0), cr=(r["cr"] or "").lower()))
    stops = [l for l in legs if l["cr"] == "sl" and l["pnl"] <= 0]
    peaks = sorted(max(0.0, l["mfe"]) / l["risk"] for l in stops)
    print(f"genuinely losing stop-outs: {len(stops)}")
    print(f"  in profit at some point : {sum(1 for p in peaks if p>0)} "
          f"({100*sum(1 for p in peaks if p>0)/max(1,len(peaks)):.0f}%)")
    print(f"  peak reached, in R      : p50 {pct(peaks,.5):.2f}  p75 {pct(peaks,.75):.2f}  "
          f"p90 {pct(peaks,.9):.2f}")
    for t in (0.25, 0.5, 0.75, 1.0, 1.5):
        n = sum(1 for p in peaks if p >= t)
        print(f"  reached +{t:.2f}R and still stopped: {n:>4}  ({100*n/max(1,len(peaks)):.0f}%)")
    print("\nCompare the p50 against the channel's configured breakeven arm point.")
    print("A backstop above the p75 is a backstop that will essentially never fire.")

    print("\n  PARTIAL-PROFIT COUNTERFACTUAL (close `frac` at the trigger, stop unmoved)")
    base = sum(l["pnl"] for l in legs)
    print(f"  actual realised over {len(legs)} legs: {base:+.2f}")
    print(f"  {'trigger':>9}{'frac':>7}{'legs hit':>10}{'simulated':>12}{'delta':>10}")
    for thr in (0.4, 0.5, 0.75, 1.0):
        for frac in (0.33, 0.5):
            tot = 0.0; n = 0
            for l in legs:
                if l["mfe"] >= thr * l["risk"]:
                    n += 1
                    tot += thr * l["risk"] * frac + l["pnl"] * (1 - frac)
                else:
                    tot += l["pnl"]
            print(f"  {thr:>8.2f}R{frac:>7.0%}{n:>10}{tot:>12.0f}{tot-base:>+10.0f}")
    print("  Optimistic: assumes a fill at the peak. The SIGN is the finding.")

    # ── 3. cumulative vs daily drawdown ──────────────────────────────────────
    section("3. DRAWDOWN — the daily breaker cannot see a slow bleed")
    print(f"{'channel':<32}{'start':>8}{'now':>10}{'cumulative':>12}{'worst day':>11}")
    worst = {r["channel_id"]: r["dd"] for r in c.execute(
        "SELECT channel_id, max(drawdown_pct) dd FROM channel_stats GROUP BY channel_id")}
    for r in c.execute("SELECT * FROM channel_balances ORDER BY system_balance"):
        cum = (r["system_balance"] - r["starting_balance"]) / r["starting_balance"] * 100
        if cum > -5:
            continue
        print(f"{NAME.get(r['channel_id'], r['channel_id']):<32}"
              f"{r['starting_balance']:>8.0f}{r['system_balance']:>10.2f}"
              f"{cum:>+11.1f}%{worst.get(r['channel_id'],0):>10.1f}%")
    print("\nA channel whose worst DAY is inside the limit can still end the week")
    print("far outside it. The ledger already holds the cumulative number.")

    # ── 4. close-reason integrity ────────────────────────────────────────────
    section("4. LABELS AND FILLS — what the raw counts get wrong")
    r = c.execute("SELECT count(*) n, round(sum(pnl),2) v FROM positions "
                  "WHERE status='closed' AND lower(close_reason)='sl' AND pnl>0").fetchone()
    print(f"profitable legs recorded as close_reason='sl': {r['n']} worth {r['v']:+.2f}")
    print("  (trailing-stop exits in profit — they understate every win rate)")
    d = defaultdict(lambda: [0, 0, 0])
    for r in c.execute("SELECT channel_id cid, close_reason cr, opened_at oa FROM positions"):
        x = d[r["cid"]]
        if r["oa"] is None:
            x[2] += 1
        elif (r["cr"] or "").lower() in TRADED:
            x[0] += 1
        else:
            x[1] += 1
    print(f"\n{'channel':<32}{'traded':>8}{'placed, no fill':>17}{'never placed':>14}{'true fill':>11}")
    for cid, (t, f, u) in sorted(d.items(), key=lambda kv: kv[1][0] / max(1, sum(kv[1]))):
        tot = t + f + u
        if not tot:
            continue
        print(f"{NAME.get(cid,cid):<32}{t:>8}{f:>17}{u:>14}{100*t/tot:>10.0f}%")
    print("\nopened_at is stamped when an order is PLACED. A resting limit that never")
    print("filled looks identical to a filled position unless you check the outcome.")

    # ── 5. edits ─────────────────────────────────────────────────────────────
    if a.log:
        section("5. EDITED MESSAGES — corrections the duplicate guard threw away")
        byid = defaultdict(list)
        for ln in open(a.log, encoding="utf-8", errors="replace"):
            m = re.search(r"\[([^\]]+)\] \[\d\d:\d\d:\d\dZ\] msg_id=(\d+): (.*)$", ln)
            if m:
                byid[(m.group(1), m.group(2))].append(m.group(3).rstrip())
        NUM = re.compile(r"\d{3,5}(?:\.\d{1,3})?")
        dup = {k: list(dict.fromkeys(v)) for k, v in byid.items() if len(v) > 1}
        cosmetic = price = 0
        for (ch, mid), u in dup.items():
            if len(u) < 2:
                cosmetic += 1
                continue
            sets = [set(NUM.findall(t)) for t in u]
            if sets[0] != sets[-1]:
                price += 1
                print(f"  [{ch}] msg {mid}: removed {sorted(sets[0]-sets[-1]) or '-'}  "
                      f"added {sorted(sets[-1]-sets[0]) or '-'}")
            else:
                cosmetic += 1
        print(f"\n  re-delivered ids: {len(dup)}   cosmetic/identical: {cosmetic}   "
              f"A PRICE CHANGED: {price}")
        print("  Every one of those corrections was refused by the idempotency guard.")

        section("6. FAULTS IN THE LOG")
        txt = open(a.log, encoding="utf-8", errors="replace").read()
        for label, pat in [
            ("account guard trips", r"ACCOUNT GUARD TRIPPED"),
            ("per-channel halts", r"HALTED — drawdown"),
            ("invalid-price order rejections", r"'code': 10015"),
            ("bridge timeouts", r"timed out after 30\.0s"),
            ("monitor cycles skipped", r"returned None — skipping cycle"),
            ("concurrent duplicate refusals", r"already being executed right now"),
            ("clock-skew alarms", r"\[CLOCK\] system clock"),
            ("notifier failures", r"\[NOTIFIER\] Send failed"),
            ("AI parser failures", r"\[AI\] .*(failed|unavailable|error)"),
            ("telegram reconnects", r"Server closed the connection|PersistentTimestampOutdated"),
        ]:
            print(f"  {label:<34}{len(re.findall(pat, txt)):>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
