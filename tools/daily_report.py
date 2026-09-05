#!/usr/bin/env python3
"""
tools/daily_report.py
=====================
Print, or re-send, the channel scorecard for any session.

The scheduler sends one report a night and has no memory of the ones it
missed while the process was down. `core.daily_report.catch_up()` now covers
the most recent miss automatically at startup, but that only reaches back one
session. This reaches back as far as the database goes.

    python tools/daily_report.py                    # last completed session
    python tools/daily_report.py --date 2026-08-25  # that session, to stdout
    python tools/daily_report.py --date 2026-08-25 --send   # ...and to Telegram
    python tools/daily_report.py --week             # each of the last 7 sessions

Read-only unless --send is given. --send does NOT mark the session reported:
this is a manual re-read, and it must not suppress tonight's scheduled report
or the automatic catch-up.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config import load_config                                   # noqa: E402
from core import daily_report as DR                              # noqa: E402
from core.daily_report import DailyReport                        # noqa: E402
from db.database import Database                                 # noqa: E402


def window_for(date_str: str | None) -> tuple[datetime, datetime]:
    """The Asian-open .. NY-close window for a given calendar date (UTC)."""
    if not date_str:
        return DR.session_window()
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = d.replace(hour=DR.NY_CLOSE_UTC, minute=0, second=0, microsecond=0)
    start = end.replace(hour=DR.ASIAN_OPEN_UTC, minute=0, second=0, microsecond=0)
    if start >= end:
        start -= timedelta(days=1)
    return start, end


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="UTC date of the session, YYYY-MM-DD")
    ap.add_argument("--week", action="store_true",
                    help="the last 7 completed sessions")
    ap.add_argument("--send", action="store_true",
                    help="also deliver to Telegram")
    ap.add_argument("--html", action="store_true",
                    help="keep the Telegram markup instead of stripping it")
    a = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)

    notifier = None
    if a.send:
        from notifications.notifier import Notifier
        # Notifier() reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID itself; pass
        # the loaded config values so a .env override still wins.
        notifier = Notifier(cfg.tg_bot_token, cfg.tg_chat_id)

    rpt = DailyReport(db, cfg, notifier)

    if a.week:
        _, end = window_for(a.date)
        windows = [(end - timedelta(days=i + 1) - timedelta(
                        hours=DR.NY_CLOSE_UTC - DR.ASIAN_OPEN_UTC),
                    end - timedelta(days=i)) for i in range(7)][::-1]
    else:
        windows = [window_for(a.date)]

    for start, end in windows:
        rows = rpt.build(start, end)
        text = rpt.render(rows, start, end)
        print("\n" + "=" * 72)
        print(text if a.html else strip_html(text))
        if a.send and notifier:
            for chunk in DR._split(text, 3900):
                await notifier.send(chunk)
            print(f"\n[sent to Telegram: {start:%Y-%m-%d} session]")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
