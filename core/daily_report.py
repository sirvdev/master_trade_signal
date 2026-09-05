"""
core/daily_report.py
====================
Ranks the channels by what they actually made, once per trading day, and sends
the table to Telegram after the New York close and before Tokyo opens.

Why the window is Asian-open to NY-close and not midnight to midnight
---------------------------------------------------------------------
Gold trades nearly around the clock, so a calendar day cuts straight through
live positions and splits one trade's entry and exit across two reports. The
session day (00:00 UTC Tokyo open to 21:00 UTC New York close) is the window a
signal channel actually operates in, and the gap between 21:00 and 00:00 is the
only moment when almost nothing is open. Running the report inside that gap
means the numbers are about trades that are finished.

Everything is computed from realised, closed P&L. Floating positions are
reported separately as "still open" and are deliberately NOT counted: an open
position is an opinion, not a result.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Session boundaries, UTC. Tokyo opens 00:00 UTC, New York closes 21:00 UTC.
ASIAN_OPEN_UTC = int(os.getenv("SESSION_ASIAN_OPEN_UTC", "0"))
NY_CLOSE_UTC   = int(os.getenv("SESSION_NY_CLOSE_UTC", "21"))
# When to send. Must sit between the NY close and the next Asian open.
REPORT_AT_UTC  = os.getenv("DAILY_REPORT_AT_UTC", "21:30")
REPORT_ENABLED = os.getenv("DAILY_REPORT_ENABLED", "true").lower() == "true"


def session_window(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """The Asian-open .. NY-close window that has most recently ENDED."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    close = now.replace(hour=NY_CLOSE_UTC, minute=0, second=0, microsecond=0)
    if now < close:
        # Today's session has not closed yet: report on yesterday's.
        close -= timedelta(days=1)
    open_ = close.replace(hour=ASIAN_OPEN_UTC, minute=0, second=0, microsecond=0)
    if open_ >= close:
        open_ -= timedelta(days=1)
    return open_, close


def _seconds_until_report(now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    hh, _, mm = REPORT_AT_UTC.partition(":")
    target = now.replace(hour=int(hh), minute=int(mm or 0),
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class DailyReport:
    def __init__(self, db, config, notifier=None):
        self.db = db
        self.config = config
        self.notifier = notifier
        self._running = False

    # ── data ─────────────────────────────────────────────────────────────────

    def build(self, start: datetime, end: datetime) -> list[dict]:
        """One row per channel, ranked by realised net P&L."""
        rows = []
        for ch in (self.config.channels if self.config else []):
            r = self._channel_row(ch, start, end)
            if r:
                rows.append(r)
        rows.sort(key=lambda r: r["net"], reverse=True)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows

    def silent(self, rows: list[dict]) -> list:
        """Enabled channels that produced nothing at all this session.

        These used to be dropped from the report entirely, which is why
        2026-08-24 came out as "12 channel(s)" with sixteen enabled and no
        explanation of where the other four went. A channel that posted
        nothing is a fact about the channel, not an absence of data, and it is
        the only place a channel you can no longer read would ever show up.
        """
        have = {r["id"] for r in rows}
        return [ch for ch in (self.config.channels if self.config else [])
                if ch.enabled and ch.id not in have]

    def _channel_row(self, ch, start: datetime, end: datetime) -> Optional[dict]:
        s_iso, e_iso = start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")
        con = self.db._conn()
        try:
            with con as c:
                sig = c.execute(
                    "SELECT COUNT(*) n FROM signals WHERE channel_id=? "
                    "AND created_at>=? AND created_at<? AND is_bare=0",
                    (ch.id, s_iso, e_iso)).fetchone()
                pos = c.execute(
                    "SELECT COUNT(*) n, SUM(pnl>0) wins, SUM(pnl<0) losses, "
                    "       COALESCE(SUM(pnl),0) net, "
                    "       COALESCE(SUM(CASE WHEN pnl>0 THEN pnl END),0) gross_win, "
                    "       COALESCE(SUM(CASE WHEN pnl<0 THEN -pnl END),0) gross_loss, "
                    "       COALESCE(MIN(pnl),0) worst, COALESCE(MAX(pnl),0) best "
                    "FROM positions WHERE channel_id=? AND status='closed' "
                    "AND pnl IS NOT NULL AND closed_at>=? AND closed_at<?",
                    (ch.id, s_iso, e_iso)).fetchone()
                still_open = c.execute(
                    "SELECT COUNT(*) n FROM positions WHERE channel_id=? "
                    "AND status='open'", (ch.id,)).fetchone()
                skipped = c.execute(
                    "SELECT COUNT(*) n FROM skipped_signals WHERE channel_id=? "
                    "AND at>=? AND at<?", (ch.id, s_iso, e_iso)).fetchone()
                # "8 skipped" on its own reads like a fault in the system. The
                # reason is what tells you whether to act: "message is 2806s
                # old" after a Telegram resync is the feed, "no stop loss" is
                # the operator, and "max deviation" is the market having
                # already left. Group them so the answer is in the report
                # instead of in a grep of system.log.
                # Grouped in Python, not in SQL: the prices and ages inside a
                # reason differ on every occurrence, so GROUP BY reason puts
                # "market 4669.56 is 8.44 from entry 4678.0" and
                # "market 4669.50 is 8.50 from entry 4678.0" in separate
                # buckets and the count never adds up. Normalise first.
                skip_why = c.execute(
                    "SELECT reason FROM skipped_signals "
                    "WHERE channel_id=? AND at>=? AND at<?",
                    (ch.id, s_iso, e_iso)).fetchall()
        except Exception as e:
            logger.error("[REPORT] query failed for %s: %s", ch.name, e)
            return None

        n_pos = int(pos["n"] or 0)
        n_sig = int(sig["n"] or 0)
        n_skip = int(skipped["n"] or 0) if skipped else 0
        if not (n_pos or n_sig or n_skip):
            return None

        wins, losses = int(pos["wins"] or 0), int(pos["losses"] or 0)
        gw, gl = float(pos["gross_win"] or 0), float(pos["gross_loss"] or 0)
        bal = self.db.get_system_balance(ch.id) or {}
        start_bal = float(bal.get("starting_balance") or ch.starting_balance or 0)
        sys_bal = float(bal.get("system_balance") or 0) or start_bal
        return {
            "name": ch.name, "id": ch.id, "symbol": ch.symbol,
            "signals": n_sig, "skipped": n_skip,
            "skip_why": _group_reasons(skip_why),
            "positions": n_pos, "wins": wins, "losses": losses,
            # Everything else closed at exactly zero: a breakeven stop, or a
            # partial that netted nothing. Without this "6 positions, W/L 0/0"
            # reads like a reporting bug rather than six flat exits.
            "flat": max(0, n_pos - wins - losses),
            "win_pct": (100.0 * wins / n_pos) if n_pos else 0.0,
            "net": float(pos["net"] or 0.0),
            "pf": (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0),
            "expectancy": (float(pos["net"] or 0.0) / n_pos) if n_pos else 0.0,
            "best": float(pos["best"] or 0.0), "worst": float(pos["worst"] or 0.0),
            "start_balance": start_bal, "system_balance": sys_bal,
            "growth_pct": (100.0 * (sys_bal - start_bal) / start_bal) if start_bal else 0.0,
            "still_open": int(still_open["n"] or 0),
            "risk_pct": ch.risk_pct, "halted": bool(ch.halted),
        }

    # ── rendering ────────────────────────────────────────────────────────────

    def render(self, rows: list[dict], start: datetime, end: datetime) -> str:
        quiet = self.silent(rows)
        n_en = len([c for c in (self.config.channels if self.config else [])
                    if c.enabled])
        head = (f"📊 <b>Daily channel scorecard</b>\n"
                f"<i>{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC "
                f"(Asian open → NY close)</i>\n"
                # Always account for every enabled channel. Printing only the
                # ones with rows is what made a 16-channel run report as
                # "12 channel(s)" with nothing to say about the other four.
                f"<i>{len(rows)} of {n_en} enabled channel(s) were active</i>\n")
        if not rows:
            return (head + "\nNo channel activity in this session."
                    + _quiet_block(quiet))

        net = sum(r["net"] for r in rows)
        pos = sum(r["positions"] for r in rows)
        sig = sum(r["signals"] for r in rows)
        parts = [head,
                 f"\n<b>Total</b>  {sig} signals · {pos} positions · "
                 f"net <code>{net:+.2f}</code>\n"]

        for r in rows:
            pf = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(r["rank"], f"{r['rank']}.")
            flag = " ⛔<i>halted</i>" if r["halted"] else ""
            parts.append(
                f"\n{medal} <b>{r['name']}</b>{flag}\n"
                f"<code>{r['id']}</code> · {r['symbol']} · risk {r['risk_pct']:g}%\n"
                f"signals <b>{r['signals']}</b>"
                + (f" (+{r['skipped']} skipped)" if r["skipped"] else "")
                + f" · positions {r['positions']}\n"
                f"W/L <b>{r['wins']}/{r['losses']}</b>"
                + (f"/{r['flat']}flat" if r["flat"] else "")
                + f" ({r['win_pct']:.0f}%) · "
                f"PF {pf} · exp <code>{r['expectancy']:+.2f}</code>\n"
                f"net <b><code>{r['net']:+.2f}</code></b> · "
                f"best <code>{r['best']:+.2f}</code> / "
                f"worst <code>{r['worst']:+.2f}</code>\n"
                f"balance <code>{r['system_balance']:.2f}</code> "
                f"from <code>{r['start_balance']:.2f}</code> "
                f"(<code>{r['growth_pct']:+.1f}%</code>)"
                + (f" · {r['still_open']} still open" if r["still_open"] else "")
                + ("".join(f"\n<i>skipped {n}x: {w}</i>"
                           for w, n in r.get("skip_why", []))))

        parts.append(_quiet_block(quiet))

        thin = [r for r in rows if r["positions"] < 10]
        if thin:
            parts.append(f"\n\n<i>{len(thin)} channel(s) closed fewer than 10 "
                         f"positions. One session is not evidence; judge on "
                         f"profit factor and expectancy across the week.</i>")
        return "".join(parts)

    # ── delivery ─────────────────────────────────────────────────────────────

    async def send_now(self, now: Optional[datetime] = None,
                        window: Optional[tuple] = None,
                        prefix: str = "") -> str:
        start, end = window or session_window(now)
        rows = self.build(start, end)
        text = prefix + self.render(rows, start, end)
        logger.info("[REPORT] daily scorecard %s..%s: %d channel(s)",
                    start, end, len(rows))
        for r in rows:
            logger.info("[REPORT] #%d %s net=%.2f pos=%d W/L=%d/%d bal=%.2f",
                        r["rank"], r["name"], r["net"], r["positions"],
                        r["wins"], r["losses"], r["system_balance"])
        sent = False
        if self.notifier:
            try:
                # Telegram caps a message at 4096 characters.
                for chunk in _split(text, 3900):
                    await self.notifier.send(chunk)
                sent = True
            except Exception as e:
                logger.error("[REPORT] send failed: %s", e)
        # Only mark a session reported once it actually went out. A send that
        # failed must be retried by the next startup, not written off.
        if sent or not self.notifier:
            self._mark_reported(end)
        return text

    # ── catch-up ─────────────────────────────────────────────────────────────

    _META_KEY = "daily_report_last_session_end"

    def _last_reported(self) -> Optional[str]:
        try:
            return self.db.get_meta(self._META_KEY)
        except Exception:
            return None

    def _mark_reported(self, end: datetime) -> None:
        try:
            self.db.set_meta(self._META_KEY, end.strftime("%Y-%m-%dT%H:%M:%S"))
        except Exception as e:
            logger.debug("[REPORT] could not record the sent session: %s", e)

    async def catch_up(self, now: Optional[datetime] = None) -> bool:
        """Send the last completed session's report if it never went out.

        The scheduler is a single very long asyncio.sleep to the next
        REPORT_AT_UTC. That is fine while the process runs, and useless the
        moment it does not: on 2026-08-25 the process was stopped before 21:30
        UTC and restarted after the next Asian open, so that session's report
        was skipped and there was no mechanism that would ever send it.

        Runs once at startup. Sends nothing when the session is already
        reported, and nothing when the session had no activity at all, so a
        fresh install does not open with an empty report for a day it was not
        running.
        """
        start, end = session_window(now)
        end_iso = end.strftime("%Y-%m-%dT%H:%M:%S")
        last = self._last_reported()
        if last and last >= end_iso:
            logger.info("[REPORT] session ending %s already reported", end_iso)
            return False
        rows = self.build(start, end)
        if not rows:
            logger.info("[REPORT] no activity in the session ending %s — "
                        "nothing to catch up", end_iso)
            self._mark_reported(end)
            return False
        logger.warning("[REPORT] the session ending %s was never reported "
                       "(last reported: %s) — sending it now",
                       end_iso, last or "never")
        await self.send_now(window=(start, end),
                            prefix=f"⏪ <b>Catch-up</b> — this session's report "
                                   f"was missed at {REPORT_AT_UTC} UTC "
                                   f"(the process was not running).\n\n")
        return True

    async def start(self):
        """Fire once per day at REPORT_AT_UTC, between NY close and Tokyo open."""
        self._running = True
        logger.info("[REPORT] daily scorecard scheduled for %s UTC "
                    "(session %02d:00 → %02d:00 UTC)",
                    REPORT_AT_UTC, ASIAN_OPEN_UTC, NY_CLOSE_UTC)
        try:
            await self.catch_up()
        except Exception as e:
            # A failed catch-up must never stop the scheduler starting: the
            # missed report is worth less than tonight's.
            logger.exception("[REPORT] catch-up failed: %s", e)
        while self._running:
            try:
                await asyncio.sleep(_seconds_until_report())
                if not self._running:
                    return
                await self.send_now()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[REPORT] daily scorecard failed: %s", e)
                await asyncio.sleep(300)

    def stop(self):
        self._running = False


def _quiet_block(quiet: list) -> str:
    """Name the enabled channels that produced nothing, rather than hiding them.

    A channel with no rows is either having a quiet day or is one the account
    can no longer read, and the scorecard cannot tell those apart. Naming them
    is what makes the second case findable at all; the startup reachability
    check in ChannelManager is what tells you which it is.
    """
    if not quiet:
        return ""
    names = "\n".join(f"  • {c.name} (<code>{c.id}</code>)" for c in quiet)
    return (f"\n\n😴 <b>No activity</b> ({len(quiet)})\n{names}\n"
            f"<i>No signals, no trades, nothing skipped. Either a quiet "
            f"session or a channel the account can no longer read — the "
            f"startup check says which.</i>")


def _group_reasons(rows, top: int = 3) -> list:
    """[(reason, count)] for the commonest skip reasons, numbers normalised."""
    counts: dict = {}
    for r in (rows or []):
        try:
            raw = r["reason"]
        except Exception:
            raw = r[0] if r else ""
        k = _shorten(raw)
        counts[k] = counts.get(k, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:top]


def _shorten(reason: str, limit: int = 64) -> str:
    """One readable phrase per skip reason, with the varying numbers removed.

    "message is 2806s old, max 600s" and "message is 2811s old, max 600s" are
    the same fault seen twice; grouping on the raw string would report them as
    two distinct reasons and bury the count that matters.
    """
    r = str(reason or "").strip()
    # No trailing \b: "2806s" has no word boundary between the digits and the
    # unit, so \b\d+\b leaves every one of those reasons distinct and the
    # grouping does nothing at all.
    r = re.sub(r"\b\d+(?:\.\d+)?", "N", r)
    r = re.sub(r"\s+", " ", r)
    return r[:limit] + ("..." if len(r) > limit else "")


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit:
            out.append(cur); cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out
