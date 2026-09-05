#!/usr/bin/env python3
"""
tools/preflight.py
==================
Go / no-go check to run before leaving the system unattended.

The clock check is the point of this tool. `w32tm /query /status` tells you what
Windows *believes*; it will report a healthy sync while the machine sits a day
out, because it reports on the last successful sync, not on whether the current
time is correct. This measures the offset against Telegram's own server clock,
which is the same clock the freshness gate compares every signal to.

    python tools/preflight.py

Read-only. It opens the Telegram session, asks MT5 for a symbol spec, and
touches the database and log directory. It places no orders.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config import load_config                                   # noqa: E402
from core.clock_sync import humanise                             # noqa: E402

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "
_problems: list[str] = []
_warnings: list[str] = []


def say(status, label, detail=""):
    print(f"[{status}] {label}" + (f"\n         {detail}" if detail else ""))
    if status == FAIL:
        _problems.append(label)
    elif status == WARN:
        _warnings.append(label)


# ── clock ─────────────────────────────────────────────────────────────────────

async def check_clock(cfg) -> None:
    from telethon import TelegramClient

    if not cfg.tg_api_id or not cfg.tg_api_hash:
        say(FAIL, "Telegram credentials", "TELEGRAM_API_ID / API_HASH missing")
        return

    async with TelegramClient(cfg.tg_session_name,
                              int(cfg.tg_api_id), cfg.tg_api_hash) as client:
        # 1. Exact: MTProto carries the server's clock in its message ids, and
        #    Telethon keeps the delta. Private attribute, so guarded.
        offset = None
        try:
            offset = float(client._sender._state.time_offset)
        except Exception:
            pass
        if offset is not None:
            if abs(offset) <= 2:
                say(OK, f"System clock vs Telegram: {offset:+.0f}s")
            elif abs(offset) <= 60:
                say(WARN, f"System clock is {humanise(offset)} Telegram",
                    "Under a minute. Signals will still pass, but fix it.")
            else:
                say(FAIL, f"System clock is {humanise(offset)} Telegram",
                    "Signals will be refused as stale. Run w32tm /resync "
                    "and re-run this check.")
        else:
            say(WARN, "Could not read Telegram's clock offset directly",
                "Falling back to the newest message across your channels.")

        # 2. Corroborate against real message timestamps. A live channel's
        #    newest post should be minutes old, not hours.
        #
        #    This loop used to `continue` past any channel it could not read,
        #    which hid the more important failure: events.NewMessage(chats=id)
        #    accepts an id it cannot resolve and then never fires, so an
        #    unreadable channel is indistinguishable from a quiet one and just
        #    never trades. Report every one of them by name.
        newest, newest_ch = None, ""
        unreadable, quiet = [], []
        for ch in [c for c in cfg.channels if c.enabled]:
            try:
                seen = None
                async for m in client.iter_messages(int(ch.id), limit=1):
                    seen = m.date
                    if m.date and (newest is None or m.date > newest):
                        newest, newest_ch = m.date, ch.name
                if seen is None:
                    quiet.append(f"{ch.name} (no messages at all)")
                else:
                    age = (datetime.now(timezone.utc) - seen).total_seconds()
                    if age > 48 * 3600:
                        quiet.append(f"{ch.name} (last post {age / 86400:.0f}d ago)")
            except Exception as e:
                unreadable.append(f"{ch.name} ({ch.id}): {type(e).__name__}")
        if unreadable:
            say(FAIL, f"{len(unreadable)} enabled channel(s) cannot be read",
                "; ".join(unreadable) + ". The handler registers but never "
                "fires, so these will look silent and will never trade. Check "
                "the account is still a member and the id is right.")
        else:
            say(OK, f"All {len([c for c in cfg.channels if c.enabled])} enabled "
                    f"channel(s) are readable")
        if quiet:
            say(WARN, f"{len(quiet)} channel(s) have been quiet for a while",
                "; ".join(quiet) + ". Not an error, but they will produce no "
                "scorecard rows.")
        if newest is None:
            say(WARN, "No channel messages readable", "Cannot corroborate the clock.")
        else:
            age = (datetime.now(timezone.utc) - newest).total_seconds()
            detail = f"newest post is from {newest_ch}, {humanise(age)} now"
            if age < -60:
                say(FAIL, "Newest message is in the FUTURE", detail)
            elif age > 6 * 3600:
                say(WARN, "Newest message is hours old", detail
                    + ". Either the clock is wrong or the channels are quiet.")
            else:
                say(OK, "Message timestamps agree with the system clock", detail)


# ── MT5 ───────────────────────────────────────────────────────────────────────

async def check_mt5(cfg) -> None:
    from bridge.mt5_bridge import MT5FileBridge

    bridge = MT5FileBridge(cfg.mt5_session_prefix, cfg.mt5_demo_mode)
    if not await bridge.connect():
        say(FAIL, "MT5 bridge", "EA not responding. Is it attached to a chart?")
        return
    say(OK, f"MT5 bridge connected (prefix {bridge.session_prefix})")

    eq = await bridge.get_equity()
    say(OK if eq else FAIL, f"Account equity: {eq}",
        "" if eq else "authenticate returned nothing")

    contract_env = float(os.getenv("CONTRACT_SIZE", "100.0"))
    seen = set()
    for ch in [c for c in cfg.channels if c.enabled]:
        if ch.symbol in seen:
            continue
        seen.add(ch.symbol)
        fn = getattr(bridge, "get_symbol_info", None)
        spec = await fn(ch.symbol) if fn else None
        if not spec or spec.get("status") != "success":
            say(WARN, f"{ch.symbol}: broker spec unavailable",
                "EA older than v2.505. CONTRACT_SIZE stays unverified.")
            continue
        cs = float(spec.get("contract_size") or 0)
        if cs and abs(cs - contract_env) > 1e-6:
            say(FAIL, f"{ch.symbol}: CONTRACT_SIZE mismatch",
                f"broker says {cs:g}, .env says {contract_env:g}. "
                f"Every lot is {cs / contract_env:.3g}x off.")
        else:
            say(OK, f"{ch.symbol}: contract size {cs:g} matches .env")
        px = await bridge.get_price(ch.symbol, "buy")
        say(OK if px else FAIL, f"{ch.symbol}: price {px}",
            "" if px else "no quote returned")

    # Partial close and stop orders both need a recent EA.
    ea = os.path.join(os.path.dirname(__file__), "..", "PythonFileBridge.mq5.txt")
    if os.path.exists(ea):
        src = open(ea, encoding="utf-8", errors="replace").read()
        say(OK if "PositionClosePartial" in src else FAIL,
            "EA source supports partial close",
            "" if "PositionClosePartial" in src
            else "Recompile PythonFileBridge v2.503+ or every partial "
                 "close will flatten the whole position.")
        say(OK if "set_trailing" in src else WARN,
            "EA source supports trailing",
            "" if "set_trailing" in src else "v2.504+ needed for EA-side trailing.")
    await bridge.disconnect()


# ── local state ───────────────────────────────────────────────────────────────

def check_local(cfg) -> None:
    from db.database import Database
    try:
        db = Database(cfg.db_path)
        n = len(db.get_all_open_positions())
        say(OK, f"Database writable ({cfg.db_path})", f"{n} position(s) open")
        if n:
            say(WARN, f"{n} position(s) already open",
                "A clean week is easier to grade from a flat book.")
    except Exception as e:
        say(FAIL, "Database", str(e))

    try:
        p = os.path.join(cfg.log_dir, ".preflight")
        os.makedirs(cfg.log_dir, exist_ok=True)
        open(p, "w").close(); os.remove(p)
        say(OK, f"Log directory writable ({cfg.log_dir})")
    except Exception as e:
        say(FAIL, "Log directory", str(e))

    en = [c for c in cfg.channels if c.enabled]
    say(OK if en else FAIL, f"{len(en)} channel(s) enabled",
        ", ".join(c.name for c in en) if en else "Nothing will trade.")

    risk = sum(c.risk_pct for c in en)
    syms = {c.symbol for c in en}
    if len(syms) == 1 and risk > 30:
        say(WARN, f"Combined risk {risk:.0f}% on one instrument",
            f"{len(en)} channels all trading {next(iter(syms))}. risk_pct is "
            f"per signal per channel, and they overlap.")

    hb = os.getenv("TELEGRAM_HEARTBEAT_MINUTES", "15")
    say(OK, f"Heartbeat: {'disabled' if hb in ('0', '') else hb + ' min'}")
    from core import daily_report as dr
    say(OK, f"Daily scorecard: "
            f"{'enabled at ' + dr.REPORT_AT_UTC + ' UTC' if dr.REPORT_ENABLED else 'disabled'}",
        f"session {dr.ASIAN_OPEN_UTC:02d}:00 → {dr.NY_CLOSE_UTC:02d}:00 UTC")


async def main() -> int:
    print("=" * 68)
    print("  Preflight  " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"))
    print("=" * 68)
    cfg = load_config()
    print("\n-- clock --")
    try:
        await check_clock(cfg)
    except Exception as e:
        say(FAIL, "Clock check crashed", str(e))
    print("\n-- MT5 --")
    try:
        await check_mt5(cfg)
    except Exception as e:
        say(FAIL, "MT5 check crashed", str(e))
    print("\n-- local --")
    check_local(cfg)

    print("\n" + "=" * 68)
    if _problems:
        print(f"  NOT READY — {len(_problems)} blocking problem(s):")
        for p in _problems:
            print(f"    - {p}")
    else:
        print("  READY" + (f" — {len(_warnings)} warning(s), none blocking"
                           if _warnings else ""))
    print("=" * 68)
    return 1 if _problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
