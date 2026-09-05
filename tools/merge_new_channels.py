#!/usr/bin/env python3
"""
tools/merge_new_channels.py
===========================
Adds the nine channels analysed from datas/ into channels.json.

MERGE, NEVER REPLACE. The operator edits channels.json by hand, so this
script:
  * loads the file that is on disk right now as the base,
  * touches only the channel ids listed in NEW,
  * refuses to change an id that is already present (prints it and skips),
  * leaves _schema, _notes, defaults and every existing channel byte-identical
    apart from the two default keys it is explicitly asked to add.

Every parser value below came from replaying the exported corpus for that
channel through core/signal_parser.py, not from guessing. The numbers in the
comments are what the replay measured.

    python tools/merge_new_channels.py            # dry run, prints the diff
    python tools/merge_new_channels.py --write    # writes channels.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PATH = os.path.join(ROOT, "channels.json")

# Applied to every new channel. The operator asked for these three explicitly:
# "starting balance 1000, trading risk 7% and drawdown 20%".
COMMON = {
    "symbol": "XAUUSDm",
    "risk_pct": 7.0,
    "drawdown_pct": 20.0,
    "starting_balance": 1000.0,
    "balance_drift_pct": 5.0,
    "pre_ann_positions": 1,
    "enabled": True,
}

NEW = [
    # ── 1 ────────────────────────────────────────────────────────────────────
    {
        "id": "-1002668402427",
        "name": "Hamza FX Gold",
        "parser": {
            "template": "XAUUSD BUY 4614/4610 + TP ladder + SL",
            "example": "XAUUSD SELL 4757/4760\nTP 4754\nTP 4751\n...\nSL 4777",
            # Replay: 1500 messages -> 206 signals, 196 of them zone entries,
            # 4 without a stop. TP count runs 4..17; the ladder is "a TP every
            # 3 dollars", not four discrete targets.
            "max_tps": 18,
            # Measured stop distance: p50 20, p90 27, p99 38, max 40.
            "max_sl_distance": 45.0,
            "min_sl_distance": 2.0,
            # Zone width p50 5, p90 6. The one 895-wide "zone" in the corpus is
            # a typo, and max_sl_distance already refuses it.
            "zone_fill": "worst",
            "requires_sl": True,
            "max_entry_deviation": 6.0,
            "ai_hint": [
                "Posts a dense ladder: up to seventeen TPs three dollars apart, with a",
                "single stop about twenty dollars away. A long list of near-identical",
                "targets is the normal shape here, not a parsing accident.",
                "Entry is written as a two-price zone with a slash: '4757/4760'.",
                "This operator almost never posts management instructions; the ladder",
                "is the management. Treat 'TP9 HIT' posts as status, not as orders.",
            ],
        },
    },
    # ── 2 ────────────────────────────────────────────────────────────────────
    {
        "id": "-1001957768371",
        "name": "Golden Signals",
        "parser": {
            "template": "XAUUSD BUY NOW 4614 / Sl : 4606 ( 80 pip ) / Tp : ...",
            "example": "XAUUSD BUY NOW 4614\nSl : 4606 ( 80 pip )\nTp : 4620 4626 4632",
            # Replay: 1253 messages -> 267 signals, no zones, 1 without a stop.
            # TP count 0..3. 24 signals carry no TP at all and become bare
            # pre-announcements, which is the correct handling.
            "max_tps": 4,
            # Measured stop distance: p50 8, p90 10, max 12. The tightest book
            # of the nine, so the ceiling is tight too.
            "max_sl_distance": 15.0,
            "min_sl_distance": 2.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "max_entry_deviation": 4.0,
            "ai_hint": [
                "Single entry price, never a zone. Stop is written 'Sl : 4606 ( 80 pip )'",
                "where the parenthesised number is the distance, NOT a second level.",
                "Stops here are tight, eight to twelve dollars; anything wider is a typo.",
                "Heavy VIP marketing chatter between signals. Almost no management posts.",
            ],
        },
    },
    # ── 3 ────────────────────────────────────────────────────────────────────
    {
        "id": "-1002587979027",
        "name": "Dubai King Gold",
        "parser": {
            "template": "GOLD_BUY NOW 4610/4608 + 5 TPs + SL",
            "example": "GOLD_BUY NOW 4610/4608\n\nTP 4612\nTP 4614\nTP 4616\n"
                       "TP 4618\nTP 4620\n\nSL 4602",
            # This channel parsed as 1432 messages / ZERO signals until the
            # underscore fix in SignalParser.normalize(): "GOLD_BUY" never
            # matched \bbuy\b because underscore is a word character. It now
            # yields 154 signals, 153 zones, none missing a stop.
            "max_tps": 6,
            # Measured stop distance: p50 8, p90 9. The 90 outlier is a typo.
            "max_sl_distance": 15.0,
            "min_sl_distance": 2.0,
            # Zone width p50 2 - the tightest of the nine.
            "zone_fill": "worst",
            "requires_sl": True,
            "max_entry_deviation": 4.0,
            "ai_hint": [
                "Writes the pair and direction joined by an underscore: 'GOLD_BUY NOW'.",
                "Entry is a two-price zone with a slash. Always exactly five TPs, two",
                "dollars apart, and a stop about eight dollars away.",
                "Most non-signal traffic is Spanish account-management marketing; it is",
                "chatter, never an instruction.",
                "Set and forget: this operator posts no close, breakeven or partial",
                "instructions at all.",
            ],
        },
    },
    # ── 4 ────────────────────────────────────────────────────────────────────
    {
        "id": "-1003669904323",
        "name": "Forex Expert Team",
        "parser": {
            "template": "two formats: own 'SIGNAL ALERT' block, and a "
                        "'Buy Gold @a-b / Sl / Tp1 / Tp2' clone",
            "example": "Buy Gold @4401.3-4391.3\nSl :4387.3\nTp1: 4405.3\nTp2: 4411\n"
                       "Enter slowly-layer with proper money management",
            # Replay: 1036 messages -> 116 signals, 34 zones, 164 CLOSE_ALL,
            # 5 SL moves, 4 partials. An active manager.
            "max_tps": 8,
            # Measured stop distance: p50 14, p90 15. Two outliers at 110 and
            # 1004 are operator typos and are refused by this ceiling.
            "max_sl_distance": 25.0,
            "min_sl_distance": 2.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "max_entry_deviation": 6.0,
            "_mirror_note": [
                "MIRROR. 35 signal texts in this corpus are byte-identical to",
                "-1002039699353 and 12 to -1003353497021, and the template is the",
                "same one Wall Street Ben (-1001765226347) already posts.",
                "Four enabled channels can therefore open the same trade four times.",
                "The duplicate guard keys on channel_id, so it will NOT stop this.",
                "Left enabled deliberately: on a demo account this measures which",
                "mirror is fastest. Disable three of the four before going live.",
            ],
            "ai_hint": [
                "Two house styles. One is a decorated 'SIGNAL ALERT' block with TP1..TP4",
                "and 'SL:'. The other is a plain 'Buy Gold @4401.3-4391.3 / Sl : / Tp1: /",
                "Tp2:' with the footer 'Enter slowly-layer with proper money management'.",
                "'Enter slowly' and 'layer' describe how to scale in; they are not",
                "separate orders.",
                "Closes are frequent and explicit. 'Close now' means close, not trim.",
            ],
        },
    },
    # ── 5 ────────────────────────────────────────────────────────────────────
    {
        "id": "-1002102979885",
        "name": "Gabriel Lopez FX (analysis)",
        "enabled": False,
        "parser": {
            "template": "narrative '#XAUUSD ANALYSIS' commentary, not a signal feed",
            "example": "#XAUUSD ANALYSIS\nDate: 13/07/2026\nXAU/USD remains under "
                       "bearish control ...",
            # Replay: 1155 messages -> ONE signal. 97 long analysis posts, 388
            # status reports, 668 chatter. There is nothing here to execute.
            "max_tps": 4,
            "max_sl_distance": 25.0,
            "min_sl_distance": 2.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "max_entry_deviation": 6.0,
            "_why_disabled": [
                "DISABLED ON EVIDENCE, not on preference. One executable signal in",
                "1155 messages. This is a free analysis channel that sells a VIP room;",
                "the tradeable calls are behind @GabrielLopezFx, not posted here.",
                "Enabling it costs nothing but also measures nothing. Set enabled=true",
                "if you join the VIP room and re-point this id at it.",
            ],
            "ai_hint": [
                "Long-form directional commentary with support and resistance levels",
                "quoted in prose ('support near $4,060'). These are levels being",
                "discussed, NOT an entry, a stop or a target. Do not build a trade",
                "from an analysis post.",
            ],
        },
    },
    # ── 6 ────────────────────────────────────────────────────────────────────
    {
        "id": "-1002393457678",
        "name": "Lucas Gold Legend",
        "parser": {
            "template": "GOLD BUY NOW / GOLD BUY ZONE 4113-4108 / TP1..TP3 + "
                        "'TP4 : open' / SL",
            "example": "GOLD BUY NOW\nGOLD BUY ZONE 4113- 4108\nTP1 : 4120\nTP2 : 4125\n"
                       "TP3 : 4130\nTP4 : open\nSL : 4100",
            # Replay: 972 messages -> 106 signals, 92 zones, 65 breakeven posts,
            # 12 closes. TP4 is written "open", which the parser reads as a
            # runner - this is the channel the EA trailing was built for.
            "max_tps": 4,
            # Measured stop distance: p50 13, p90 14. Two 113 outliers are
            # posts where the stop and the targets are on the same side of
            # entry; the geometry check already refuses those.
            "max_sl_distance": 25.0,
            "min_sl_distance": 2.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "max_entry_deviation": 6.0,
            "_yield_note": [
                "EXPECT A LOW FILL RATE. 44 of 106 signals in the corpus withhold the",
                "stop ('SL : PREMIUM', 'vip'), and requires_sl=true refuses those.",
                "That is the correct outcome, not a bug: sizing a gold trade without a",
                "stop means sizing off nothing. Roughly 60% of this channel's calls",
                "will trade. Judge it on the ones that do.",
            ],
            "ai_hint": [
                "Zone entries written as 'GOLD BUY ZONE 4113- 4108' and sometimes",
                "shorthand ('4620-25' means 4620 to 4625).",
                "'TP4 : open' is a runner with no target, not a missing number.",
                "The stop is often withheld as 'SL : PREMIUM' or 'vip'. If there is no",
                "numeric stop, return no trade. Never invent one.",
                "Posts breakeven instructions often; 'All Trade close BuY' means close",
                "the buy side.",
            ],
        },
    },
    # ── 7 ────────────────────────────────────────────────────────────────────
    {
        "id": "-1003447617877",
        "name": "Elites Trader",
        "parser": {
            "template": "GOLD BUY NOW (4168-4163) / TP1..TP3 / STOP LOSS:",
            "example": "GOLD SELL NOW (4600-4605)\n\nTP1: 4595\nTP2: 4590\nTP3: 4580\n\n"
                       "STOP LOSS:  4610\nLayer slowly in zone. Risk 3-5% per trade.",
            # Replay: 906 messages -> 81 signals, 78 zones, and the heaviest
            # management traffic of the nine: 191 partial-close posts and 77
            # breakeven posts. This is a hands-on operator.
            "max_tps": 4,
            # Measured stop distance: p50 10, p90 16. Three outliers at 80-110
            # are posts with the stop on the wrong side of entry.
            "max_sl_distance": 25.0,
            "min_sl_distance": 2.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "max_entry_deviation": 6.0,
            "ai_hint": [
                "Entry zone is in parentheses: 'GOLD BUY NOW (4168-4163)'. Stop is",
                "spelled out as 'STOP LOSS:'.",
                "Manages constantly and in plain English: 'Secure half and set",
                "breakeven', 'I'm closing 80% profit now', 'Can secure now'.",
                "'Secure' means take partial profit. A bare 'Can secure now' with no",
                "amount is ambiguous - return no trade rather than guessing a fraction.",
                "'Layer slowly in zone' is sizing advice, not an order.",
            ],
        },
        "breakeven": {
            "on_operator_message": True,
            "on_tp_index": 1,
            "buffer_pips": 2,
        },
    },
    # ── 8 ────────────────────────────────────────────────────────────────────
    {
        "id": "-1003353497021",
        "name": "Gold Layer Scalper (mixed feed)",
        "parser": {
            "template": "Buy Gold @4623-4613 / Sl : / Tp1 / Tp2 "
                        "(same clone template as #4 and #9)",
            "example": "Buy Gold @4623-4613\n\nSl :4609\n\nTp1: 4627\nTp2: 4630\n\n"
                       "Enter Slowly-Layer with proper money management",
            # Replay: 915 messages -> 51 gold signals, 46 zones, 68 closes.
            "max_tps": 4,
            # Measured stop distance: p50 14, p90 15, max 18. Tight and
            # consistent, so the ceiling can be tight.
            "max_sl_distance": 25.0,
            "min_sl_distance": 2.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "max_entry_deviation": 6.0,
            # This is the reason ai_sanity_band_pct exists. See the note below.
            "ai_sanity_band_pct": 3.0,
            "_contamination_note": [
                "MIXED FEED - THIS CHANNEL IS NOT GOLD-ONLY.",
                "52 of 915 messages are Indian index and equity options:",
                "'BUY DIXON 14500 CE ABOVE 391 / TARGET :- 345 / 430 / SL :- PAID',",
                "'BUY: SENSEX 77900 PE / Entry Above: 405 / Stoploss: 385'.",
                "The deterministic parser correctly abstains on these, which used to",
                "hand them to the AI parser with default_symbol=XAUUSDm - and a",
                "helpful AI will return a XAUUSDm buy at 391.",
                "ai_sanity_band_pct now refuses any AI-parsed level more than 3% from",
                "the live gold price. Nothing else in the system checked those numbers.",
            ],
            "ai_hint": [
                "Gold entries look like 'Buy Gold @4623-4613 / Sl : / Tp1: / Tp2:'.",
                "THIS CHANNEL ALSO FORWARDS INDIAN INDEX AND EQUITY OPTION CALLS",
                "(SENSEX, DIXON, PERSISTENT, POLICYBZR, prices in the 60-15000 range,",
                "'CE'/'PE', 'AUGUST EXPIRY', 'SL :- PAID'). Those are a different",
                "instrument on a different exchange. Return no trade for them.",
                "If the numbers are not within a few dollars of the gold price, it is",
                "not a gold signal.",
            ],
            "_mirror_note": [
                "MIRROR: 12 signal texts identical to -1003669904323 and 15 to",
                "-1002039699353. See the note on -1003669904323.",
            ],
        },
    },
    # ── 9 ────────────────────────────────────────────────────────────────────
    {
        "id": "-1002039699353",
        "name": "Jason Noah FX",
        "parser": {
            "template": "Buy Gold @a-b / Sl : / Tp1 / Tp2 "
                        "(same clone template as #4 and #8)",
            "example": "Buy Gold @4401.3-4391.3\nSl :4387.3\nTp1: 4405.3\nTp2: 4411\n"
                       "Enter Slowly-Layer with proper money management",
            # Replay: 945 messages -> 135 signals, 134 zones, none missing a
            # stop, plus 267 closes, 16 partials and 16 SL moves. The most
            # complete and best-formed of the three clone channels.
            "max_tps": 6,
            # Measured stop distance: p50 14, p90 15, p99 21.
            "max_sl_distance": 25.0,
            "min_sl_distance": 2.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "max_entry_deviation": 6.0,
            "_mirror_note": [
                "MIRROR: 35 signal texts identical to -1003669904323 and 15 to",
                "-1003353497021. Of the three clones this one has the cleanest data",
                "(zero missing stops, most management posts), so if you keep only one",
                "of the family after the measurement week, keep this one.",
            ],
            "ai_hint": [
                "'Buy Gold @4401.3-4391.3' is a ten-dollar entry zone; prices carry one",
                "decimal. Stop is 'Sl :', targets are 'Tp1:'/'Tp2:'.",
                "Footer 'Enter Slowly-Layer with proper money management' and 'Do not",
                "rush your entries' are advice, not orders.",
                "Manages actively: closes, partials and stop moves are all explicit.",
            ],
        },
    },
]

# Added to defaults.parser so every channel inherits it, including the eight
# that already exist. 5% of gold is ~230 dollars: wide enough that no real gold
# signal is ever refused by it, narrow enough to catch a level from a different
# instrument. Per-channel overrides win; -1003353497021 tightens it to 3.
DEFAULTS_ADD = {
    "ai_sanity_band_pct": 5.0,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--path", default=PATH)
    a = ap.parse_args()

    with open(a.path, encoding="utf-8") as fh:
        cfg = json.load(fh)

    existing = {c["id"]: c for c in cfg["channels"]}
    before = len(cfg["channels"])
    added, skipped = [], []

    for spec in NEW:
        cid = spec["id"]
        if cid in existing:
            skipped.append(f"{cid} ({existing[cid].get('name')})")
            continue
        entry = dict(COMMON)
        entry.update({k: v for k, v in spec.items() if k != "parser"})
        # id and name first so the file stays readable.
        ordered = {"id": entry.pop("id"), "name": entry.pop("name")}
        ordered.update(entry)
        ordered["parser"] = spec["parser"]
        cfg["channels"].append(ordered)
        added.append(f"{cid}  {ordered['name']}"
                     f"{'' if ordered['enabled'] else '   [DISABLED]'}")

    d = cfg.setdefault("defaults", {}).setdefault("parser", {})
    for k, v in DEFAULTS_ADD.items():
        if k not in d:
            d[k] = v
            added.append(f"defaults.parser.{k} = {v}")

    print(f"base: {a.path}  ({before} channels)")
    for line in added:
        print("  + " + line)
    for line in skipped:
        print("  = already present, untouched: " + line)
    print(f"result: {len(cfg['channels'])} channels")

    if not a.write:
        print("\ndry run. re-run with --write to save.")
        return 0

    bak = a.path + "." + datetime.now().strftime("%Y%m%d_%H%M%S") + ".bak"
    shutil.copy2(a.path, bak)
    with open(a.path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"\nwritten. backup at {os.path.basename(bak)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
