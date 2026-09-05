#!/usr/bin/env python3
"""
tools/apply_breakeven_policy.py
===============================
Sets each channel's breakeven policy from evidence about that channel.

THE EVIDENCE PROBLEM, stated first because it changes what can be concluded.

Mr David FX's `on_tp_index: 2` came from OUR OWN FILLS: three signals on
2026-08-26/27 banked TP1 and TP2 and then handed back -83.15 on leg 3. That is
the strongest kind of evidence and nothing here replaces it.

The eleven channels added on 2026-08-30 have never traded. Their only history is
their message exports, and for six of them that history is a highlight reel:

    channel              "TP hit" posts   "SL hit" posts
    Mr William FX                   669                0
    FX4Team                         928                1
    John Wick FX                    216                0
    XAU Daily Signals               200                0
    Analysis Lab                    138                0
    Green Pips Zone                 170                0

A loss rate cannot be estimated from a sample with the losses removed. So the
outcome history is unusable for six of eleven, and the only thing their exports
say honestly is GEOMETRY - where they place TP1 relative to their stop.

WHAT GEOMETRY SETTLES

  R1 = distance(fill -> TP1) / distance(fill -> SL)
  banked1 = R1 / n_legs        (TP1 closes one leg of n)

A TP1 at 0.3R is inside ordinary noise: price reaches it and comes back through
entry routinely. Arming a breakeven there converts a large share of eventual
winners into scratches. A TP1 at 1.0R is past noise and worth protecting.

Measured over every complete ladder in every export (tools/breakeven_geometry.py):

    channel                  legs     R1     R2   banked1   verdict
    Mr William FX               6   0.30   0.60      0.05   operator + backstop
    FX4Team                     5   0.42   0.83      0.08   TP2
    Mr Zack FX                  4   0.42   0.90      0.10   TP2
    Analysis Lab                6   0.31   0.60      0.05   operator + backstop
    Green Pips Zone             5   0.30   0.60      0.06   operator + backstop
    Orient Forex                3   0.40   0.90      0.13   TP2
    Isabelle FX                 2   0.42   0.83      0.21   her own rule
    Sabin Gold                  4   0.17   0.33      0.04   operator + backstop
    John Wick FX                4   0.29   0.58      0.07   operator + backstop
    XAU Daily Signals           4   0.30   0.60      0.07   operator + backstop
    Emily Pips                  4   0.45   0.91      0.11   TP2
    Golden Signals              3   0.71   1.43      0.24   TP2
    Elites Trader               3   0.50   1.00      0.17   TP2
    Jason Noah FX               2   0.29   0.58      0.14   operator + backstop
    Lucas Gold Legend           3   0.38   0.77      0.13   operator + backstop
    Dubai King Gold             5   0.25   0.50      0.05   operator + backstop
    Hamza FX Gold               9   0.25   0.48      0.03   operator + backstop
    Gold Layer Scalper          2   0.29   0.67      0.14   operator + backstop
    Forex Expert Team           2   0.29   0.67      0.14   operator + backstop

NOT ONE CHANNEL QUALIFIES FOR A TP1 BREAKEVEN. Every TP1 in the roster sits
between 0.17R and 0.71R. That is the direct answer to "should we make it TP1?":
no, nowhere, on this evidence. The worry about TP1 being too tight was correct.

The fills that do exist agree with the geometry wherever both are available:

    channel           signals  hit TP1  hit TP2  banked-then-stopped  given back
    Mr David FX            17       11        9                    5     -188.02
    Golden Signals          6        5        3                    2      -73.54
    Gold_PRO_Trader         7        3        2                    1      -52.00
    Dubai King Gold         7        5        5                    1      -16.39
    GTMO FX                16        9        8                    9       -3.79

Golden Signals gives back real money and geometry says TP2 - set. GTMO trips the
pattern nine times for -3.79 total, so an index rule buys nothing there and it
is left alone.

THE BACKSTOP, and why it is not a strategy

Five of the six "operator message only" channels essentially never post a
breakeven instruction: Mr William 3 in 1427 messages, Analysis Lab 2 in 941,
John Wick 1 in 1282, Green Pips 0 in 1431, XAU Daily 0 in 405. For those,
"operator message only" means NO BREAKEVEN AT ALL - a runner rides from a
banked target back to a full stop with nothing in the way.

So they get `on_pips` at 1.5R, computed from each channel's own median
fill-to-stop distance. 1.5R is deliberately far past the whole ladder: it can
only fire on a trade that is already a decisive winner, so it cannot turn a
winner into a loser or a loser into a winner. It caps one specific tail - "was
deeply in profit, gave it all back" - and perturbs the R-multiple ranking
almost not at all, which matters because this week is a measurement.

Sabin Gold is the exception among the operator-only group: 122 breakeven
instructions in 972 messages. He manages constantly and on_operator_message
does the work. He still gets the backstop for his "TP: open" runner leg, which
has no target and can travel a long way.

Isabelle FX states her rule in the text of every signal - "SL to BE whenever
profit 20pips+" - so hers is `on_pips: 20`, her number, not ours. Note it is
0.17R against her $12 median stop, far tighter than anything else here, and she
posts only two targets so an index rule could never arm on anything but her
final leg. Worth watching for churn.

WHAT TO REDO NEXT WEEK

Once these channels have real fills, rerun the leg2.MAE-minus-leg1.MAE study
from 2026-08-27. Measured retracement beats posted geometry every time, and
every setting written here should be replaced by it.

Usage:  python tools/apply_breakeven_policy.py <channels.json> [-o out.json]
"""
import argparse
import json
import sys

# on_tp_index > 0  : arm once that many targets are banked
# on_pips     > 0  : arm once price is this many pips beyond the leg's entry
# Both may be set; they are OR'd. slack/min_profit come from defaults.breakeven.
POLICY = {
    # ── arm at TP2: R2 >= 0.8 and the ladder has legs left after TP2 ──────────
    "-1001754612869": dict(tp=2, pips=0, why=[
        "R1 0.42, R2 0.83 over 256 complete ladders, five legs.",
        "TP2 banks 25% of the position at 0.83R, which is past noise and",
        "leaves three legs to protect."]),
    "-1001810326498": dict(tp=2, pips=0, why=[
        "R1 0.42, R2 0.90 over 223 ladders, four legs.",
        "TP2 banks 33% at 0.90R with two legs still running."]),
    "-1002470316467": dict(tp=2, pips=0, why=[
        "R1 0.40, R2 0.90 over 25 ladders, three legs.",
        "Only 25 ladders because this channel posts roughly two clean tickets",
        "a week, but all 25 have identical geometry: $5 zone, $5 stop, three",
        "targets. Uniform enough for an index rule."]),
    "-1004335397917": dict(tp=2, pips=0, why=[
        "R1 0.45, R2 0.91 over 60 ladders, four legs.",
        "The widest TP2 in the batch relative to stop."]),
    "-1001957768371": dict(tp=2, pips=0, why=[
        "R1 0.71, R2 1.43 over 235 ladders, three legs - the widest ladder on",
        "the roster, and the only channel that comes close to justifying TP1.",
        "It does not: R1 0.71 is still inside noise.",
        "The fills agree. Six signals traded, five reached TP1, three reached",
        "TP2, and two then handed back -73.54 on a later leg. That is the",
        "exact pattern this setting stops."]),
    "-1003447617877": dict(tp=2, pips=0, why=[
        "R1 0.50, R2 1.00 over 72 ladders, three legs.",
        "Five signals traded, three reached TP1, two banked a target and then",
        "stopped out on a later leg."]),

    # ── operator message plus a 1.5R backstop ────────────────────────────────
    # pips = round(1.5 x median fill-to-stop distance / pip_value) to the nearest 5.
    "-1001449395038": dict(tp=0, pips=150, why=[
        "R1 0.30, R2 0.60 over 147 ladders, six legs. Even TP5 is under 1R.",
        "No index rule can arm outside noise on a ladder this tight.",
        "THREE breakeven instructions in 1427 messages, so 'operator only'",
        "would mean no breakeven at all.",
        "150 pips = $15.00 = 1.5x the $10.00 median fill-to-stop distance."]),
    "-1001825393261": dict(tp=0, pips=165, why=[
        "R1 0.31, R2 0.60 over 121 ladders, six legs.",
        "Two breakeven instructions in 941 messages.",
        "165 pips = $16.50 = 1.5x the $11.00 median stop."]),
    "-1002145295870": dict(tp=0, pips=150, why=[
        "R1 0.30, R2 0.60 over 149 ladders, five legs.",
        "ZERO breakeven instructions in 1431 messages. This operator posts",
        "tickets and results and never manages a trade in the channel.",
        "150 pips = $15.00 = 1.5x the $10.00 median stop."]),
    "-1003331716434": dict(tp=0, pips=180, why=[
        "R1 0.17, R2 0.33, R3 0.50 over 40 ladders - the tightest ladder in",
        "the roster. His whole target stack sits inside half his stop, so no",
        "index rule belongs here.",
        "He does not need one: 122 breakeven instructions in 972 messages,",
        "plus 'Set Breakeven!', 'SL to Entries!' and explicit stop moves.",
        "on_operator_message carries this channel.",
        "The backstop exists only for his 'TP: open' runner leg, which has no",
        "target and can travel a long way after the ladder is done.",
        "180 pips = $18.00 = 1.5x the $12.00 median stop."]),
    "-1003778011534": dict(tp=0, pips=195, why=[
        "R1 0.29, R2 0.58 over 167 ladders, four legs.",
        "ONE breakeven instruction in 1282 messages.",
        "195 pips = $19.50 = 1.5x the $13.00 median stop."]),
    "-1003864453549": dict(tp=0, pips=150, why=[
        "R1 0.30, R2 0.60 over 59 ladders, four legs.",
        "ZERO breakeven instructions in 405 messages.",
        "150 pips = $15.00 = 1.5x the $10.00 median stop."]),
    "-1002039699353": dict(tp=0, pips=210, why=[
        "R1 0.29, R2 0.58 over 133 ladders, and only two legs - so TP2 is the",
        "LAST leg and an index rule there would arm on nothing.",
        "210 pips = $21.00 = 1.5x the $14.00 median stop."]),
    "-1002393457678": dict(tp=0, pips=195, why=[
        "R1 0.38, R2 0.77 over 103 ladders, three legs. R2 lands just under",
        "the 0.8R bar, so no index rule.",
        "195 pips = $19.50 = 1.5x the $13.00 median stop."]),
    "-1002587979027": dict(tp=0, pips=120, why=[
        "R1 0.25, R2 0.50 over 146 ladders, five legs.",
        "Seven signals traded; one banked a target then gave back -16.39.",
        "120 pips = $12.00 = 1.5x the $8.00 median stop."]),
    "-1002668402427": dict(tp=0, pips=300, why=[
        "R1 0.25, R2 0.48 over 196 ladders, NINE legs. Each target banks 11%",
        "of the position, so banked1 is 0.03R - the lowest on the roster.",
        "300 pips = $30.00 = 1.5x the $20.00 median stop."]),
    "-1003353497021": dict(tp=0, pips=210, why=[
        "R1 0.29, R2 0.67 over 46 ladders, two legs, so TP2 is the last leg.",
        "210 pips = $21.00 = 1.5x the $14.00 median stop."]),
    "-1003669904323": dict(tp=0, pips=210, why=[
        "R1 0.29, R2 0.67 over 109 ladders, two legs, so TP2 is the last leg.",
        "210 pips = $21.00 = 1.5x the $14.00 median stop."]),
}

# Left deliberately alone, with the reason recorded rather than implied.
UNTOUCHED = {
    "-1002215701365": "Mr David FX - on_tp_index 2 came from our own fills "
                      "(-188.02 handed back on later legs across five signals). "
                      "Measured retracement beats posted geometry; do not "
                      "overwrite it with a weaker method.",
    "-1003298640045": "Isabelle FX - on_pips 20 is her own stated rule, posted "
                      "on every signal. Already set.",
    "-1002495224665": "GTMO FX - trips the banked-then-stopped pattern nine "
                      "times in sixteen signals for -3.79 in total. The legs "
                      "are tiny and the operator already manages them. An index "
                      "rule would buy nothing and cost fills.",
    "-1001799443074": "Lion Trading Academy - excluded from this week's changes "
                      "on instruction.",
    "-1002102979885": "Gabriel Lopez FX - no complete ladder in 1155 messages. "
                      "Nothing to measure.",
    "-1002070727316": "Gold_PRO_Trader - no export. One signal gave back -52.00, "
                      "which is suggestive, but seven traded signals is not a "
                      "basis for a rule. Revisit with next week's fills.",
    "-1001643924999": "Gold Hunter (Paul) - no export, one signal reached TP1.",
    "-1001765226347": "Wall Street Ben - no export, one signal reached TP1.",
    "-1002284339674": "Prime Market Makers - no export, no fills.",
    "-1002193412690": "Wealth Growth Lab - no export, no fills.",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()

    cfg = json.load(open(a.path, encoding="utf-8"))
    dflt_be = cfg.get("defaults", {}).get("breakeven", {})
    slack = dflt_be.get("slack_pips", 5)
    minp = dflt_be.get("min_profit_pips", 12)

    n_tp = n_pips = 0
    for ch in cfg["channels"]:
        pol = POLICY.get(ch["id"])
        if not pol:
            continue
        be = dict(ch.get("breakeven") or {})
        be.update({
            "_why": pol["why"],
            "on_operator_message": True,
            "on_tp_index": pol["tp"],
            "on_pips": pol["pips"],
            "buffer_pips": 0,
            "slack_pips": slack,
            "min_profit_pips": minp,
        })
        ch["breakeven"] = be
        if pol["tp"]:
            n_tp += 1
        if pol["pips"]:
            n_pips += 1

    out = a.out or a.path
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"breakeven policy written for {len(POLICY)} channels")
    print(f"  arm at TP2:            {n_tp}")
    print(f"  1.5R on_pips backstop: {n_pips}")
    print(f"  arm at TP1:            0   (no channel's TP1 clears 0.8R)")
    print(f"  left untouched:        {len(UNTOUCHED)}")
    for cid, why in UNTOUCHED.items():
        print(f"     {cid}  {why.splitlines()[0][:88]}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
