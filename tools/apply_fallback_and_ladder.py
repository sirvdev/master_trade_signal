#!/usr/bin/env python3
"""
tools/apply_fallback_and_ladder.py
==================================
Three policy changes, applied on instruction 2026-08-30:

1. A UNIVERSAL 70-pip stop fallback. No signal is dropped for an unreadable
   stop any more. defaults.parser.default_sl_pips = 70 applies to every
   channel, and the per-channel overrides (Lucas 80, Mr William 70, Analysis
   Lab 100, John Wick 110, Emily Pips 110) are removed so there is one number,
   not six that drift apart.

2. ZONE LADDERING on the channels that genuinely post ranges. zone_fill
   "ladder" spreads the legs across the posted zone instead of taking one
   market fill at the worst edge.

3. A 70-pip protective stop on bare calls, with no fixed target, handed to the
   EA's trailing engine at 1R. (Code side: PRE_SIGNAL_SL_PIPS_DEFAULT and
   RUNNER_TRAIL_ARM_R.)

WHAT THE EVIDENCE SAYS ABOUT 70, recorded because it argues the other way.

747 closed legs carry a recorded entry, stop and maximum adverse excursion.
Replaying them with a flat stop at various distances, holding DOLLAR RISK
CONSTANT (the sizer already does this: risk_amt = risk_pct x book, so a tighter
stop simply buys a bigger lot):

    flat stop     simulated P&L    legs stopped    winners turned into losers
      $5.00           -7816.79             397                            39
      $7.00           -5588.60             309                            28
      $9.00           -4602.72             250                            13
     $11.00           -4190.36             200                             6
     $13.00           -3441.05             146                             1
     $15.00           -2910.11             115                             0
     $20.00           -1645.54              53                             0
    (actual, using each operator's own stop:  -9.93)

Monotonic. Tighter is worse, at every step. 41% of all legs put more than $7.00
of adverse excursion on the board before resolving, and MAE p75 is $11.70,
p90 $18.00.

The mechanism is worth stating plainly because it is counter-intuitive: a
tighter stop does NOT risk fewer dollars. Dollar risk is fixed at risk_pct of
the book whatever the stop distance. The stop distance only decides two things:
how big the lot is, and how often the stop is hit. So a 70-pip synthetic stop
is not a cautious choice - it is the same money, staked on a narrower band.

Two things blunt that finding rather than overturning it:
  - The simulation replaces EVERY stop, including the ones the operator posted
    correctly. The actual policy touches only the unreadable subset, which is a
    few percent of signals, so the effect is proportionally smaller.
  - Every synthesised stop is tagged in the signal's notes ("SL synthesised at
    70 pips"), so the affected trades can be graded as their own population at
    the end of the week rather than confounding the channel comparison.

Set defaults.parser.default_sl_pips to 120-180 if the week shows the synthetic
subset stopping out far more than the readable one. That is the single number
to change; nothing else depends on it.

Usage:  python tools/apply_fallback_and_ladder.py <channels.json> [-o out.json]
"""
import argparse
import json
import sys

FALLBACK_PIPS = 70

# Channels whose exports show they really post ranges: at least 85% of their
# tradeable signals carry two distinct bounds, and the median width is wide
# enough that spreading legs across it means something.
#   ladder_cancel_pips = that channel's own median stop distance. Once price
#   has run a full R past the leg that filled, the unfilled discount legs are
#   no longer a discount and _manage_ladders deletes them.
LADDER = {
    "-1001449395038": dict(cancel=100, why="97% of signals are zones, median width $3.00"),
    "-1002039699353": dict(cancel=140, why="99% zones, median width $10.00"),
    "-1002393457678": dict(cancel=130, why="89% zones, median width $5.00"),
    "-1002470316467": dict(cancel=100, why="96% zones, median width $5.00"),
    "-1002668402427": dict(cancel=200, why="96% zones, median width $5.00, nine legs"),
    "-1003298640045": dict(cancel=120, why="100% zones, median width $5.00, two legs"),
    "-1003331716434": dict(cancel=120, why="100% zones, median width $4.00"),
    "-1003353497021": dict(cancel=140, why="90% zones, median width $10.00"),
    "-1003447617877": dict(cancel=100, why="94% zones, median width $5.00"),
    "-1003778011534": dict(cancel=130, why="87% zones, median width $4.00 — "
                                           "readable as zones only since the "
                                           "'4816__4814' separator fix"),
    "-1004335397917": dict(cancel=110, why="95% zones, median width $4.00"),
}

# Measured and deliberately NOT laddered.
NO_LADDER = {
    "-1001754612869": "FX4Team posts a single entry price, never a range (0% zones).",
    "-1001957768371": "Golden Signals: 0% zones.",
    "-1003864453549": "XAU Daily Signals: 0% zones.",
    "-1001810326498": "Mr Zack: only 25% of signals are zones.",
    "-1001825393261": "Analysis Lab: only 20%.",
    "-1002145295870": "Green Pips Zone: only 21%, despite the name.",
    "-1003669904323": "Forex Expert Team: only 27%.",
    "-1002587979027": "Dubai King Gold: 99% zones but the median width is $2.00. "
                      "Five legs across two dollars is five legs at the same "
                      "price with extra pending orders to manage.",
    "-1002102979885": "Gabriel Lopez: one signal in 1155 messages.",
    "-1002495224665": "GTMO FX posts zones but has no export to measure. Left on "
                      "'worst' until one exists.",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()

    cfg = json.load(open(a.path, encoding="utf-8"))

    # 1. The fallback, globally.
    d = cfg["defaults"]["parser"]
    d["default_sl_pips"] = FALLBACK_PIPS
    d["pre_signal_sl_pips"] = FALLBACK_PIPS
    d["max_zone_width"] = 30.0
    d["_default_sl_pips_note"] = [
        "UNIVERSAL FALLBACK. When a stop cannot be read - withheld ('SL: VIP',",
        "'SL :- PAID'), mangled by markdown, or posted as a number that cannot",
        "be true (wrong side of entry, or further away than max_sl_distance) -",
        "the stop is synthesised this far from the PROTECTIVE edge of the zone",
        "rather than the signal being dropped.",
        "A stop is only rescued from a bad posted number when every take profit",
        "in the same message is on the correct side of the entry. If the",
        "targets disagree too, the message was mis-parsed and is still refused.",
        "Every trade taken this way is tagged 'SL synthesised at 70 pips' in",
        "the signal notes, so it can be graded separately at the end of the week.",
        "",
        "70 pips = $7.00. This is TIGHTER than any channel's own median stop",
        "($8.00 to $20.00) and tighter than the corpus-wide MAE p75 of $11.70.",
        "Replaying 747 closed legs at constant dollar risk, a flat $7.00 stop",
        "returns -5588.60 against -9.93 actual, and the curve is monotonic:",
        "$9 -4602, $11 -4190, $13 -3441, $15 -2910, $20 -1645.",
        "Dollar risk does not change with stop distance - the sizer keeps it at",
        "risk_pct of the book - so a tighter stop buys a bigger lot and is hit",
        "more often, not risked less. Raise this to 120-180 if the synthesised",
        "population stops out materially more than the readable one.",
    ]

    # 2. Remove the per-channel overrides so there is one number.
    removed = []
    for ch in cfg["channels"]:
        p = ch.get("parser") or {}
        if "default_sl_pips" in p:
            removed.append((ch["id"], ch["name"], p.pop("default_sl_pips")))
            p.pop("_sl_fallback_note", None)

    # 3. Laddering.
    laddered = []
    for ch in cfg["channels"]:
        spec = LADDER.get(ch["id"])
        if not spec:
            continue
        p = ch.setdefault("parser", {})
        p["zone_fill"] = "ladder"
        p["min_zone_width"] = 2.0
        p["max_zone_width"] = 30.0
        p["ladder_cancel_pips"] = spec["cancel"]
        p["_ladder_note"] = [
            f"Legs are spread across the posted zone instead of taking one "
            f"market fill at the worst edge. {spec['why']}.",
            "The nearest edge carries TP1 and the runner; the far edge carries "
            "the last target leg.",
            "CONSEQUENCE: the whole signal becomes PENDING LIMIT ORDERS. If "
            "price never returns to the zone, nothing fills - which is what "
            "the operator's zone actually means, but it is a change from the "
            "old behaviour where every signal filled at market immediately.",
            f"ladder_cancel_pips {spec['cancel']} = one R at this channel's "
            f"median stop. Once price runs that far past the leg that filled, "
            f"the unfilled discount legs are deleted: a limit below a market "
            f"that has already left is not a discount, it is a pullback "
            f"waiting to fill you on a stop sized for a different entry.",
        ]
        laddered.append((ch["id"], ch["name"]))

    out = a.out or a.path
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"defaults.parser.default_sl_pips  = {FALLBACK_PIPS}")
    print(f"defaults.parser.pre_signal_sl_pips = {FALLBACK_PIPS}")
    print(f"\nremoved {len(removed)} per-channel override(s):")
    for cid, name, was in removed:
        print(f"   {cid}  {name:<26} was {was}")
    print(f"\nzone laddering enabled on {len(laddered)}:")
    for cid, name in laddered:
        print(f"   {cid}  {name}")
    print(f"\nleft on single-entry fill ({len(NO_LADDER)}):")
    for cid, why in NO_LADDER.items():
        print(f"   {cid}  {why}")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
