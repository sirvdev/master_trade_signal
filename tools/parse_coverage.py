#!/usr/bin/env python3
"""tools/parse_coverage.py - what each channel's parser actually reads, and what it does not.

Replays every exported message under that channel's REAL config from
channels.json (only max_signal_age_sec is lifted, because replayed messages are
all stale). Splits every message the parser declines into a reason, so the
difference between "the operator did not post a stop" and "we cannot read the
stop he posted" is visible rather than assumed.
"""
import glob, json, os, re, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.signal_parser import Intent, SignalParser   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = json.load(open(os.path.join(ROOT, "channels.json"), encoding="utf-8"))
DFLT = RAW["defaults"]["parser"]
BY = {c["id"]: c for c in RAW["channels"]}
CORPUS = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/work/corpus_all"

# Does the raw text even claim to carry a stop? Distinguishes an operator
# omission from a parser failure.
HAS_SL = re.compile(r"(?i)\b(s\s*[./\-_]?\s*l|stop\s*-?\s*loss|stoploss)\b")

print(f"{'channel':<32} {'msgs':>5} {'trade':>6} {'mgmt':>5} {'pre':>4} "
      f"{'no-SL':>6} {'unread':>7} {'other':>6} {'chat':>6} {'unk':>4}")
print("-" * 100)
grand = Counter()
for fp in sorted(glob.glob(os.path.join(CORPUS, "*.json"))):
    cid = os.path.basename(fp)[:-5]
    if cid not in BY:
        continue
    ch = dict(BY[cid])
    m = dict(DFLT); m.update(ch.get("parser", {}))
    m["max_signal_age_sec"] = 10 ** 9
    ch["parser"] = m
    P = SignalParser(ch)
    c = Counter()
    for msg in json.load(open(fp, encoding="utf-8")):
        t = (msg.get("text") or "").strip()
        if not t:
            c["empty"] += 1; continue
        c["msgs"] += 1
        r = P.parse(t, msg_id=msg.get("id"))
        if r.intent is Intent.NEW_SIGNAL:
            if not r.rejected_reasons:
                c["trade"] += 1
            elif any("no stop loss" in x for x in r.rejected_reasons):
                # The important split: did he post one we failed to read?
                c["unread" if HAS_SL.search(t) else "no-SL"] += 1
            else:
                c["other"] += 1
        elif r.intent is Intent.PRE_SIGNAL:
            c["pre"] += 1
        elif r.intent in (Intent.CHATTER, Intent.STATUS_REPORT):
            c["chat"] += 1
        elif r.intent is Intent.UNKNOWN:
            c["unk"] += 1
        else:
            c["mgmt"] += 1
    grand.update(c)
    print(f"{BY[cid]['name']:<32} {c['msgs']:>5} {c['trade']:>6} {c['mgmt']:>5} "
          f"{c['pre']:>4} {c['no-SL']:>6} {c['unread']:>7} {c['other']:>6} "
          f"{c['chat']:>6} {c['unk']:>4}")
print("-" * 100)
print(f"{'TOTAL':<32} {grand['msgs']:>5} {grand['trade']:>6} {grand['mgmt']:>5} "
      f"{grand['pre']:>4} {grand['no-SL']:>6} {grand['unread']:>7} "
      f"{grand['other']:>6} {grand['chat']:>6} {grand['unk']:>4}")
print("\ntrade  = signal that passes every gate and would open a position")
print("mgmt   = breakeven / close / partial / SL move / cancel, all actionable")
print("pre    = bare directional call, opens on the preflight stop")
print("no-SL  = the operator posted no stop. Refused unless default_sl_pips is set.")
print("unread = a stop IS in the text and the parser could not read it. THIS IS THE BUG COLUMN.")
print("other  = refused for geometry: wrong-side stop, stop too far, targets on the wrong side")
