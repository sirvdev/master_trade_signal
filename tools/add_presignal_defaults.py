#!/usr/bin/env python3
"""
tools/add_presignal_defaults.py
===============================
Adds the pre-signal and breakeven defaults to channels.json.

MERGE, NEVER REPLACE — same rule as tools/merge_new_channels.py. It touches
only `defaults.parser` and `defaults.breakeven`, only adds keys that are not
already there, and leaves every channel, `_schema` and `_notes` byte-identical.
Run it instead of copying a whole channels.json over your edited one.

    python tools/add_presignal_defaults.py            # dry run
    python tools/add_presignal_defaults.py --write
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "channels.json")

PARSER_ADD = {
    # Take a bare directional call ("Gold buy now", "Sell gold") as an
    # instruction. GTMO posted one on 2026-08-25 at 09:32:22Z and price moved
    # 100 pips inside a minute; Lion Trading's came 87 seconds before the full
    # signal. Median head start across the corpus is 69 seconds.
    "pre_signal": True,
    # Longer than this and it is prose, not an order.
    "pre_signal_max_chars": 64,
    # A bare "Sell gold" with no "now" scores 0.70. The channel's
    # min_confidence is tuned for full signals and would refuse it.
    "pre_signal_min_confidence": 0.65,
    # Protective stop for a blind entry. 100 pips = $10 on gold at pip_value
    # 0.1. Only 59% of bare calls are ever followed by levels, so the other
    # 41% need a bounded loss rather than an open-ended one.
    "pre_signal_sl_pips": 100,
}

BREAKEVEN_ADD = {
    "_why": [
        "2026-08-25, Lion Trading: the operator posted 'GOLD sell now' and the",
        "full signal 87 seconds later. Our fill was 4635.028, about five pips",
        "worse than his. When he said 'go risk free' his stop sat at his entry",
        "and ours five pips lower; price retraced through ours and not his.",
        "slack_pips puts the stop that far on the LOSING side of entry, because",
        "a stop exactly at entry is already a small loss once the spread is paid",
        "and it sits precisely where noise lives.",
        "min_profit_pips refuses to move the stop at all until the trade is",
        "actually in profit: below that, 'breakeven' means putting a stop on top",
        "of the market, which is an instant exit, not risk-free.",
        "Set both to 0 to restore the old exactly-at-entry behaviour.",
    ],
    "on_operator_message": True,
    "slack_pips": 5,
    "min_profit_pips": 12,
    "on_tp_index": 0,
    "on_pips": 0,
    "buffer_pips": 0,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--path", default=PATH)
    a = ap.parse_args()

    with open(a.path, encoding="utf-8") as fh:
        cfg = json.load(fh)

    added, present = [], []
    p = cfg.setdefault("defaults", {}).setdefault("parser", {})
    for k, v in PARSER_ADD.items():
        (added if k not in p else present).append(f"defaults.parser.{k}")
        p.setdefault(k, v)
    b = cfg["defaults"].setdefault("breakeven", {})
    for k, v in BREAKEVEN_ADD.items():
        (added if k not in b else present).append(f"defaults.breakeven.{k}")
        b.setdefault(k, v)

    print(f"base: {a.path}  ({len(cfg['channels'])} channels, untouched)")
    for k in added:
        print("  + " + k)
    for k in present:
        print("  = already set, left alone: " + k)
    if not added:
        print("\nnothing to do.")
        return 0
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
