"""
main.py
=======
Signal bot entry point.
Starts all async loops via asyncio.gather.
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from config import load_config
from bridge.mt5_bridge import MT5FileBridge
from core.ai_parser import AIParser
from core.signal_executor import SignalExecutor
from core.position_monitor import PositionMonitor
from core.bare_trade_watcher import BareTradeWatcher
from channels.channel_manager import ChannelManager
from core import daily_report
from core.daily_report import DailyReport
from db.database import Database
from notifications.notifier import Notifier


class _Tee:
    """Duplicate a stream to a file without swallowing it."""

    def __init__(self, stream, fh):
        self._stream, self._fh = stream, fh

    def write(self, data):
        try:
            self._stream.write(data)
        except Exception:
            pass
        try:
            self._fh.write(data)
            self._fh.flush()
        except Exception:
            pass
        return len(data)

    def flush(self):
        for t in (self._stream, self._fh):
            try:
                t.flush()
            except Exception:
                pass

    def isatty(self):
        return getattr(self._stream, "isatty", lambda: False)()

    def fileno(self):
        return self._stream.fileno()


def _tee_stdio(path: str):
    """Send stdout and stderr to the terminal AND to terminal.log."""
    try:
        fh = open(path, "a", encoding="utf-8", errors="replace")
        fh.write("\n" + "=" * 70 + "\n[terminal capture started "
                 + datetime.utcnow().isoformat() + "Z]\n" + "=" * 70 + "\n")
        fh.flush()
        sys.stdout = _Tee(sys.stdout, fh)
        sys.stderr = _Tee(sys.stderr, fh)
    except Exception as e:
        print("[MAIN] terminal capture unavailable: " + str(e))


def _setup_logging(log_dir: str):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    Path(f"{log_dir}/channels").mkdir(exist_ok=True)

    # UTC, and say so on every line.
    #
    # logging defaults to the machine's local time. This box runs at UTC-7
    # while Telegram message timestamps, the signals/positions tables and the
    # daily-report schedule are all UTC, so every log line read seven hours
    # earlier than the same event in Telegram. Reading a signal that the
    # channel posted at 13:24 next to a log line stamped 06:25 is how you end
    # up believing signals were missed when they were processed on time. One
    # clock for the whole system, and it is UTC.
    #
    # LOG_LOCAL_TIME=true restores the old behaviour if you prefer wall clock.
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S" +
                ("" if os.getenv("LOG_LOCAL_TIME", "").lower() == "true" else "Z"),
    )
    if os.getenv("LOG_LOCAL_TIME", "").lower() != "true":
        fmt.converter = time.gmtime
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{log_dir}/system.log", encoding="utf-8"),
        # Everything logged also lands here, alongside anything printed outside
        # the logging module (see _tee_stdio). system.log only ever contained
        # records that went through logging: a bare traceback or a library
        # writing to stderr went to the terminal and nowhere else, so it was
        # gone as soon as the window scrolled. terminal.log is the one to read
        # when something died.
        logging.FileHandler(f"{log_dir}/terminal.log", encoding="utf-8"),
    ]
    for h in handlers:
        h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        root.addHandler(h)

    _tee_stdio(f"{log_dir}/terminal.log")

    # Suppress noisy third-party loggers
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Trade-specific logger — writes to trades.log AND system.log
    trade_log = logging.getLogger("trades")
    trade_log.setLevel(logging.INFO)
    trade_handler = logging.FileHandler(f"{log_dir}/trades.log", encoding="utf-8")
    trade_handler.setFormatter(fmt)
    trade_log.addHandler(trade_handler)
    trade_log.propagate = True   # also goes to root → system.log + console


logger = logging.getLogger(__name__)


async def _heartbeat_loop(notifier: Notifier, interval_minutes: int):
    while True:
        try:
            await asyncio.sleep(interval_minutes * 60)
            await notifier.notify_alive(interval_minutes)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("[MAIN] Heartbeat failed: %s", e)


async def main():
    # ── Load config ───────────────────────────────────────────────────────────
    cfg = load_config()
    _setup_logging(cfg.log_dir)

    logger.info("=" * 60)
    logger.info("  Master Trade Signal Bot  starting up")
    logger.info("=" * 60)

    enabled_channels = [ch for ch in cfg.channels if ch.enabled]
    if not enabled_channels:
        logger.error("No enabled channels in channels.json — exiting.")
        sys.exit(1)

    # ── Notifier ──────────────────────────────────────────────────────────────
    notifier = Notifier()

    # ── Database ──────────────────────────────────────────────────────────────
    db = Database(cfg.db_path)

    # ── MT5 Bridge ────────────────────────────────────────────────────────────
    bridge = MT5FileBridge(
        session_prefix = cfg.mt5_session_prefix,
        demo_mode      = cfg.mt5_demo_mode,
    )
    connected = await bridge.connect()
    if not connected and not cfg.mt5_demo_mode:
        logger.error("MT5 bridge failed to connect — check EA is running")
        await notifier.notify_error("MT5 Bridge", "Failed to connect — is the EA running?")
        # Continue anyway — bridge will retry on each command

    # ── AI Parser ─────────────────────────────────────────────────────────────
    parser = AIParser(
        provider     = cfg.ai_provider,
        api_key      = cfg.ai_api_key,
        model        = cfg.ai_model,
        ollama_host  = cfg.ollama_host,
        ollama_model = cfg.ollama_model,
    )
    active_provider = await parser.startup_check()
    logger.info(f"Active AI provider: {active_provider}")

    # ── Executor ──────────────────────────────────────────────────────────────
    executor = SignalExecutor(bridge=bridge, db=db, notifier=notifier)

    # ── Components ────────────────────────────────────────────────────────────
    channel_manager = ChannelManager(
        config   = cfg,
        parser   = parser,
        executor = executor,
        notifier = notifier,
    )
    position_monitor = PositionMonitor(
        bridge   = bridge,
        db       = db,
        config   = cfg,
        notifier = notifier,
    )
    bare_watcher = BareTradeWatcher(
        bridge   = bridge,
        db       = db,
        notifier = notifier,
    )

    # Notify startup
    await notifier.notify_startup(active_provider, enabled_channels)
    await notifier.notify_alive(15)

    # Daily channel scorecard, fired between the NY close and the Tokyo open.
    report = DailyReport(db, cfg, notifier)
    report_task = None
    if daily_report.REPORT_ENABLED:
        report_task = asyncio.create_task(report.start())

    heartbeat_interval = int(os.getenv("TELEGRAM_HEARTBEAT_MINUTES", "15"))
    heartbeat_task = None
    if heartbeat_interval > 0:
        heartbeat_task = asyncio.create_task(_heartbeat_loop(notifier, heartbeat_interval))
        logger.info(f"Telegram heartbeat enabled every {heartbeat_interval} minute(s)")

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    loop = asyncio.get_event_loop()

    def _shutdown(sig):
        logger.info(f"Signal {sig.name} received — shutting down")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    if sys.platform != "win32":
        for s in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(s, lambda s=s: _shutdown(s))

    # ── Run ───────────────────────────────────────────────────────────────────
    logger.info("All components started — listening for signals")
    logger.info(f"Channels: {[ch.name for ch in enabled_channels]}")

    try:
        # asyncio.gather over three INFINITE loops only returns when all three
        # finish. If the position monitor died, the listener kept opening
        # positions with nothing detecting closes, updating P&L, tracking
        # drawdown or halting anything - and because gather never returned,
        # the crash was never logged and never notified. Wait for the FIRST
        # one to stop instead, and treat that as fatal.
        names = ["channel_manager", "position_monitor", "bare_watcher"]
        tasks = [asyncio.create_task(c, name=n) for c, n in zip(
            (channel_manager.start(), position_monitor.start(), bare_watcher.start()),
            names)]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for t in done:
            exc = t.exception() if not t.cancelled() else None
            who = t.get_name()
            if exc:
                logger.critical(f"[{who}] crashed — shutting the rest down", exc_info=exc)
                detail = f"{type(exc).__name__}: {exc}"
            else:
                logger.critical(f"[{who}] exited unexpectedly — shutting the rest down")
                detail = "exited without raising"
            if notifier:
                try:
                    await notifier.send(
                        f"🛑 <b>{who} stopped</b>\n<code>{detail}</code>\n"
                        f"The other components are being stopped too. Nothing is "
                        f"watching open positions until this is restarted.")
                except Exception:
                    pass

        # One component down means the invariants the others rely on are gone.
        # Stop cleanly rather than trading half-supervised.
        position_monitor.stop()
        bare_watcher.stop()
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    except asyncio.CancelledError:
        logger.info("Tasks cancelled")
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    finally:
        if report_task is not None:
            report.stop()
            report_task.cancel()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
        await notifier.notify_shutdown()
        await bridge.disconnect()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)