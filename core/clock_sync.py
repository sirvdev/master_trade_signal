"""
core/clock_sync.py
==================
Estimates the offset between this machine's clock and Telegram's, and turns a
useless rejection ("message is 25581s old") into an actionable one ("your system
clock is 7h00m ahead of Telegram").

How the estimate works
----------------------
For a live push, `now - msg.date` is `clock_skew + delivery_lag`, and delivery
lag is always >= 0. So the MINIMUM of that difference over recent messages
converges on the skew from above. One sample already gives a usable upper bound.

Why this does NOT auto-correct by default
-----------------------------------------
A clock this wrong corrupts more than the freshness gate.
`signal_executor._handle_entry` picks the Asian/London/NY risk multiplier from
`hour_utc`, and every `opened_at` / `closed_at` in the database is written from
the same clock. Silently patching the parser would hide the fault while leaving
position sizing keyed to the wrong trading session. Fix the machine clock.

`CLOCK_SKEW_COMPENSATE=true` is available as a stopgap when you cannot, but it
only corrects staleness, not the session multipliers or the audit trail.
"""
from __future__ import annotations

import os
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def humanise(seconds: float) -> str:
    sign = "ahead of" if seconds >= 0 else "behind"
    s = abs(seconds)
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m {sign}"
    if m:
        return f"{m}m{sec:02d}s {sign}"
    return f"{s:.1f}s {sign}"


class ClockSkew:
    """Rolling estimate of (this machine's UTC) - (Telegram's UTC), in seconds."""

    def __init__(self, warn_sec: float = 120.0, horizon_sec: float = 3600.0,
                 compensate: Optional[bool] = None):
        self.warn_sec = float(warn_sec)
        self.horizon = timedelta(seconds=horizon_sec)
        self.compensate = (
            os.getenv("CLOCK_SKEW_COMPENSATE", "false").lower() == "true"
            if compensate is None else bool(compensate))
        self._samples: deque[tuple[datetime, float]] = deque(maxlen=512)

    # ── observation ──────────────────────────────────────────────────────────
    def observe(self, msg_dt: Optional[datetime],
                now: Optional[datetime] = None) -> None:
        if msg_dt is None:
            return
        ref = _utc(now or datetime.now(timezone.utc))
        self._samples.append((ref, (ref - _utc(msg_dt)).total_seconds()))
        cutoff = ref - self.horizon
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def estimate(self) -> Optional[float]:
        """Best estimate of the skew. None until at least one message is seen."""
        if not self._samples:
            return None
        return min(d for _, d in self._samples)

    @property
    def samples(self) -> int:
        return len(self._samples)

    @property
    def is_significant(self) -> bool:
        e = self.estimate
        return e is not None and abs(e) > self.warn_sec

    # ── reporting ────────────────────────────────────────────────────────────
    def diagnosis(self) -> Optional[str]:
        """One line naming the fault, or None when the clock looks fine."""
        e = self.estimate
        if e is None or not self.is_significant:
            return None

        parts = [f"system clock is {humanise(e)} Telegram "
                 f"(best of {self.samples} sample(s))"]

        # A whole number of hours is a timezone setting, not drift. Saying so
        # turns "the system is broken" into "check the timezone".
        hours = e / 3600.0
        nearest = round(hours)
        if abs(e) > 1800 and abs(e - nearest * 3600) < 300 and nearest != 0:
            parts.append(
                f"that is almost exactly {abs(nearest)} hour(s), so it is a "
                f"TIMEZONE configuration fault, not clock drift: the machine's "
                f"wall time and its configured time zone disagree by that much")
        else:
            parts.append("that is not a whole number of hours, so it looks like "
                         "genuine clock drift: enable time sync (w32tm /resync)")

        parts.append("every signal will be refused as stale until this is fixed")
        if self.compensate:
            parts.append("CLOCK_SKEW_COMPENSATE=true is ON: staleness is being "
                         "corrected, but session risk multipliers and database "
                         "timestamps are still wrong")
        else:
            parts.append("set CLOCK_SKEW_COMPENSATE=true only as a stopgap")
        return ". ".join(parts) + "."

    # ── use ──────────────────────────────────────────────────────────────────
    def adjusted_now(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """`now` moved into Telegram's frame, or None to leave it untouched.

        Returns None unless compensation is explicitly enabled AND the skew is
        big enough to matter, so normal sub-second delivery lag never shifts
        anything.
        """
        ref = _utc(now or datetime.now(timezone.utc))
        if not self.compensate or not self.is_significant:
            return None
        return ref - timedelta(seconds=self.estimate)
