"""Replay every exported message through the parser and dump a stable digest.

Run it once before a parser change and once after; diff the two files. Any
line that moves is a behaviour change you must be able to explain.
"""
import json, glob, os, sys, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.signal_parser import SignalParser, Intent

CORPUS = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/work/corpus_all"
OUT    = sys.argv[2] if len(sys.argv) > 2 else "/tmp/digest.txt"

CFG = {"id": "X", "symbol": "XAUUSD", "parser": {
    "requires_sl": True, "max_sl_distance": 60.0, "min_sl_distance": 1.0,
    "max_signal_age_sec": 10**9, "min_confidence": 0.7,
    "allow_bare_signals": True, "pre_signal": True}}

lines = []
for fp in sorted(glob.glob(os.path.join(CORPUS, "*.json"))):
    cid = os.path.basename(fp)[:-5]
    cfg = json.loads(json.dumps(CFG)); cfg["id"] = cid
    P = SignalParser(cfg)
    for m in json.load(open(fp, encoding="utf-8")):
        t = (m.get("text") or "").strip()
        if not t:
            continue
        r = P.parse(t, msg_id=m.get("id"))
        s = r.signal
        sig = "-" if not s else (
            f"{s.direction}|{s.entry}|{s.entry_low}|{s.entry_high}|{s.sl}|"
            f"{','.join(str(x) for x in (s.tps or []))}")
        rej = ";".join(sorted(r.rejected_reasons or []))
        lines.append(f"{cid}\t{m.get('id')}\t{r.intent.name}\t{r.confidence:.2f}\t{sig}\t{rej}")

open(OUT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print(f"{len(lines)} rows -> {OUT}")
