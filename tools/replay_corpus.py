#!/usr/bin/env python3
"""
tools/replay_corpus.py
======================
Replay exported Telegram channel history through the parser and report what the
live system WOULD have done. This is the regression harness: run it after every
parser or channels.json change and diff the summary.
Usage
-----
    python tools/replay_corpus.py --channels channels.json \
        --exports /path/to/channel_messages_*.json \
        --out replay_out
Outputs
-------
    replay_out/parsed.csv        one row per message, every intent
    replay_out/signals.csv       NEW_SIGNAL rows only, with validation verdict
    replay_out/rejected.csv      rows the executor would refuse, with reasons
    replay_out/review.txt        UNKNOWN + low-confidence rows for manual audit
Freshness and slippage checks are skipped in replay (there is no live price and
every message is historic). They are exercised live only.

NOTE: the export schema carries no sender_id, so replay runs with sender
verification disabled. Numbers here are therefore an UPPER bound on what the
live system will act on once operator_sender_ids is populated.
"""
from __future__ import annotations
import argparse
import csv
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.signal_parser import (  # noqa: E402
    Intent, SignalParser, SignalAssembler, normalize)


def load_channels(path: str) -> dict:
    cfg = json.load(open(path, encoding="utf-8"))
    dflt = cfg.get("defaults", {}).get("parser", {})
    for ch in cfg["channels"]:
        merged = dict(dflt)
        merged.update(ch.get("parser", {}))
        ch["parser"] = merged
    return cfg


def channel_id_from_path(p: str) -> str:
    return os.path.basename(p).rsplit("_", 1)[-1].replace(".json", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default="channels.json")
    ap.add_argument("--exports", nargs="+", required=True)
    ap.add_argument("--out", default="replay_out")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    cfg = load_channels(args.channels)
    by_id = {str(c["id"]): c for c in cfg["channels"]}
    files = []
    for pat in args.exports:
        files.extend(glob.glob(pat))
    files.sort()
    rows, counts = [], defaultdict(Counter)
    for f in files:
        cid = channel_id_from_path(f)
        ch = by_id.get(cid)
        if ch is None:
            print(f"!! no config for channel {cid} ({os.path.basename(f)})")
            continue
        parser = SignalParser(ch)
        # Replay in chronological order so the assembler sees real sequencing.
        msgs = sorted(json.load(open(f, encoding="utf-8")),
                      key=lambda m: m["date_utc"])
        asm = SignalAssembler(parser)
        for m in msgs:
            dt = datetime.strptime(m["date_utc"], "%Y-%m-%d %H:%M:%S")
            # now=dt disables the freshness gate for historic replay.
            res = asm.feed(m["text"], m["id"], dt, market_price=None, now=dt)
            if res is None:
                counts[cid]["BUFFERED"] += 1
                continue
            counts[cid][res.intent.value] += 1
            if res.rejected_reasons and res.intent == Intent.NEW_SIGNAL:
                counts[cid]["_signal_rejected"] += 1
            s = res.signal
            rows.append({
                "channel": cid, "name": ch["name"], "msg_id": m["id"],
                "date_utc": m["date_utc"], "intent": res.intent.value,
                "conf": round(res.confidence, 2),
                "executable": res.executable,
                "direction": s.direction if s else "",
                "order_type": s.order_type.value if s else "",
                "entry": s.entry if s else "",
                "zone": f"{s.entry_low}-{s.entry_high}" if s and s.is_zone else "",
                "sl": s.sl if s else "",
                "tps": "|".join(str(x) for x in s.tps) if s else "",
                "runner": s.has_open_runner if s else "",
                "fraction": res.action.fraction if res.action else "",
                "sl_price": res.action.price if res.action else "",
                "rejected": " ; ".join(res.rejected_reasons),
                "notes": " ; ".join(res.notes),
                "text": normalize(m["text"])[:300].replace("\n", " \\n "),
            })
    if not rows:
        print("no rows produced")
        return
    cols = list(rows[0].keys())

    def dump(name, sel):
        sub = [r for r in rows if sel(r)]
        with open(os.path.join(args.out, name), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(sub)
        return len(sub)

    n_all = dump("parsed.csv", lambda r: True)
    n_sig = dump("signals.csv", lambda r: r["intent"] == "NEW_SIGNAL")
    n_rej = dump("rejected.csv", lambda r: bool(r["rejected"]))
    with open(os.path.join(args.out, "review.txt"), "w", encoding="utf-8") as fh:
        for r in rows:
            if r["intent"] == "UNKNOWN" or (r["conf"] and float(r["conf"]) < 0.75
                                            and r["intent"] not in ("CHATTER",
                                                                    "STATUS_REPORT")):
                fh.write(f"[{r['channel']}|{r['msg_id']}|{r['intent']}|{r['conf']}] "
                         f"{r['text']}\n")
    # ── summary ───────────────────────────────────────────────────────────────
    print(f"\nparsed {n_all} messages -> {args.out}/  "
          f"({n_sig} NEW_SIGNAL, {n_rej} with rejections)\n")
    hdr = ["NEW_SIGNAL", "  ok", " rej", "CANCEL", "CLOSE", "PARTIAL", "BE",
           "SL@", "REPORT", "UNK", "CHAT", "BUFF"]
    print(f"{'channel':<16}{'name':<30}" + "".join(f"{h:>9}" for h in hdr))
    for cid, c in counts.items():
        nm = by_id[cid]["name"][:28]
        sig = c["NEW_SIGNAL"]
        rej = c["_signal_rejected"]
        vals = [sig, sig - rej, rej, c["CANCEL_PENDING"], c["CLOSE_ALL"],
                c["CLOSE_PARTIAL"], c["MOVE_SL_BE"], c["MOVE_SL_PRICE"],
                c["STATUS_REPORT"], c["UNKNOWN"], c["CHATTER"], c["BUFFERED"]]
        print(f"{cid:<16}{nm:<30}" + "".join(f"{v:>9}" for v in vals))
    # rejection reason histogram
    reasons = Counter()
    for r in rows:
        for x in r["rejected"].split(" ; "):
            if x:
                reasons[x.split(",")[0][:70]] += 1
    print("\ntop rejection reasons")
    for k, v in reasons.most_common(12):
        print(f"  {v:>5}  {k}")
    # Fragments still buffered when the corpus ended are trades that were never
    # completed. A large number here means assemble_window_sec is mis-tuned.
    print("\nNOTE: BUFF counts fragments held for multi-message assembly. A "
          "fragment that never completes is dropped, never traded.")


if __name__ == "__main__":
    main()
