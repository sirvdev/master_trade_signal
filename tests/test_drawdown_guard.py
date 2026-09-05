#!/usr/bin/env python3
"""
tests/test_drawdown_guard.py
============================
The per-channel drawdown breaker and the account-level kill switch.

Every case here encodes a real defect. The headline one:

  dd = (start_eq - equity) / start_eq * 100

with start_eq = the channel's $1000 notional book and equity = the WHOLE
ACCOUNT's live equity. On the live account ($9,794.60) that is -879% for every
channel on every cycle, so no halt could ever fire - and none ever did, across
the system's entire log history. Below $700 of account equity it flips and
halts everything at once, winners included.

Run with: python tests/test_drawdown_guard.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config import AppConfig, ChannelConfig          # noqa: E402
from core.position_monitor import PositionMonitor    # noqa: E402
from db.database import Database                     # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f"  [{detail}]" if detail else ""))
        FAILS.append(label)


class Bridge:
    def __init__(self, equity):
        self.equity = equity

    async def get_equity(self):
        return self.equity

    async def get_positions(self):
        return []

    async def get_all_orders(self):
        return []


class Notifier:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


def build(equity, channels, tmp):
    db = Database(tmp)
    cfg = AppConfig.__new__(AppConfig)
    cfg.channels = channels
    m = PositionMonitor.__new__(PositionMonitor)
    m.bridge = Bridge(equity)
    m.db = db
    m.config = cfg
    m.notifier = Notifier()
    return m, db


def seed(db, channel_id, book, realised=0.0, floating=0.0):
    """Give a channel a book and a day's P&L, through the real DB API."""
    from datetime import datetime
    db.init_system_balance(channel_id, book)
    today = datetime.utcnow().date().isoformat()
    now = datetime.utcnow().isoformat()
    with db._conn() as c:
        if realised:
            c.execute(
                "INSERT INTO positions (signal_id, channel_id, tp_index, ticket,"
                " lot_size, entry_price, stop_loss, status, opened_at, closed_at,"
                " pnl) VALUES (?,?,?,?,?,?,?,'closed',?,?,?)",
                (f"S-{channel_id}-r", channel_id, 1, abs(hash(channel_id)) % 10**7,
                 0.01, 4000.0, 3990.0, now, today + "T12:00:00", realised))
        if floating:
            c.execute(
                "INSERT INTO positions (signal_id, channel_id, tp_index, ticket,"
                " lot_size, entry_price, stop_loss, status, opened_at, pnl)"
                " VALUES (?,?,?,?,?,?,?,'open',?,?)",
                (f"S-{channel_id}-f", channel_id, 2,
                 abs(hash(channel_id + "f")) % 10**7,
                 0.01, 4000.0, 3990.0, now, floating))


def ch(cid, name, book=1000.0, dd=30.0):
    return ChannelConfig(id=cid, name=name, symbol="XAUUSDm", risk_pct=10.0,
                         drawdown_pct=dd, starting_balance=book, enabled=True)


print("the old formula, reproduced")
# Not a behaviour test - a demonstration that the arithmetic could not work.
OLD = lambda start_eq, equity: (start_eq - equity) / start_eq * 100  # noqa: E731
check("on the live account it returns -879%, so no halt was reachable",
      round(OLD(1000.0, 9794.60), 0) == -879.0, str(round(OLD(1000.0, 9794.60), 1)))
check("and below $700 equity it exceeds 30% for EVERY channel at once",
      OLD(1000.0, 699.0) > 30.0, str(round(OLD(1000.0, 699.0), 1)))


print("\nper-channel drawdown now reads the channel's own book")
with tempfile.TemporaryDirectory() as d:
    os.environ["ACCOUNT_MAX_DAILY_LOSS_PCT"] = "0"        # isolate the per-channel path
    a, b, c_ = ch("-100", "Loser"), ch("-200", "Winner"), ch("-300", "Quiet")
    m, db = build(9794.60, [a, b, c_], os.path.join(d, "t.db"))
    seed(db, "-100", 1000.0, realised=-180.0, floating=-140.0)   # -32% of its book
    seed(db, "-200", 1000.0, realised=+250.0)
    seed(db, "-300", 1000.0)
    asyncio.run(m._update_drawdowns())

    check("a channel down 32% of its own book halts",
          a.halted and round(a.current_drawdown, 1) == 32.0,
          f"halted={a.halted} dd={a.current_drawdown:.1f}")
    check("a profitable channel on the same account does not",
          not b.halted and b.current_drawdown == 0.0)
    check("nor does an idle one", not c_.halted and c_.current_drawdown == 0.0)
    check("floating P&L counts toward the drawdown, not just realised",
          round(a.current_drawdown, 1) == 32.0,
          "180 realised alone would be 18% and would not have halted")
    check("exactly one halt notice was sent",
          sum(1 for s in m.notifier.sent if "Channel Halted" in s) == 1)
    check("the recorded equity is the channel's book, not account equity",
          abs(float(db.get_today_stats("-100")["current_equity"]) - 680.0) < 0.01,
          str(db.get_today_stats("-100")["current_equity"]))
    check("two channels no longer record identical equity",
          db.get_today_stats("-100")["current_equity"]
          != db.get_today_stats("-200")["current_equity"])

print("\na channel just under its limit keeps trading")
with tempfile.TemporaryDirectory() as d:
    os.environ["ACCOUNT_MAX_DAILY_LOSS_PCT"] = "0"
    a = ch("-100", "Nearly")
    m, db = build(9794.60, [a], os.path.join(d, "t.db"))
    seed(db, "-100", 1000.0, realised=-299.0)
    asyncio.run(m._update_drawdowns())
    check("-29.9% does not halt", not a.halted, f"dd={a.current_drawdown:.1f}")

print("\nstarting_balance = 0 still sizes and measures off live equity")
with tempfile.TemporaryDirectory() as d:
    os.environ["ACCOUNT_MAX_DAILY_LOSS_PCT"] = "0"
    a = ChannelConfig(id="-400", name="LiveEquity", symbol="XAUUSDm",
                      risk_pct=10.0, drawdown_pct=30.0, starting_balance=0.0,
                      enabled=True)
    m, db = build(10000.0, [a], os.path.join(d, "t.db"))
    with db._conn() as c:
        pass
    asyncio.run(m._update_drawdowns())
    check("the account is its book, so 0% drawdown with no trades",
          not a.halted and a.current_drawdown == 0.0)

print("\naccount-level kill switch")
with tempfile.TemporaryDirectory() as d:
    os.environ["ACCOUNT_MAX_DAILY_LOSS_PCT"] = "10"
    chans = [ch(f"-{i}00", f"Ch{i}") for i in range(1, 6)]
    m, db = build(9000.0, chans, os.path.join(d, "t.db"))
    # Five channels each down $200: no single one is near its own 30% limit,
    # but together they are $1000 = 10% of the account.
    for c2 in chans:
        seed(db, c2.id, 1000.0, realised=-200.0)
    asyncio.run(m._update_drawdowns())
    check("no individual channel breached its own 30% limit",
          all(round(c2.current_drawdown, 1) == 20.0 for c2 in chans),
          str([round(c2.current_drawdown, 1) for c2 in chans]))
    check("but the account guard halted all five",
          all(c2.halted for c2 in chans))
    check("and said so once, loudly",
          sum(1 for s in m.notifier.sent if "ACCOUNT GUARD" in s) == 1)
    check("the notice says open positions are not closed",
          any("NOT closed" in s for s in m.notifier.sent))

print("\nthe account guard can be switched off")
with tempfile.TemporaryDirectory() as d:
    os.environ["ACCOUNT_MAX_DAILY_LOSS_PCT"] = "0"
    chans = [ch(f"-{i}00", f"Ch{i}") for i in range(1, 6)]
    m, db = build(9000.0, chans, os.path.join(d, "t.db"))
    for c2 in chans:
        seed(db, c2.id, 1000.0, realised=-200.0)
    asyncio.run(m._update_drawdowns())
    check("0 disables it and restores unbounded behaviour",
          not any(c2.halted for c2 in chans))

print("\na halt is a decision about today, not forever")
with tempfile.TemporaryDirectory() as d:
    os.environ["ACCOUNT_MAX_DAILY_LOSS_PCT"] = "0"
    a = ch("-100", "Recovers")
    m, db = build(9794.60, [a], os.path.join(d, "t.db"))
    seed(db, "-100", 1000.0, realised=-400.0)
    asyncio.run(m._update_drawdowns())
    check("halts today", a.halted)
    m._halt_day = "1970-01-01"          # simulate the date rolling over
    with db._conn() as c:               # yesterday's damage is no longer today's
        c.execute("UPDATE positions SET closed_at='1970-01-01T12:00:00'")
        c.execute("DELETE FROM channel_stats")
    asyncio.run(m._update_drawdowns())
    check("and un-halts on the next UTC day",
          not a.halted, f"dd={a.current_drawdown:.1f}")

os.environ.pop("ACCOUNT_MAX_DAILY_LOSS_PCT", None)
print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
