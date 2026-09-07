"""
db/database.py
==============
SQLite database for signal bot.
Tables:
  - signals        : one row per parsed signal
  - positions      : one row per MT5 position opened from a signal
  - channel_stats  : daily equity snapshots per channel (for drawdown tracking)
  - trades_log     : closed trade audit trail
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "data/signals.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS signals (
                    signal_id       TEXT PRIMARY KEY,
                    channel_id      TEXT NOT NULL,
                    channel_name    TEXT,
                    message_id      INTEGER,
                    reply_to_id     INTEGER,
                    raw_text        TEXT,
                    symbol          TEXT,
                    direction       TEXT,
                    entry_type      TEXT,
                    entry_price     REAL,
                    stop_loss       REAL,
                    take_profits    TEXT,   -- JSON array
                    status          TEXT DEFAULT 'pending',
                    is_bare         INTEGER DEFAULT 0,  -- 1 = no SL/TP at open
                    bare_opened_at  TEXT,
                    created_at      TEXT NOT NULL,
                    notes           TEXT
                );

                CREATE TABLE IF NOT EXISTS positions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id       TEXT REFERENCES signals(signal_id),
                    channel_id      TEXT,
                    tp_index        INTEGER,
                    tp_price        REAL,
                    ticket          INTEGER UNIQUE,
                    lot_size        REAL,
                    entry_price     REAL,
                    stop_loss       REAL,
                    order_type      TEXT,
                    status          TEXT DEFAULT 'pending',
                    opened_at       TEXT,
                    closed_at       TEXT,
                    close_reason    TEXT,
                    pnl             REAL,
                    mfe             REAL,
                    mae             REAL
                );

                CREATE TABLE IF NOT EXISTS skipped_signals (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id  TEXT,
                    message_id  INTEGER,
                    intent      TEXT,
                    reason      TEXT,
                    at          TEXT
                );

                CREATE TABLE IF NOT EXISTS channel_stats (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id      TEXT NOT NULL,
                    date            TEXT NOT NULL,
                    starting_equity REAL,
                    current_equity  REAL,
                    drawdown_pct    REAL,
                    trades_count    INTEGER DEFAULT 0,
                    UNIQUE(channel_id, date)
                );

                CREATE TABLE IF NOT EXISTS channel_balances (
                    channel_id      TEXT PRIMARY KEY,
                    starting_balance REAL NOT NULL,
                    system_balance  REAL NOT NULL,
                    last_updated    TEXT NOT NULL,
                    last_pnl        REAL DEFAULT 0,
                    notes           TEXT
                );

                -- Small durable key/value for things that must survive a
                -- restart. Currently: which session the daily scorecard has
                -- already been sent for, so a restart cannot silently skip a
                -- day (2026-08-25: the process was stopped before 21:30 UTC
                -- and restarted after the next Asian open, and that session's
                -- report was never sent and never would be).
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_sig_channel
                    ON signals(channel_id, status);
                CREATE INDEX IF NOT EXISTS idx_sig_symbol
                    ON signals(symbol, status);
                CREATE INDEX IF NOT EXISTS idx_pos_ticket
                    ON positions(ticket);
                CREATE INDEX IF NOT EXISTS idx_pos_signal
                    ON positions(signal_id, status);
                CREATE INDEX IF NOT EXISTS idx_pos_channel
                    ON positions(channel_id, status);
            """)
            self._add_missing_columns(c)
        logger.info("[DB] Schema ready")

    # Columns added after the first release. CREATE TABLE IF NOT EXISTS does
    # nothing to a table that already exists, so a new column never reaches a
    # live database without this. Purely additive: an ALTER TABLE ADD COLUMN
    # with a default rewrites no rows and cannot lose data.
    _ADDED_COLUMNS = {
        "positions": [
            # How much of this ticket's realised P&L has already been moved
            # into channel_balances. The EA's get_deal_history returns the
            # CUMULATIVE profit of every closing deal on a position id, so a
            # partial close and the eventual full close both report the first
            # slice. Booking each reply whole counted that slice twice in the
            # ledger, and channel_balances is the risk-sizing base and the
            # drawdown denominator: phantom credit makes a losing channel look
            # solvent and sizes the next trade off money that does not exist.
            ("booked_pnl", "REAL DEFAULT 0"),
        ],
    }

    def _add_missing_columns(self, c: sqlite3.Connection):
        for table, cols in self._ADDED_COLUMNS.items():
            try:
                have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                continue
            if not have:
                continue
            for name, decl in cols:
                if name in have:
                    continue
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    logger.info("[DB] added %s.%s", table, name)
                except sqlite3.Error as e:
                    logger.error("[DB] could not add %s.%s: %s", table, name, e)

    # ── Signals ────────────────────────────────────────────────────────────────

    def save_signal(self, signal_id: str, channel_id: str, channel_name: str,
                    message_id: int, reply_to_id: Optional[int],
                    raw_text: str, symbol: str, direction: Optional[str],
                    entry_type: Optional[str], entry_price: Optional[float],
                    stop_loss: Optional[float], take_profits: list,
                    status: str = "pending", is_bare: bool = False,
                    notes: Optional[str] = None) -> str:
        now = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO signals
                (signal_id, channel_id, channel_name, message_id, reply_to_id,
                 raw_text, symbol, direction, entry_type, entry_price, stop_loss,
                 take_profits, status, is_bare, bare_opened_at, created_at, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (signal_id, channel_id, channel_name, message_id, reply_to_id,
                  raw_text, symbol, direction, entry_type, entry_price, stop_loss,
                  json.dumps(take_profits), status,
                  1 if is_bare else 0,
                  now if is_bare else None,
                  now,
                  # The parser's own notes - "SL synthesised at 70 pips",
                  # "stop treated as a typo", "zone collapsed to a single
                  # entry" - never reached this column, so the population of
                  # trades taken on an inferred stop could not be graded
                  # separately after the fact. That was the whole point of
                  # tagging them.
                  notes))
        return signal_id

    def get_signal(self, signal_id: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM signals WHERE signal_id=?",
                             (signal_id,)).fetchone()

    def get_signal_by_message(self, channel_id: str,
                               message_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM signals WHERE channel_id=? AND message_id=?",
                (channel_id, message_id)).fetchone()

    def get_open_signals(self, channel_id: Optional[str] = None,
                          symbol: Optional[str] = None) -> list:
        sql  = "SELECT * FROM signals WHERE status IN ('pending','open','partially_closed')"
        args = []
        if channel_id:
            sql += " AND channel_id=?"; args.append(channel_id)
        if symbol:
            sql += " AND symbol=?";     args.append(symbol)
        # NEWEST FIRST, explicitly. With no ORDER BY this returned index order,
        # so the executor's `all_open[:1]` for an unqualified "close it" picked
        # the channel's OLDEST open signal - closing a stale trade and leaving
        # the one the operator was talking about running, while reporting
        # success either way.
        sql += " ORDER BY created_at DESC, rowid DESC"
        with self._conn() as c:
            return c.execute(sql, args).fetchall()

    def get_bare_signals(self, channel_id: Optional[str] = None) -> list:
        sql  = "SELECT * FROM signals WHERE is_bare=1 AND status IN ('pending','open')"
        args = []
        if channel_id:
            sql += " AND channel_id=?"; args.append(channel_id)
        with self._conn() as c:
            return c.execute(sql, args).fetchall()

    def update_signal_status(self, signal_id: str, status: str, notes: str = ""):
        with self._conn() as c:
            c.execute("UPDATE signals SET status=?, notes=? WHERE signal_id=?",
                      (status, notes, signal_id))

    def upgrade_bare_signal(self, signal_id: str, stop_loss: float,
                             take_profits: list):
        """Mark a bare signal as upgraded when full signal arrives."""
        with self._conn() as c:
            c.execute("""
                UPDATE signals SET is_bare=0, stop_loss=?, take_profits=?,
                status='open', notes='upgraded from bare'
                WHERE signal_id=?
            """, (stop_loss, json.dumps(take_profits), signal_id))

    # ── Positions ──────────────────────────────────────────────────────────────

    def save_position(self, signal_id: str, channel_id: str,
                       tp_index: int, tp_price: float,
                       lot_size: float, stop_loss: float,
                       order_type: str, ticket: Optional[int] = None,
                       entry_price: Optional[float] = None,
                       status: str = "pending") -> int:
        now = datetime.utcnow().isoformat()
        with self._conn() as c:
            cur = c.execute("""
                INSERT INTO positions
                (signal_id, channel_id, tp_index, tp_price, ticket, lot_size,
                 entry_price, stop_loss, order_type, status, opened_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (signal_id, channel_id, tp_index, tp_price, ticket, lot_size,
                  entry_price, stop_loss, order_type, status,
                  now if ticket else None))
            return cur.lastrowid

    def update_position_opened(self, row_id: int, ticket: int, entry_price: float):
        with self._conn() as c:
            c.execute("""
                UPDATE positions SET ticket=?, entry_price=?, status='open',
                opened_at=? WHERE id=?
            """, (ticket, entry_price, datetime.utcnow().isoformat(), row_id))

    def update_position_closed(self, ticket: int, pnl: float,
                                reason: str = "closed",
                                closed_at: Optional[str] = None) -> bool:
        """closed_at defaults to NOW, but pass the BROKER's close time when the
        deal history supplies it. get_channel_day_pnl buckets on this column, so
        a stop hit at 23:59:58 and detected at 00:00:03 was landing in the next
        day - after the rollover had already un-halted the channel, so the
        breaker never saw the loss that should have tripped it.

        Returns True only if THIS call performed the transition. The update is
        conditional on the row not already being closed, so two writers racing
        on the same ticket cannot both believe they closed it. That race is
        real: bare_trade_watcher closes a position and books it, while the
        monitor is part-way through a cycle holding the row it read before the
        close. The monitor then sees the ticket missing from the live pool and
        books the same money a second time. Gating the ledger move on this
        return value makes the whole class of double-book impossible rather
        than patching each caller."""
        now = closed_at or datetime.utcnow().isoformat()
        with self._conn() as c:
            cur = c.execute("""
                UPDATE positions SET status='closed', closed_at=?,
                close_reason=?, pnl=? WHERE ticket=? AND status!='closed'
            """, (now, reason, pnl, ticket))
            changed = cur.rowcount > 0
            # Check if whole signal is closed
            row = c.execute(
                "SELECT signal_id FROM positions WHERE ticket=?",
                (ticket,)).fetchone()
            if row:
                self._maybe_close_signal(c, row["signal_id"])
        if not changed:
            logger.info("[DB] ticket=%s was already closed - ignoring a second "
                        "close (%s, %+.2f)", ticket, reason, pnl or 0.0)
        return changed

    def _maybe_close_signal(self, conn: sqlite3.Connection, signal_id: str):
        rows = conn.execute(
            "SELECT status FROM positions WHERE signal_id=?",
            (signal_id,)).fetchall()
        if all(r["status"] in ("closed", "cancelled") for r in rows):
            conn.execute(
                "UPDATE signals SET status='closed' WHERE signal_id=?",
                (signal_id,))

    def get_open_positions(self, signal_id: str) -> list:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM positions WHERE signal_id=? AND status='open'",
                (signal_id,)).fetchall()

    def book_realised(self, channel_id: str, pnl: float, why: str = ""):
        """Move a channel's ledger by a realised P&L, from anywhere.

        update_system_balance had exactly ONE caller - position_monitor's close
        handler - so four other paths that realise money never touched the
        ledger at all: the bare scale-out, the bare timeout, the executor's
        manual close and close_partial. channel_balances therefore drifted
        permanently above reality, and it is both the risk-sizing base
        (_get_working_balance) and the drawdown denominator on any day where
        channel_stats has no row yet. Route every realised P&L through here.
        """
        if not channel_id or not pnl:
            return
        # update_system_balance silently returns when the channel has no
        # channel_balances row, which is exactly how a ledger drifts without
        # anyone noticing. Create it from the config's starting balance if it
        # is missing, and say so.
        if not self.get_system_balance(str(channel_id)):
            logger.warning("[DB] no balance ledger for channel %s — creating one "
                           "before booking %+.2f (%s)", channel_id, pnl, why)
            self.init_system_balance(str(channel_id), 1000.0)
        self.update_system_balance(str(channel_id), float(pnl))

    def book_position_cumulative(self, ticket: int, channel_id: str,
                                 cumulative: float, why: str = "") -> float:
        """Book a ticket's realised P&L when the source reports a RUNNING TOTAL.

        The EA's get_deal_history sums DEAL_PROFIT over every closing deal that
        shares a position id, so the number it returns is cumulative, not the
        P&L of the close that just happened. Three callers were booking that
        number whole every time they asked:

            partial at +0.40R   ->  reply says  +3.00  ->  ledger +3.00
            remainder stops out ->  reply says  -5.00  ->  ledger -5.00
                                    (that -5.00 already contains the +3.00)

        so the ledger moved -2.00 while the account moved -5.00. With N
        partials on a ticket the first slice is counted N times. It is small
        while partials are rare - 22 of them last week, $1.11 of drift - but
        defaults.partial_profit now takes a slice off EVERY leg that reaches
        0.40R, which turns a rounding error into a systematic credit. And the
        error is always in the flattering direction, because a partial is only
        ever taken in profit.

        channel_balances is not a scoreboard. _get_working_balance sizes the
        next trade from it and the cumulative drawdown breaker divides by it,
        so phantom credit both oversizes the next position and delays the halt
        that should have stopped the channel.

        Returns the delta actually applied.
        """
        try:
            cumulative = float(cumulative)
        except (TypeError, ValueError):
            return 0.0
        with self._conn() as c:
            row = c.execute("SELECT booked_pnl FROM positions WHERE ticket=?",
                            (ticket,)).fetchone()
            already = float((row["booked_pnl"] if row else 0.0) or 0.0)
            delta = round(cumulative - already, 2)
            if row and delta:
                c.execute("UPDATE positions SET booked_pnl=? WHERE ticket=?",
                          (round(cumulative, 2), ticket))
        if not row:
            # No row to remember against. Booking the running total blind would
            # be the very double-count this exists to prevent, so refuse and be
            # loud rather than quietly corrupt the risk base.
            logger.error("[DB] no position row for ticket=%s - refusing to book "
                         "%+.2f (%s)", ticket, cumulative, why)
            return 0.0
        if delta:
            self.book_realised(channel_id, delta, why)
        return delta

    def mark_pnl_booked(self, ticket: int, delta: float):
        """Add to a ticket's booked total without moving the ledger.

        For the one path whose number is NOT a running total: an unconfirmed
        close books the last floating P&L directly, and this records that it
        happened so a late-arriving deal history cannot bank it a second time.
        """
        with self._conn() as c:
            c.execute("UPDATE positions SET booked_pnl="
                      "COALESCE(booked_pnl,0)+? WHERE ticket=?",
                      (round(float(delta), 2), ticket))

    def get_booked_pnl(self, ticket: int) -> float:
        """How much of this ticket has already reached the ledger.

        Non-zero means a slice of it has been taken and banked. Survives a
        restart, which the monitor's in-memory _partial_taken set does not.
        """
        with self._conn() as c:
            row = c.execute("SELECT booked_pnl FROM positions WHERE ticket=?",
                            (ticket,)).fetchone()
        return float((row["booked_pnl"] if row else 0.0) or 0.0)

    def update_position_lot(self, ticket: int, lot: float):
        """Record what is LEFT of a position after a partial close.

        Without this the bare scale-out measures every step against the
        original size, so "close half" takes the same absolute amount each
        time and the position is gone in two steps instead of four.
        """
        with self._conn() as c:
            c.execute("UPDATE positions SET lot_size=? WHERE ticket=?",
                      (lot, ticket))

    def update_position_sl(self, ticket: int, sl: float):
        """Record a stop that was moved on the broker side (runner trailing)."""
        with self._conn() as c:
            c.execute("UPDATE positions SET stop_loss=? WHERE ticket=?",
                      (sl, ticket))

    def log_skipped_signal(self, channel_id: str, message_id, intent: str,
                            reason: str):
        """Record a signal-shaped message the system decided not to trade.

        Without this the daily scorecard can only count what executed, so a
        channel posting twenty signals of which fifteen were refused looks
        identical to one that posted five clean ones. Grading a source needs
        both numbers.
        """
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO skipped_signals "
                    "(channel_id, message_id, intent, reason, at) "
                    "VALUES (?,?,?,?,?)",
                    (str(channel_id), message_id, str(intent)[:40],
                     str(reason)[:300], datetime.utcnow().isoformat()))
        except Exception:
            pass          # telemetry must never break execution

    def get_positions_by_signal(self, signal_id: str) -> list:
        """Every position for a signal, open or closed.

        get_open_positions() only returns the open ones, which cannot answer
        "have the other take-profit legs been reached yet" — the question the
        runner's trailing stop depends on.
        """
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM positions WHERE signal_id=?",
                (signal_id,)).fetchall()

    def get_position_by_ticket(self, ticket: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM positions WHERE ticket=?", (ticket,)).fetchone()

    def get_orphan_positions(self) -> list:
        """Rows we saved before sending, that never got a ticket back."""
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM positions WHERE ticket IS NULL "
                "AND status IN ('pending','open')").fetchall()

    def get_all_open_positions(self, channel_id: Optional[str] = None) -> list:
        sql = "SELECT * FROM positions WHERE status='open'"
        with self._conn() as c:
            if channel_id:
                return c.execute(sql + " AND channel_id=?", (channel_id,)).fetchall()
            return c.execute(sql).fetchall()

    def update_position_pnl(self, ticket: int, pnl: float,
                              mfe: float = 0.0, mae: float = 0.0):
        with self._conn() as c:
            c.execute("""
                UPDATE positions SET pnl=?, mfe=?, mae=? WHERE ticket=?
            """, (pnl, mfe, mae, ticket))

    # ── Channel stats ──────────────────────────────────────────────────────────

    def upsert_channel_equity(self, channel_id: str, equity: float,
                               starting_equity: float = 0.0):
        today = datetime.utcnow().date().isoformat()
        dd = 0.0
        if starting_equity > 0:
            dd = max(0.0, (starting_equity - equity) / starting_equity * 100)
        with self._conn() as c:
            c.execute("""
                INSERT INTO channel_stats (channel_id, date, starting_equity,
                    current_equity, drawdown_pct)
                VALUES (?,?,?,?,?)
                ON CONFLICT(channel_id, date) DO UPDATE SET
                    current_equity=excluded.current_equity,
                    drawdown_pct=excluded.drawdown_pct
            """, (channel_id, today, starting_equity, equity, dd))

    def get_today_stats(self, channel_id: str) -> Optional[sqlite3.Row]:
        today = datetime.utcnow().date().isoformat()
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM channel_stats WHERE channel_id=? AND date=?",
                (channel_id, today)).fetchone()

    def get_channel_day_pnl(self, channel_id: str,
                            day: Optional[str] = None) -> tuple[float, float]:
        """(realised_today, floating_now) in account currency, for ONE channel.

        realised: every position of this channel that CLOSED today.
        floating: the live P&L of every position of this channel still open,
                  whatever day it was opened. A trade carried overnight is
                  money at risk now, so a drawdown breaker must see it.

        Both are signed: losses are negative. This is the only honest input to
        a per-channel drawdown, because account equity moves with all 28
        channels at once and tells you nothing about any one of them.
        """
        day = day or datetime.utcnow().date().isoformat()
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) v FROM positions "
                "WHERE channel_id=? AND status='closed' AND pnl IS NOT NULL "
                "AND substr(closed_at, 1, 10)=?",
                (str(channel_id), day)).fetchone()
            realised = float(r["v"] or 0.0)
            r = c.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) v FROM positions "
                "WHERE channel_id=? AND status='open' AND pnl IS NOT NULL",
                (str(channel_id),)).fetchone()
            floating = float(r["v"] or 0.0)
        return realised, floating

    # ── System balance ledger ──────────────────────────────────────────────────

    def get_system_balance(self, channel_id: str) -> Optional[dict]:
        """Returns {'starting_balance', 'system_balance', 'last_updated'} or None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT starting_balance, system_balance, last_updated, last_pnl "
                "FROM channel_balances WHERE channel_id=?",
                (channel_id,)).fetchone()
            if not row:
                return None
            return {
                "starting_balance": float(row["starting_balance"]),
                "system_balance":   float(row["system_balance"]),
                "last_updated":     row["last_updated"],
                "last_pnl":         float(row["last_pnl"] or 0.0),
            }

    def init_system_balance(self, channel_id: str, starting_balance: float):
        """Insert if not exists. starting_balance also becomes initial system_balance."""
        now = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO channel_balances
                (channel_id, starting_balance, system_balance, last_updated, last_pnl)
                VALUES (?, ?, ?, ?, 0.0)
            """, (channel_id, float(starting_balance), float(starting_balance), now))

    def update_system_balance(self, channel_id: str, pnl_delta: float):
        """system_balance += pnl_delta, set last_updated=now, last_pnl=pnl_delta."""
        now = datetime.utcnow().isoformat()
        with self._conn() as c:
            row = c.execute(
                "SELECT system_balance FROM channel_balances WHERE channel_id=?",
                (channel_id,)).fetchone()
            if not row:
                # Nothing to update — channel didn't opt in to ledger
                return
            new_bal = float(row["system_balance"]) + float(pnl_delta)
            c.execute("""
                UPDATE channel_balances
                SET system_balance=?, last_updated=?, last_pnl=?
                WHERE channel_id=?
            """, (new_bal, now, float(pnl_delta), channel_id))

    def reset_system_balance(self, channel_id: str, new_starting: float):
        """Used when user manually resets via dashboard."""
        now = datetime.utcnow().isoformat()
        with self._conn() as c:
            c.execute("""
                INSERT INTO channel_balances
                (channel_id, starting_balance, system_balance, last_updated, last_pnl, notes)
                VALUES (?, ?, ?, ?, 0.0, 'manual reset')
                ON CONFLICT(channel_id) DO UPDATE SET
                    starting_balance=excluded.starting_balance,
                    system_balance=excluded.system_balance,
                    last_updated=excluded.last_updated,
                    last_pnl=0.0,
                    notes='manual reset'
            """, (channel_id, float(new_starting), float(new_starting), now))

    # ── Performance ────────────────────────────────────────────────────────────

    def get_channel_winrate(self, channel_id: str, days: int = 30) -> dict:
        """Returns {wins, losses, scratches, total, wr_pct} for a channel."""
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

        with self._conn() as c:
            # Wins = at least one position closed via TP for the signal
            rows = c.execute("""
                SELECT s.signal_id,
                       -- 'trail' is a trailing stop that closed IN PROFIT.
                       -- It is a win, not a stop-out; counting it as the
                       -- latter understated the win rate on every report.
                       SUM(CASE WHEN p.close_reason IN ('tp','trail') THEN 1 ELSE 0 END) AS tp_count,
                       SUM(CASE WHEN p.close_reason='sl' THEN 1 ELSE 0 END) AS sl_count,
                       SUM(CASE WHEN p.close_reason IN ('manual','be','upgraded_to_full')
                                THEN 1 ELSE 0 END) AS scratch_count
                FROM signals s
                LEFT JOIN positions p ON p.signal_id = s.signal_id
                WHERE s.channel_id = ?
                  AND s.created_at >= ?
                  AND s.status = 'closed'
                GROUP BY s.signal_id
            """, (channel_id, cutoff)).fetchall()

        wins = sum(1 for r in rows if (r["tp_count"] or 0) > 0)
        losses = sum(1 for r in rows if (r["tp_count"] or 0) == 0
                                         and (r["sl_count"] or 0) > 0)
        scratches = sum(1 for r in rows if (r["tp_count"] or 0) == 0
                                            and (r["sl_count"] or 0) == 0
                                            and (r["scratch_count"] or 0) > 0)
        total = wins + losses + scratches
        wr_pct = (wins / total * 100) if total > 0 else 0.0
        return {"wins": wins, "losses": losses, "scratches": scratches,
                "total": total, "wr_pct": wr_pct}

    # ── Lookups for idempotency (Task 6) ───────────────────────────────────────

    def get_signals_by_message(self, channel_id: str, message_id: int,
                                any_status: bool = False) -> list:
        """All signals (bare or full) for this exact message_id.

        any_status=True is what the entry idempotency check needs. Restricting
        to pending/open meant that once a signal had closed, an edit of the same
        message was no longer recognised as already traded and opened the whole
        position a second time.
        """
        sql = "SELECT * FROM signals WHERE channel_id=? AND message_id=?"
        if not any_status:
            sql += " AND status IN ('pending','open')"
        with self._conn() as c:
            return c.execute(sql, (channel_id, message_id)).fetchall()

    def get_meta(self, key: str) -> Optional[str]:
        try:
            with self._conn() as c:
                row = c.execute("SELECT value FROM meta WHERE key=?",
                                (key,)).fetchone()
            return row["value"] if row else None
        except Exception as e:
            logger.debug("[DB] get_meta(%s) failed: %s", key, e)
            return None

    def set_meta(self, key: str, value: str) -> None:
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, value, datetime.utcnow().isoformat()))
        except Exception as e:
            logger.debug("[DB] set_meta(%s) failed: %s", key, e)

    def find_live_duplicate(self, channel_id: str, direction: str,
                             stop_loss: float, take_profits: list,
                             exclude_message_id: Optional[int] = None
                             ) -> Optional[sqlite3.Row]:
        """A signal for this channel whose trade is STILL LIVE and identical.

        The in-memory fingerprint guard in the executor expires after
        DUPLICATE_WINDOW_SEC, which is right for a market order: if the
        operator reposts the same setup an hour later and the first trade is
        long finished, that is a genuine second trade.

        It is wrong for a pending order. Gold Hunter posted the same buy limit
        at 4643 twice on 2026-08-24, 49 minutes apart, and again at 4621.74
        eleven hours apart. Both got through the time window and placed a
        second identical order on top of one that had never filled.

        This asks the state question instead of the clock question: is there
        already an unfilled or open leg of exactly this trade? If so, a repost
        is a reminder, not a new instruction.
        """
        tps = json.dumps(sorted(float(t) for t in (take_profits or [])))
        with self._conn() as c:
            rows = c.execute(
                "SELECT s.* FROM signals s "
                "WHERE s.channel_id=? AND LOWER(s.direction)=LOWER(?) "
                "  AND s.is_bare=0 "
                "  AND (? IS NULL OR s.message_id <> ?) "
                # A row with no ticket never reached the broker - the bridge
                # timed out after the EA already had the command file, so we
                # never learned the ticket. Those rows stay 'pending' forever
                # and used to blackhole this exact stop/target combination for
                # the channel permanently: every future repost was refused as
                # "already live" against an order nothing was tracking.
                # Require a real ticket before a row can block a repost.
                "  AND EXISTS (SELECT 1 FROM positions p "
                "              WHERE p.signal_id = s.signal_id "
                "                AND p.ticket IS NOT NULL "
                "                AND p.status IN ('pending','open')) "
                "ORDER BY s.created_at DESC",
                (channel_id, direction, exclude_message_id,
                 exclude_message_id)).fetchall()
        for r in rows:
            if stop_loss is not None and r["stop_loss"] is not None:
                if abs(float(r["stop_loss"]) - float(stop_loss)) > 1e-6:
                    continue
            try:
                prev = json.dumps(sorted(float(t) for t in
                                         json.loads(r["take_profits"] or "[]")))
            except Exception:
                continue
            if prev == tps:
                return r
        return None

    def find_mirror_signals(self, channel_id: str, direction: str,
                             stop_loss: float, take_profits: list,
                             within_sec: int = 900) -> list:
        """The same trade posted by OTHER channels in the last `within_sec`.

        Several of the enabled channels relay the same desk. On 2026-08-24
        three pairs went through within minutes of each other: Wall Street Ben
        and Gold Layer Scalper both posted buy limit 4652 (42 seconds apart),
        Gold Layer Scalper and Jason Noah both posted buy limit 4658, and both
        posted the 4631.7 sell.

        This deliberately does NOT block anything. The point of the measurement
        week is to find out which relay is fastest and cleanest, and suppressing
        the copies would throw that data away. It exists so the operator is told
        that one desk's opinion is being taken N times, rather than discovering
        it from the drawdown.
        """
        cutoff = (datetime.utcnow() - timedelta(seconds=int(within_sec))).isoformat()
        tps = json.dumps(sorted(float(t) for t in (take_profits or [])))
        out = []
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM signals WHERE channel_id <> ? AND is_bare=0 "
                "AND LOWER(direction)=LOWER(?) AND created_at >= ? "
                "ORDER BY created_at DESC", (channel_id, direction, cutoff)
            ).fetchall()
        for r in rows:
            if stop_loss is not None and r["stop_loss"] is not None:
                if abs(float(r["stop_loss"]) - float(stop_loss)) > 1e-6:
                    continue
            try:
                prev = json.dumps(sorted(float(t) for t in
                                         json.loads(r["take_profits"] or "[]")))
            except Exception:
                continue
            if prev == tps:
                out.append(r)
        return out

    # ── Reporting ──────────────────────────────────────────────────────────────

    def get_trades_report(self, channel_id: Optional[str] = None,
                           from_date: Optional[str] = None,
                           to_date: Optional[str] = None) -> list:
        sql  = """
            SELECT p.*, s.channel_name, s.symbol, s.direction, s.raw_text,
                   s.created_at as signal_created
            FROM positions p
            JOIN signals s ON s.signal_id = p.signal_id
            WHERE p.status = 'closed'
        """
        args = []
        if channel_id:
            sql += " AND p.channel_id=?";     args.append(channel_id)
        if from_date:
            sql += " AND p.opened_at >= ?";   args.append(from_date)
        if to_date:
            sql += " AND p.opened_at <= ?";   args.append(to_date)
        sql += " ORDER BY p.opened_at DESC"
        with self._conn() as c:
            return c.execute(sql, args).fetchall()

    def get_channel_summary(self) -> list:
        with self._conn() as c:
            return c.execute("""
                SELECT
                    p.channel_id,
                    s.channel_name,
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN p.pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN p.pnl <= 0 THEN 1 ELSE 0 END) as losses,
                    ROUND(SUM(p.pnl), 2) as total_pnl,
                    ROUND(AVG(p.pnl), 2) as avg_pnl
                FROM positions p
                JOIN signals s ON s.signal_id = p.signal_id
                WHERE p.status = 'closed'
                GROUP BY p.channel_id
                ORDER BY total_pnl DESC
            """).fetchall()