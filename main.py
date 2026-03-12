"""
main.py
=======
Signal bot entry point.
Starts all async loops via asyncio.gather.
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

from config import load_config
from bridge.mt5_bridge import MT5FileBridge
from core.ai_parser import AIParser
from core.signal_executor import SignalExecutor
from core.position_monitor import PositionMonitor
from core.bare_trade_watcher import BareTradeWatcher
from channels.channel_manager import ChannelManager
from db.database import Database
from notifications.notifier import Notifier


def _setup_logging(log_dir: str):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    Path(f"{log_dir}/channels").mkdir(exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{log_dir}/system.log", encoding="utf-8"),
    ]
    for h in handlers:
        h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        root.addHandler(h)

    # Trade-specific logger
    trade_handler = logging.FileHandler(f"{log_dir}/trades.log", encoding="utf-8")
    trade_handler.setFormatter(fmt)
    logging.getLogger("trades").addHandler(trade_handler)


logger = logging.getLogger(__name__)


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
        results = await asyncio.gather(
            channel_manager.start(),
            position_monitor.start(),
            bare_watcher.start(),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                names = ["channel_manager", "position_monitor", "bare_watcher"]
                logger.error(f"[{names[i]}] crashed: {r}", exc_info=r)

    except asyncio.CancelledError:
        logger.info("Tasks cancelled")
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    finally:
        await notifier.notify_shutdown()
        await bridge.disconnect()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)