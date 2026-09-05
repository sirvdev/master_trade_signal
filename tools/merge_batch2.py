#!/usr/bin/env python3
"""
tools/merge_batch2.py
=====================
Adds the eleven channels exported on 2026-08-30 and puts every channel on the
same measurement footing for the coming week: $1000 notional book, 10% risk,
30% drawdown, enabled. Lion Trading Academy is left exactly as it is, on
instruction, because it is the one channel already graded and running on its
own settings.

MERGE, NEVER RESET. The live file is read, mutated in place, and written back.
Existing per-channel parser blocks - including GTMO's assemble_window_sec=500,
which was hand-set - are carried through untouched. The only fields this script
writes on an existing channel are starting_balance, risk_pct, drawdown_pct and
enabled.

Every parser number for the new eleven comes from replaying that channel's own
export through the deterministic parser. Nothing here is a guess; the numbers
that produced each setting are recorded in the block that carries it.

Usage:  python tools/merge_batch2.py <channels.json> [-o out.json]
"""
import argparse
import json
import sys

KEEP_AS_IS = "-1001799443074"          # Lion Trading Academy (Marshal)
TARGET = {"starting_balance": 1000.0, "risk_pct": 10.0,
          "drawdown_pct": 30.0, "enabled": True}

# ── The eleven ────────────────────────────────────────────────────────────────
# n = signals the deterministic parser reads from the export
# t = of those, how many pass every gate and would actually trade
NEW = [
    {
        "id": "-1001449395038",
        "name": "Mr William FX",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "bare zone call, then the full ticket about a minute later",
            "example": ("XAUUSD SELL ZONE 4200\n\n"
                        "-- ~57s later --\n"
                        "XAU/USD SELL Zone 4200 OR 4403\nTP 4197\nTP 4194\nTP 4192\n"
                        "TP 4191\nTP 4188\nTP 4180\nSTOP Loss 4210"),
            "max_tps": 7,
            "max_sl_distance": 25.0,
            "min_sl_distance": 2.0,
            "max_entry_deviation": 5.0,
            "max_limit_distance": 15.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "default_sl_pips": 70,
            "_measured": [
                "1427 messages, 294 signals, 145 tradeable before the fallback.",
                "Stop distance from the protective edge: p10 7.0, p50 7.0,",
                "p90 8.0, p97 23.0. max_sl_distance 25 keeps the whole real",
                "distribution and refuses the typos above it.",
                "146 signals carry no stop. 138 of those are followed by the",
                "full ticket within 30 minutes, median 57 seconds, p90 115s.",
                "That is a pre-signal habit, not a paywall: default_sl_pips 70",
                "opens the bare call on a $7.00 synthetic stop and",
                "_upgrade_bare_trades attaches the real stop and targets when",
                "the ticket lands. Set it to 0 to go back to refusing."
            ],
            "_risk_notes": [
                "Writes zones as 'SELL Zone 4200 OR 4403'. The OR is a second,",
                "far-away idea, not the other edge of one zone, and the parser",
                "reads it as a $203-wide zone. max_limit_distance 15 stops the",
                "resulting order from resting hundreds of dollars off market.",
                "Markdown sometimes splits the stop mid-number ('STOP Loss",
                "42**10' -> 42, 'STOP Loss 437' on a 4148 buy). Three cases in",
                "the corpus; all three are refused, none is mis-sized.",
                "20 message texts are byte-identical to John Wick",
                "(-1003778011534). Partial mirror; both are kept on purpose so",
                "they can be graded separately."
            ],
            "ai_hint": [
                "'SELL Zone 4200 OR 4403' offers two alternative zones. Take",
                "the one nearer the market, never a zone spanning both.",
                "A bare 'XAUUSD SELL ZONE 4200' with no stop and no targets is",
                "a pre-signal; the full ticket follows within about a minute."
            ]
        }
    },
    {
        "id": "-1001754612869",
        "name": "FX4Team",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "#GOLD / BUY <price> / TP...x5 / SL... - single entry, no zone",
            "example": ("#GOLD\nBUY 4563.00\nTP...4568.00\nTP...4573.00\nTP...4580.00\n"
                        "TP...4587.00\nTP...4597.00\nSL...4550.00"),
            "max_tps": 7,
            "max_sl_distance": 22.0,
            "min_sl_distance": 2.0,
            "max_entry_deviation": 6.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "_measured": [
                "1496 messages, 277 signals, 256 tradeable - 92%, the highest",
                "yield in this batch. Stop distance p10 8.0, p50 11.0, p90 15.0,",
                "p97 16.0. Almost no tail; max_sl_distance 22 is generous.",
                "Every entry is a single price, never a zone.",
                "Only 19 signals lack a stop and 12 of those are silver."
            ],
            "_risk_notes": [
                "ALSO POSTS #XAGUSD. Silver trades near 75, which is outside",
                "price_min, so those signals are refused by range and again by",
                "refuse_foreign_symbol. Verified across the export: no silver",
                "signal reaches the executor. Left on the default ai_fallback",
                "because a 75.5 entry is nowhere near the gold sanity band."
            ],
            "ai_hint": [
                "The dot separator ('TP...4568.00') is decoration.",
                "#XAGUSD is silver on a different contract. Return no trade."
            ]
        }
    },
    {
        "id": "-1001810326498",
        "name": "Mr Zack FX",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "'Trade Idea Buy Gold Entry Point <p> / Stop Loss / Take Profit 1..4'",
            "example": ("XAUUSD Trade Analysis Setup\nTrade Idea Buy Gold Entry Point 4466\n"
                        "Stop Loss  4456\nTake Profit  1  4470\nTake Profit  2  4474\n"
                        "Take Profit  3  4478\nTake Profit  4  4482"),
            "max_tps": 6,
            "max_sl_distance": 30.0,
            "min_sl_distance": 2.0,
            "max_entry_deviation": 6.0,
            "max_limit_distance": 15.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "_measured": [
                "1244 messages, 275 signals, 226 tradeable.",
                "Stop distance p10 10.0, p50 10.0, p90 17.0, p97 39.0.",
                "The tail above 30 is operator typos ('Entry Point 4573 / Stop",
                "Loss 4463' - he meant 4563). max_sl_distance 30 covers the",
                "real distribution and refuses those.",
                "52 signals used a second template, 'XAUUSD BUY NOW ( 4597) /",
                "TARGET 1 ( 4602) / ...'. Until 2026-08-30 the opening bracket",
                "was missing from the TP separator class, so all 52 parsed with",
                "a stop and zero targets and were then dropped by the executor",
                "with 'All TPs passed'. Fixed; all 52 now read three targets."
            ],
            "_risk_notes": [
                "Mixes genuine bare calls ('Gold Sell Now 4921') with review",
                "prose ('The Focus Was On Two Levels: 5055 And 5031'). 28 of",
                "the 41 stop-less signals are never followed by a ticket, so",
                "default_sl_pips is deliberately NOT set here: it would open",
                "trades on his commentary."
            ],
            "ai_hint": [
                "Targets appear as 'Take Profit 1 4470' or 'TARGET 1 ( 4602)'.",
                "Posts marked Analysis, Review or Focus discuss past levels.",
                "Return no trade for those."
            ]
        }
    },
    {
        "id": "-1001825393261",
        "name": "Analysis Lab",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "bare 'GOLD BUY NOW <p> <p>' then 'GOLD BUY ZONE 4562/68' + TP ladder + Stop Loss",
            "example": ("GOLD BUY NOW 4565  4560\n\n-- ~51s later --\n"
                        "GOLD BUY ZONE 4562/68\nTP 4572\nTP 4576\nTP 4580\nTP 4583\n"
                        "TP 4586\nTP 4589\nTP 4596\nStop Loss 4554"),
            "max_tps": 7,
            "max_sl_distance": 22.0,
            "min_sl_distance": 2.0,
            "max_entry_deviation": 8.0,
            "max_limit_distance": 15.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "default_sl_pips": 100,
            "_measured": [
                "941 messages, 191 signals, 122 tradeable before the fallback.",
                "Stop distance p10 8.0, p50 10.0, p90 13.0, p97 21.0.",
                "Zone width p50 5.0, p90 8.0.",
                "63 signals carry no stop; 58 are followed by the full ticket,",
                "median 51 seconds, p90 127s. default_sl_pips 100 = $10.00,",
                "taken from his own median. Set to 0 to refuse instead."
            ],
            "_risk_notes": [
                "Shorthand zones: '4562/68' means 4562 to 4568 and parses",
                "correctly, but '4556//50' loses the second edge and reads as a",
                "single price. The trade still goes out with the near edge, so",
                "the effect is a slightly better entry, not a wrong one."
            ],
            "ai_hint": [
                "'GOLD BUY ZONE 4562/68' is a zone from 4562 to 4568.",
                "A bare 'GOLD BUY NOW 4565 4560' with no stop is a pre-signal."
            ]
        }
    },
    {
        "id": "-1002145295870",
        "name": "Green Pips Zone",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "'Entry -> <p> / Targets: / <bare prices> / SL -> <p>'",
            "example": ("XAUUSD (SELL)\nEntry → 4322\nTargets:\n4319\n4316\n4312\n"
                        "4307\n4302\n\nSL → 4332"),
            "max_tps": 6,
            "max_sl_distance": 20.0,
            "min_sl_distance": 2.0,
            "max_entry_deviation": 6.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "_measured": [
                "1431 messages, 261 signals. Before 2026-08-30 only 34 were",
                "tradeable; now 150.",
                "This channel writes every label with an arrow, and its target",
                "lines carry no TP token at all. The stop was on the screen in",
                "116 messages and unreadable in all of them, so 44% of the",
                "channel was refused for 'no stop loss'. Two contained parser",
                "changes fixed it: the arrow joined the separator class, and an",
                "unlabelled price list under a bare 'Targets:' header is now",
                "read when no TP token appears anywhere in the message.",
                "Replayed over all 21,738 exported messages both changes moved",
                "exactly 116 rows, every one of them this channel, every one to",
                "a correct-side stop and a correct-side ladder.",
                "Stop distance after the fix: p10 7.0, p50 10.0, p90 10.0,",
                "p97 13.0 - 99 of 116 sit at exactly $10.00."
            ],
            "_risk_notes": [
                "109 signals genuinely carry no stop and are not followed by a",
                "ticket. Those stay refused; default_sl_pips is not set."
            ],
            "ai_hint": [
                "The arrow is a separator: 'Entry → 4322' is entry 4322 and",
                "'SL → 4332' is the stop. The prices listed under 'Targets:'",
                "are take profits in order, nearest first."
            ]
        }
    },
    {
        "id": "-1002470316467",
        "name": "Orient Forex",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "SMC gameplan prose most days; a clean 'Gold buy zone / SL / TP1-3' ticket occasionally",
            "example": "Gold buy zone 4596-4591\nSL 4586\nTP1 4600\nTP2 4605\nTP3 4611",
            "max_tps": 3,
            "max_sl_distance": 12.0,
            "min_sl_distance": 3.0,
            "max_entry_deviation": 5.0,
            "max_limit_distance": 8.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "ai_fallback": False,
            "_measured": [
                "1091 messages, 173 parse as signals, only 26 are tradeable -",
                "roughly two clean tickets a week. EXPECT VERY LITTLE DATA.",
                "The 26 are remarkably uniform: a $5 zone, a $5 stop from the",
                "protective edge in all 27 measured cases, exactly three",
                "targets. max_sl_distance 12 and max_entry_deviation 5 encode",
                "that shape; anything outside it is not his ticket format."
            ],
            "_risk_notes": [
                "AI FALLBACK IS OFF, ON PURPOSE. Most of this channel is",
                "structured-analysis prose: 'potential sells at our OB zone at",
                "5101-5116', 'Medium risk buys at our M30 OB zone at",
                "4958-4946.2'. Those are gold-range numbers inside the sanity",
                "band, so ai_sanity_band_pct cannot catch them - an AI reading",
                "one returns a plausible, entirely fictional trade. The",
                "deterministic parser refuses them for having no stop, which is",
                "the correct outcome, and asking the AI would undo it.",
                "For the same reason default_sl_pips must stay unset here: a",
                "synthetic stop would turn every gameplan into a position.",
                "One gameplan produced a $1995-wide zone. max_limit_distance 8",
                "means such an order can never rest."
            ],
            "ai_hint": [
                "Only 'Gold buy zone <a>-<b> / SL / TP1 / TP2 / TP3' is a",
                "trade. Numbered gameplans, 'potential sells', 'OB zone',",
                "'FVG', 'if price breaks' are analysis. Return no trade."
            ]
        }
    },
    {
        "id": "-1003298640045",
        "name": "Isabelle FX",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "'XAUUSD BUY: 4178-4173 / SL: / TP 1 / TP 2' with a standing +20 pip breakeven rule",
            "example": ("XAUUSD BUY: 4178-4173\n\nSL: 4166\n\nTP 1: 4183\nTP 2: 4188\n\n"
                        "SL to BE whenever profit 20pips+"),
            "max_tps": 2,
            "max_sl_distance": 12.0,
            "min_sl_distance": 3.0,
            "max_entry_deviation": 6.0,
            "max_limit_distance": 12.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "_measured": [
                "909 messages, 72 signals, 50 tradeable. Low frequency.",
                "Stop distance is EXACTLY 7.0 on all 51 measured signals, with",
                "no tail at all. Zone width 5.0. Two targets, never more.",
                "The tight parameters here are not caution, they are her",
                "actual, unusually consistent format."
            ],
            "_risk_notes": [
                "The 21 stop-less signals are 'Market Forecast' and 'Current M5",
                "mapping' posts - zones she is watching, not tickets. 20 of the",
                "21 are never followed by a ticket, so default_sl_pips is NOT",
                "set: it would trade her forecasts."
            ],
            "_breakeven_note": [
                "She states her own rule on every signal: 'SL to BE whenever",
                "profit 20pips+'. breakeven.on_pips 20 below is her rule, not",
                "our guess. slack_pips and min_profit_pips stay at the defaults",
                "so the stop lands 5 pips on the losing side of entry rather",
                "than exactly on it."
            ],
            "ai_hint": [
                "'BUY: 4178-4173' is a zone. Exactly two targets.",
                "'Market Forecast' and 'Current M5 mapping' posts are zones",
                "under observation, not entries. Return no trade for those."
            ]
        },
        "breakeven": {
            "on_operator_message": True,
            "slack_pips": 5,
            "min_profit_pips": 12,
            "on_tp_index": 0,
            "on_pips": 20,
            "buffer_pips": 0
        }
    },
    {
        "id": "-1003331716434",
        "name": "Sabin Gold",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "'Ready Signal' -> bare 'Gold Sell Now' -> full zone ticket + TP ladder",
            "example": ("\U0001f514Ready Signal\n-- 3s --\nGold Sell Now\n-- 46s --\n"
                        "Gold Sell Now 4601.7 - 4606\n\nSL: 4614\n\nTP: 4600\nTP: 4598\n"
                        "TP: 4596\nTP: 4594\nTP: open"),
            "max_tps": 5,
            "max_sl_distance": 12.0,
            "min_sl_distance": 3.0,
            "max_entry_deviation": 5.0,
            "max_limit_distance": 12.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "min_confidence": 0.75,
            "assemble_window_sec": 300,
            "_measured": [
                "972 messages over ten days, 51 signals, ALL 51 TRADEABLE.",
                "The only channel in this batch that never once withholds a",
                "stop or breaks its own format.",
                "Stop distance is exactly 8.0 on all 51. Zone width 4.0-5.0.",
                "Four or five targets plus a 'TP: open' runner.",
                "Management traffic is heavy and all of it parses: 48",
                "pre-signals, 38 breakevens, 8 stop moves, 13 partial closes.",
                "Cadence measured from the export: 'Ready Signal' banner, the",
                "bare direction about 3 seconds later, the full ticket 46-75",
                "seconds after that."
            ],
            "_risk_notes": [
                "The bare 'Gold Sell Now' carries no digits, so it is read as a",
                "PRE_SIGNAL and opens on the 100-pip preflight stop; the ticket",
                "then upgrades it. This is the channel the preflight path was",
                "built for and it will exercise it several times a day.",
                "Operator posts 'Change SL to 4485', 'Set Breakeven!', 'SL to",
                "Entries!', 'Close bottom entries!' - all mapped intents.",
                "This export contains member conversation. Sender filtering",
                "should be turned on here once operator ids are known."
            ],
            "ai_hint": [
                "'TP: open' is a runner leg with no fixed target.",
                "'Gold Sell Now 4601.7 - 4606' is a zone.",
                "'Always take partials along the way' is standing advice, not",
                "an instruction to close now."
            ]
        }
    },
    {
        "id": "-1003778011534",
        "name": "John Wick FX",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "bare 'GOLD BUY NOW <p>' then '<p>__<p> LIMITES / 1 TARGET.. / STOP LOSS'",
            "example": ("GOLD BUY NOW 4816\n\n-- ~54s later --\n"
                        "GOLD BUY NOW  4816__4814\nLIMITES\n\n1 TARGET  4820\n"
                        "2 TARGET  4824\n3 TARGET  4828\n4 TARGET OPEN\n\nSTOP LOSS 4806"),
            "max_tps": 6,
            "max_sl_distance": 22.0,
            "min_sl_distance": 2.0,
            "max_entry_deviation": 8.0,
            "max_limit_distance": 15.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "default_sl_pips": 110,
            "_measured": [
                "1282 messages, 229 signals, 166 tradeable before the fallback.",
                "Stop distance p10 8.0, p50 11.0, p90 15.0.",
                "57 signals carry no stop; 50 are followed by the full ticket,",
                "median 54 seconds, p90 134s. default_sl_pips 110 = $11.00 from",
                "his own median. Set to 0 to refuse instead.",
                "11 of his signals used the parenthesised target format and,",
                "like Mr Zack's 52, parsed with a stop and no targets until the",
                "2026-08-30 separator fix. They now read four targets each."
            ],
            "_risk_notes": [
                "20 message texts are byte-identical to Mr William FX",
                "(-1001449395038). Partial mirror, both kept on purpose.",
                "'LIMITES' is his word for the zone, not a limit-order",
                "instruction. '4 TARGET OPEN' is a runner."
            ],
            "ai_hint": [
                "'4816__4814' is a zone from 4814 to 4816; the underscores are",
                "decoration. 'N TARGET' is take profit N. 'TARGET OPEN' is a",
                "runner with no fixed target."
            ]
        }
    },
    {
        "id": "-1003864453549",
        "name": "XAU Daily Signals",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "'XAUUSD sell <p> / TP x4 / SL <p>' - single entry, no zone",
            "example": "XAUUSD sell 4046\n\nTP 4043\nTP 4040\nTP 4037\nTP 4030\n\nSL 4056",
            "max_tps": 6,
            "max_sl_distance": 18.0,
            "min_sl_distance": 2.0,
            "max_entry_deviation": 6.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "_measured": [
                "405 messages, 64 signals, 59 tradeable - 92%.",
                "Stop distance p10 10.0, p50 10.0, p90 12.0, p97 14.0. Tight.",
                "Only two signals ever omit a stop, so no fallback is needed."
            ],
            "_risk_notes": [
                "MIXED FEED, but benign. Posts BTCUSD signals and #XAGUSD",
                "result reports. Verified against the whole export:",
                "  - the BTCUSD entry is refused (its prices fall outside",
                "    price_min/price_max, so no entry survives).",
                "  - every silver post is a STATUS_REPORT ('TP 2 HIT 70PIPS'),",
                "    and _handle_tp_hit is log-only, so a silver result cannot",
                "    touch a gold position.",
                "No gold trade in this export was contaminated by either."
            ],
            "ai_hint": [
                "Gold only. BTCUSD and #XAGUSD posts are a different",
                "instrument. Return no trade for them, including their result",
                "reports."
            ]
        }
    },
    {
        "id": "-1004335397917",
        "name": "Emily Pips",
        "symbol": "XAUUSDm",
        "parser": {
            "template": "bare 'GOLD SELL NOW <p>' then 'Price Open @ <p>_<p> / Take profit 1..4 / STOP LOSS @'",
            "example": ("GOLD SELL NOW 4097\n\n-- ~52s later --\nGOLD SELL NOW\n"
                        "Price Open @ 4097_4000\n\nTake profit 1 @ 4092\nTake profit 2 @ 4087\n"
                        "Take profit 3 @ 4082\nTake profit 4 @ 4076\n\nSTOP LOSS @ 4107"),
            "max_tps": 4,
            "max_sl_distance": 18.0,
            "min_sl_distance": 2.0,
            "max_entry_deviation": 8.0,
            "max_limit_distance": 15.0,
            "zone_fill": "worst",
            "requires_sl": True,
            "default_sl_pips": 110,
            "_measured": [
                "426 messages, 119 signals, 60 tradeable before the fallback.",
                "Stop distance p10 10.0, p50 11.0, p90 12.0, p97 13.0.",
                "58 signals carry no stop; 55 are followed by the full ticket,",
                "median 52 seconds, p90 97s - the tightest pre-signal cadence",
                "in the batch. default_sl_pips 110 = $11.00 from her own",
                "median. Set to 0 to refuse instead."
            ],
            "_risk_notes": [
                "The second price in 'Price Open @ 4097_4000' is sometimes a",
                "typo for 4090 and reads as a $97-wide zone. Also writes",
                "'SELL 4060 / 4163' where 4163 should be 4063.",
                "max_limit_distance 15 stops those from resting off market.",
                "'SL_ 4068' with a trailing underscore parses; the underscore",
                "is already in the stop separator class."
            ],
            "ai_hint": [
                "'Price Open @ 4097_4000' is a zone; the underscore is a",
                "separator. If the two prices are more than about $15 apart",
                "the second one is a typo - use the first."
            ]
        }
    },
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()

    cfg = json.load(open(a.path, encoding="utf-8"))
    chans = cfg["channels"]
    by_id = {c["id"]: c for c in chans}

    added, skipped, retuned = [], [], []

    for spec in NEW:
        if spec["id"] in by_id:
            skipped.append(spec["id"])
            continue
        row = {
            "id": spec["id"],
            "name": spec["name"],
            "symbol": spec["symbol"],
            "risk_pct": TARGET["risk_pct"],
            "drawdown_pct": TARGET["drawdown_pct"],
            "starting_balance": TARGET["starting_balance"],
            "balance_drift_pct": 5.0,
            "pre_ann_positions": 1,
            "asian_risk_mult": 1.0,
            "london_risk_mult": 1.0,
            "ny_risk_mult": 1.0,
            "enabled": True,
            "parser": spec["parser"],
        }
        if "breakeven" in spec:
            row["breakeven"] = spec["breakeven"]
        chans.append(row)
        added.append(spec["id"])

    for c in chans:
        if c["id"] == KEEP_AS_IS:
            continue
        before = {k: c.get(k) for k in TARGET}
        c.update(TARGET)
        if before != TARGET:
            retuned.append((c["id"], c["name"], before))

    out = a.out or a.path
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"added   {len(added)}: {', '.join(added) or '-'}")
    print(f"already present {len(skipped)}: {', '.join(skipped) or '-'}")
    print(f"re-tuned {len(retuned)} channel(s) to "
          f"balance {TARGET['starting_balance']:.0f} / "
          f"risk {TARGET['risk_pct']:g}% / dd {TARGET['drawdown_pct']:g}% / enabled")
    for cid, name, before in retuned:
        print(f"   {cid}  {name:<34} was "
              f"bal={before['starting_balance']} risk={before['risk_pct']} "
              f"dd={before['drawdown_pct']} enabled={before['enabled']}")
    print(f"{KEEP_AS_IS} left untouched on instruction")
    print(f"-> {out}  ({len(chans)} channels)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
