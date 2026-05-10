"""
config.py
=========
Loads all configuration from .env and channels.json.
Channels are stored in channels.json (non-sensitive, dashboard-editable).
Secrets stay in .env only.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

CHANNELS_FILE = Path(os.getenv("CHANNELS_FILE", "channels.json"))


@dataclass
class ChannelConfig:
    id:                  str           # Telegram channel ID or username
    name:                str           # human label
    symbol:              str = "XAUUSD"
    risk_pct:            float = 10.0  # % equity risked per signal
    drawdown_pct:        float = 20.0  # halt trading this channel at X% drawdown
    pre_ann_positions:   int   = 1     # positions to open on bare "buy/sell now"
    enabled:             bool  = True
    # Optional working-balance ledger
    starting_balance:    float = 0.0   # 0 = disabled (use live equity)
    system_balance:      float = 0.0   # runtime tracking, not persisted in JSON
    balance_drift_pct:   float = 5.0   # warn when |equity - system| > this %
    # per-channel drawdown tracking (runtime, not persisted in JSON)
    starting_equity:     float = 0.0
    current_drawdown:    float = 0.0
    halted:              bool  = False


@dataclass
class AppConfig:
    # AI
    ai_provider:    str = "ollama"   # claude | openai | deepseek | ollama
    ai_api_key:     str = ""
    ai_model:       str = ""         # blank = use provider default
    ollama_host:    str = "http://localhost:11434"
    ollama_model:   str = "phi3"

    # MT5
    mt5_session_prefix: str  = "signal"
    mt5_demo_mode:      bool = False

    # Telegram listener (Telethon)
    tg_api_id:          str = ""
    tg_api_hash:        str = ""
    tg_session_name:    str = "signal_session"

    # Telegram bot (notifications)
    tg_bot_token:       str = ""
    tg_chat_id:         str = ""

    # Dashboard
    dashboard_port:     int  = 8501
    dashboard_host:     str  = "0.0.0.0"

    # DB / paths
    db_path:            str  = "data/signals.db"
    log_dir:            str  = "logs"

    # Channels (loaded from channels.json)
    channels:           list = field(default_factory=list)


def load_config() -> AppConfig:
    cfg = AppConfig(
        ai_provider         = os.getenv("AI_PROVIDER",    "ollama").lower(),
        ai_api_key          = os.getenv("AI_API_KEY",     ""),
        ai_model            = os.getenv("AI_MODEL",       ""),
        ollama_host         = os.getenv("OLLAMA_HOST",    "http://localhost:11434"),
        ollama_model        = os.getenv("OLLAMA_MODEL",   "phi3"),

        mt5_session_prefix  = os.getenv("MT5_SESSION_PREFIX", "signal"),
        mt5_demo_mode       = os.getenv("MT5_DEMO_MODE",  "false").lower() == "true",

        tg_api_id           = os.getenv("TELEGRAM_API_ID",    ""),
        tg_api_hash         = os.getenv("TELEGRAM_API_HASH",  ""),
        tg_session_name     = os.getenv("TELEGRAM_SESSION_NAME", "signal_session"),
        tg_bot_token        = os.getenv("TELEGRAM_BOT_TOKEN", ""),
        tg_chat_id          = os.getenv("TELEGRAM_CHAT_ID",   ""),

        dashboard_port      = int(os.getenv("DASHBOARD_PORT", "8501")),
        dashboard_host      = os.getenv("DASHBOARD_HOST", "0.0.0.0"),

        db_path             = os.getenv("DB_PATH",  "data/signals.db"),
        log_dir             = os.getenv("LOG_DIR",  "logs"),

        channels            = _load_channels(),
    )
    return cfg


def _load_channels() -> list[ChannelConfig]:
    if not CHANNELS_FILE.exists():
        logger.warning(f"[CONFIG] {CHANNELS_FILE} not found — no channels configured")
        return []
    try:
        data = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
        channels = []
        for ch in data.get("channels", []):
            channels.append(ChannelConfig(
                id                = str(ch["id"]),
                name              = ch.get("name", str(ch["id"])),
                symbol            = ch.get("symbol", "XAUUSD"),
                risk_pct          = float(ch.get("risk_pct", 10.0)),
                drawdown_pct      = float(ch.get("drawdown_pct", 20.0)),
                pre_ann_positions = int(ch.get("pre_ann_positions", 1)),
                enabled           = bool(ch.get("enabled", True)),
                starting_balance  = float(ch.get("starting_balance", 0.0)),
                balance_drift_pct = float(ch.get("balance_drift_pct", 5.0)),
            ))
        logger.info(f"[CONFIG] Loaded {len(channels)} channel(s)")
        return channels
    except Exception as e:
        logger.error(f"[CONFIG] Failed to load channels.json: {e}")
        return []


def save_channels(channels: list[ChannelConfig]):
    """Write channel configs back to channels.json (called from dashboard)."""
    data = {
        "channels": [
            {
                "id":                 ch.id,
                "name":               ch.name,
                "symbol":             ch.symbol,
                "risk_pct":           ch.risk_pct,
                "drawdown_pct":       ch.drawdown_pct,
                "pre_ann_positions":  ch.pre_ann_positions,
                "starting_balance":   ch.starting_balance,
                "balance_drift_pct":  ch.balance_drift_pct,
                "enabled":            ch.enabled,
            }
            for ch in channels
        ]
    }
    CHANNELS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info(f"[CONFIG] Saved {len(channels)} channel(s) to {CHANNELS_FILE}")
    