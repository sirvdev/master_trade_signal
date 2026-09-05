#!/usr/bin/env python3
"""
tests/test_bare_runner_trail.py
===============================
A bare call ("gold sell now", no levels) opens one leg with a protective stop
and NO take profit. Until 2026-08-30 _trail_one returned immediately for such a
position:

    siblings = [legs of this signal that have a take profit]
    if not siblings:
        return                      # "no ladder: nothing to wait for"

So a bare trade was never trailed and never managed by anything except the
39-minute scale-out. It either ran to its 70-pip stop or sat there.

It now arms on DISTANCE instead: once the trade is RUNNER_TRAIL_ARM_R times its
own stop distance in profit (1.0 = one R), the EA's trailing engine takes the
stop over. That is deliberately not a fixed take profit at 1R, which would cap
every bare trade at exactly 1R and close the ones that were about to run.

Run with: python tests/test_bare_runner_trail.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

os.environ.setdefault("RUNNER_TRAIL_ARM_R", "1.0")
import core.position_monitor as PM        # noqa: E402
from config import ChannelConfig          # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f"  [{detail}]" if detail else ""))
        FAILS.append(label)


class Row(dict):
    def __getitem__(self, k):
        return self.get(k)


class Bridge:
    def __init__(self):
        self.armed = []
        self.mods = []

    async def set_trailing(self, ticket, dist, step):
        self.armed.append((ticket, dist, step))
        return {"ok": True}

    async def modify_position(self, ticket, sl=None, tp=None):
        self.mods.append((ticket, sl, tp))
        return {"ok": True}


class DB:
    def __init__(self, signal, positions):
        self._sig = signal
        self._pos = positions

    def get_signal(self, sid):
        return self._sig

    def get_positions_by_signal(self, sid):
        return self._pos

    def update_position_sl(self, *a, **k):
        pass


def run(direction, entry, sl, current, siblings=(), arm_r="1.0"):
    """Returns the list of (ticket, dist, step) handed to the EA."""
    os.environ["RUNNER_TRAIL_ARM_R"] = arm_r
    PM.RUNNER_TRAIL_ARM_R = float(arm_r)
    runner = Row(ticket=901, signal_id="BARE-1", channel_id="c", status="open",
                 tp_index=1, tp_price=0.0, entry_price=entry, stop_loss=sl,
                 mfe=0.0, mae=0.0)
    sig = Row(signal_id="BARE-1", channel_id="c", direction=direction,
              stop_loss=sl)
    ch = ChannelConfig(id="c", name="Bare Channel", symbol="XAUUSDm",
                       parser={"pip_value": 0.1}, starting_balance=1000.0)
    br = Bridge()
    mon = PM.PositionMonitor.__new__(PM.PositionMonitor)
    mon.bridge = br
    mon.db = DB(sig, [runner] + list(siblings))
    mon.config = types.SimpleNamespace(channels=[ch])
    mon.notifier = None
    mon._armed = set()
    mon._ea_trailing = True
    live = {901: {"ticket": 901, "current_price": current, "sl": sl}}
    asyncio.run(mon._trail_one(runner, live[901]))
    return br.armed


# A bare BUY at 4500 with a 70-pip stop: risk $7.00, so 1R is 4507.
print("a bare call with no ladder now trails")
check("nothing happens while the trade is only half an R in profit",
      run("buy", 4500.0, 4493.0, 4503.5) == [], "0.5R must not arm")
check("...and nothing happens while it is losing",
      run("buy", 4500.0, 4493.0, 4497.0) == [])
armed = run("buy", 4500.0, 4493.0, 4507.0)
check("at exactly 1R the EA takes the stop over",
      len(armed) == 1 and armed[0][0] == 901, str(armed))
check("...holding the trade's own stop distance behind price",
      armed and abs(armed[0][1] - 7.0) < 1e-9, str(armed))
check("beyond 1R it still arms", len(run("buy", 4500.0, 4493.0, 4520.0)) == 1)

print("\nthe same on the sell side")
check("a sell 0.5R in profit does not arm",
      run("sell", 4500.0, 4507.0, 4496.5) == [])
check("a sell at 1R does",
      len(run("sell", 4500.0, 4507.0, 4493.0)) == 1)

print("\nthe arm point is configurable")
check("RUNNER_TRAIL_ARM_R=2 holds off at 1R",
      run("buy", 4500.0, 4493.0, 4507.0, arm_r="2.0") == [])
check("...and arms at 2R",
      len(run("buy", 4500.0, 4493.0, 4514.0, arm_r="2.0")) == 1)

print("\na runner that DOES have a ladder is unaffected")
# The ladder rule is unchanged: wait for the sibling target legs to close,
# regardless of how far price has travelled.
open_sib = Row(ticket=902, signal_id="BARE-1", channel_id="c", status="open",
               tp_index=2, tp_price=4520.0, entry_price=4500.0,
               stop_loss=4493.0, mfe=0.0, mae=0.0)
check("distance alone does not arm a ladder runner",
      run("buy", 4500.0, 4493.0, 4560.0, siblings=[open_sib]) == [],
      "an open sibling leg must still hold it back")
closed_sib = Row(ticket=902, signal_id="BARE-1", channel_id="c",
                 status="closed", tp_index=2, tp_price=4520.0,
                 entry_price=4500.0, stop_loss=4493.0, mfe=0.0, mae=0.0)
check("...and it arms once the sibling closes",
      len(run("buy", 4500.0, 4493.0, 4560.0, siblings=[closed_sib])) == 1)

os.environ.pop("RUNNER_TRAIL_ARM_R", None)
print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
