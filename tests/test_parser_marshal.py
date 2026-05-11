"""
Smoke tests against real Marshal channel messages.
"""
import asyncio
import os
import sys

# Add project root to path so imports work when run from tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ai_parser import AIParser, _regex_classify, _normalise, _has_runner_tp

CASES = [
    # (text, expected_type)
    ("RiskY traDE ☠️\n👉🏾sell XAUUSD now \n🛑 SL 5142\n✅ TP 5081\n✅ TP 5076\n✅ TP open", "entry"),
    ("Move stop loss to breakeven we have news", "breakeven"),
    ("Move stop loss to break even", "breakeven"),
    ("Now move SL to Be and wait the fall gang", "breakeven"),
    ("🛑 SL 5097 \nSorry I was texting fast correct the stop loss", "sl_correction"),
    ("Looking buys on GOLD \nWait for my confirmation ready", "scouting"),
    ("Am looking buys on gold", "scouting"),
    ("You can close the last position now", "close"),
    ("Close last position", "close"),
    ("Okay I don't like it anymore close it", "close"),
    ("Not good anymore close with breakeven", "close"),
    ("✅ Our 5th TP successfully hit ✅", "tp_hit"),
    ("✅ Our first TP successfully hit ✅", "tp_hit"),
    ("Breakeven hit", "tp_hit"),
    ("Send your trades to @liontradingacademy", None),  # chatter — should not regex-match
    ("Good morning", None),
]


def test_regex_classifications():
    failures = []
    for text, expected in CASES:
        result = _regex_classify(text)
        actual = result["type"] if result else None
        if expected is None:
            if actual is not None:
                failures.append(f"  ❌ {text[:50]!r}\n     expected None, got {actual}")
        else:
            if actual != expected:
                failures.append(
                    f"  ❌ {text[:50]!r}\n     expected {expected}, got {actual}")
    if failures:
        print("\n".join(failures))
        raise AssertionError(f"{len(failures)} parser regression(s)")
    print(f"✅ {len(CASES)} parser cases pass")


RUNNER_CASES = [
    ("RiskY traDE ☠️\n👉🏾 buy XAUUSD now 4212\n🛑 SL 4198\n✅ TP 4217\n✅ TP open",
     "entry", True),  # entry with runner
    ("✅ TP 5081\n✅ TP open",
     "entry", True),
    ("✅ TP 5081\n✅ TP 5076",
     None, False),  # not a complete signal — no SL
]


def test_runner_detection():
    failures = []
    for text, _, expected_runner in RUNNER_CASES:
        actual = _has_runner_tp(text)
        if actual != expected_runner:
            failures.append(f"  ❌ {text[:50]!r}: expected runner={expected_runner}, got {actual}")
    if failures:
        raise AssertionError("\n".join(failures))
    print(f"✅ {len(RUNNER_CASES)} runner cases pass")


if __name__ == "__main__":
    test_regex_classifications()
    test_runner_detection()
