# Master Trade Signal Bot

A standalone Telegram signal bot that listens to trading channels, parses signals using AI, and executes trades on MetaTrader 5 via a file bridge — no TCP, no ports.

---

## Features

- **Multi-channel support** — listen to multiple Telegram channels simultaneously, each with its own risk % and drawdown limit
- **AI-powered parsing** — uses Claude / OpenAI / DeepSeek / Ollama (phi3) to understand trader messages including natural language like "looking for buys" vs "buy now"
- **Automatic AI fallback** — if your configured provider fails at startup, the system automatically falls back to Ollama phi3 running locally for free
- **All TPs executed** — opens a position for every TP level (min lot floor ensures no TP is ever skipped due to account size)
- **Pre-announcement handling** — "buy now" / "sell gold" opens a bare position immediately; full signal with SL/TP upgrades it when it arrives
- **15-minute auto-close** — bare trades with no SL/TP auto-close in profit or at market after 15 minutes
- **Reply-aware** — "close this" as a reply to a signal message closes all positions from that exact signal
- **Per-channel drawdown halting** — each channel has its own drawdown limit; hitting it halts that channel only
- **Streamlit dashboard** — configure channels, view live positions, reports with equity curves, per-channel performance rankings
- **File-based MT5 bridge** — no TCP server required, works alongside the main trading system without conflicts using session prefixes

---

## Project Structure

```
master-trade-signal/
├── main.py                        # Entry point
├── config.py                      # Loads .env + channels.json
├── channels.json                  # Channel configs (non-sensitive, dashboard-editable)
├── .env.example                   # Environment variable template
├── requirements.txt
│
├── bridge/
│   └── mt5_bridge.py              # File-based MT5 communication
│
├── core/
│   ├── ai_parser.py               # AI provider abstraction + price extraction
│   ├── signal_executor.py         # Order placement, sizing, all signal types
│   ├── bare_trade_watcher.py      # 15-min auto-close for bare trades
│   └── position_monitor.py        # Live P&L tracking, drawdown, close detection
│
├── channels/
│   ├── channel_manager.py         # Manages all listeners on one Telethon client
│   └── channel_listener.py        # Per-channel message handler
│
├── db/
│   └── database.py                # SQLite: signals, positions, stats, reporting
│
├── notifications/
│   └── notifier.py                # Telegram bot notifications
│
├── dashboard/
│   └── app.py                     # Streamlit dashboard
│
├── logs/
│   ├── system.log
│   ├── trades.log
│   └── channels/
│
└── data/
    └── signals.db
```

---

## Prerequisites

- Python 3.11+
- MetaTrader 5 running with the `PythonFileBridge` EA attached to a chart
- Telegram account (for Telethon channel listener)
- Telegram bot (for notifications — create via [@BotFather](https://t.me/BotFather))
- (Optional) Ollama installed for free local AI: [ollama.com](https://ollama.com)

---

## Installation

```bash
# Clone or create the repo
git clone https://github.com/yourname/master-trade-signal
cd master-trade-signal

# Create virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
```

---

## Configuration

### 1. `.env` — Secrets and system settings

```env
# AI Provider: claude | openai | deepseek | ollama
AI_PROVIDER=ollama
AI_API_KEY=                    # leave blank for ollama

# MT5 — must differ from your main system's prefix
MT5_SESSION_PREFIX=signal
MT5_DEMO_MODE=false

# Telegram listener (get from https://my.telegram.org/apps)
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_SESSION_NAME=signal_session

# Telegram bot notifications (from @BotFather)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 2. `channels.json` — Channel configs (or edit via dashboard)

```json
{
  "channels": [
    {
      "id": "-100xxxxxxxxxx",
      "name": "marshal_500_to_100k",
      "symbol": "XAUUSD",
      "risk_pct": 20.0,
      "drawdown_pct": 60.0,
      "pre_ann_positions": 1,
      "starting_balance": 500.0,
      "balance_drift_pct": 5.0,
      "enabled": true
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `id` | Telegram channel ID (negative number) or username |
| `name` | Display name used in notifications and dashboard |
| `symbol` | Default symbol when none is mentioned in the signal |
| `risk_pct` | % of account equity risked per signal |
| `drawdown_pct` | Channel halts when daily drawdown exceeds this % |
| `pre_ann_positions` | How many positions to open on bare "buy now" signals |
| `starting_balance` | Optional per-channel working balance ($, `0` = use live equity). When set, sizing uses `min(live_equity, system_balance)` as the working balance; realized P&L flows into `system_balance` and is persisted in the DB. |
| `balance_drift_pct` | Notify when `\|equity - system_balance\|` exceeds this % (default 5%) |

---

## Running

### Start the signal bot

```bash
python main.py
```

On first run with Telethon you'll be prompted to enter your phone number and a confirmation code — this creates a session file so you only need to do it once.

### Start the dashboard (separate terminal)

```bash
streamlit run dashboard/app.py
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

---

## AI Provider Setup

The system tries your configured provider at startup. If it fails, it automatically switches to Ollama.

### Recommended providers for production

For real money / high-risk challenges, **do not** use Ollama phi3 as the primary provider — it hallucinates on the `pre_announcement` class (the parser already has a confidence < 0.90 guard to mitigate this, but the cloud providers are much more reliable). Use one of:

| Provider | Suggested model | Approx monthly cost (at typical channel volume) |
|----------|-----------------|-------------------------------------------------|
| **Claude** *(recommended)* | `claude-haiku-4-5` | ~$0.20 |
| **Gemini** | `gemini-1.5-flash` | Free tier (15 RPM) |
| OpenAI | `gpt-4o-mini` | ~$0.30 |
| DeepSeek | `deepseek-chat` | ~$0.10 |
| Ollama | `phi3` (fallback only) | Free (local) |

Set `AI_PROVIDER=claude` or `AI_PROVIDER=gemini` in `.env` and supply the matching `AI_API_KEY`. Ollama remains the automatic fallback if the cloud provider is unavailable.

### Ollama (free, local)

```bash
# Install from https://ollama.com
# Then pull the model (one-time, ~2GB):
ollama pull phi3
```

The bot will auto-pull `phi3` if Ollama is running but the model isn't downloaded yet.

### Claude (Anthropic)

```env
AI_PROVIDER=claude
AI_API_KEY=sk-ant-...
AI_MODEL=claude-haiku-4-5     # cheapest, fast enough for classification
```

### OpenAI

```env
AI_PROVIDER=openai
AI_API_KEY=sk-...
AI_MODEL=gpt-4o-mini
```

### DeepSeek

```env
AI_PROVIDER=deepseek
AI_API_KEY=your_key
AI_MODEL=deepseek-chat
```

### Gemini (Google)

Free tier covers ~15 requests per minute, more than enough for signal parsing. Get an API key at [aistudio.google.com](https://aistudio.google.com/apikey).

```env
AI_PROVIDER=gemini
AI_API_KEY=your_google_api_key
AI_MODEL=gemini-1.5-flash
```

---

## MT5 File Bridge

This bot uses a **file-based bridge** to communicate with MT5 — the same EA as the main trading system. Both can run simultaneously without conflict because each uses a different session prefix in the filename.

| System | `MT5_SESSION_PREFIX` | Command files |
|--------|---------------------|---------------|
| Main trading system | `main` | `python_command_main_XXXXXXXX_N.txt` |
| Signal bot | `signal` | `python_command_signal_XXXXXXXX_N.txt` |

The EA handles both automatically — no changes to the MQ5 file needed.

### Finding your channel ID

If you only have a channel username, send a message to [@userinfobot](https://t.me/userinfobot) or use this snippet:

```python
from telethon.sync import TelegramClient
with TelegramClient('tmp', API_ID, API_HASH) as client:
    for dialog in client.iter_dialogs():
        print(dialog.id, dialog.name)
```

---

## Signal Types Handled

| Message example | Parsed as | Action |
|----------------|-----------|--------|
| `RiskY traDE\nSELL XAUUSD\nSL 5110\nTP 5095...` | `entry` | Open all TP positions |
| `buy now` / `sell gold` | `pre_announcement` | Open bare position(s), await full signal |
| `looking for sells` / `am looking buys` | `scouting` | Notify only, no trade |
| `Move SL to breakeven` | `breakeven` | Modify all open positions |
| `TP2 hit` | `tp_hit` | Notify only (MT5 closes automatically) |
| `close this` (reply) | `close` | Close all positions from replied signal |
| `close all` | `close_all` | Close all open positions for that channel |
| `SL 5080` (standalone) | `sl_correction` | Update SL on all open positions |

---

## Dashboard Pages

| Page | Description |
|------|-------------|
| **Overview** | Summary metrics, channel status, drawdown progress, equity curve |
| **Live Positions** | Table of all open positions with live P&L |
| **Channel Config** | Add/edit/disable channels — saved to `channels.json` |
| **Balances** | Per-channel `system_balance` ledger view — see starting balance, current ledger, account equity, drift %, and reset the system balance manually |
| **Reports** | Date range + channel filter, equity curve, trade log, CSV export |
| **Performance** | Per-channel ranking by win rate and P&L, highlights underperformers |
| **Logs** | Live log viewer with search/filter |

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PROVIDER` | `claude` | AI provider: `claude`, `openai`, `deepseek`, `gemini`, `ollama` |
| `AI_API_KEY` | — | API key for cloud providers |
| `AI_MODEL` | *(provider default)* | Model override |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `phi3` | Ollama model name |
| `MT5_SESSION_PREFIX` | `signal` | File prefix — must differ from main system |
| `MT5_DEMO_MODE` | `false` | Simulate MT5 responses without real trades |
| `MT5_FILES_PATH` | *(auto-detected)* | Override MT5 Common Files path |
| `TELEGRAM_API_ID` | — | Telethon API ID |
| `TELEGRAM_API_HASH` | — | Telethon API hash |
| `TELEGRAM_SESSION_NAME` | `signal_session` | Telethon session file name |
| `TELEGRAM_BOT_TOKEN` | — | Notification bot token |
| `TELEGRAM_CHAT_ID` | — | Your Telegram chat ID for notifications |
| `DASHBOARD_PORT` | `8501` | Streamlit dashboard port |
| `DB_PATH` | `data/signals.db` | SQLite database path |
| `LOG_DIR` | `logs` | Log files directory |
| `MIN_LOT` | `0.01` | Minimum lot size |
| `MAX_LOT` | `10.0` | Maximum lot size |
| `LOT_STEP` | `0.01` | Lot size increment |
| `CONTRACT_SIZE` | `100.0` | Contract size (100 for XAUUSD, 1 for BTC, 100000 for FX) |
| `SIGNAL_MAGIC` | `234567` | MT5 magic number for signal bot orders |

---

## Testing Without MT5

Set `MT5_DEMO_MODE=true` in `.env` to run the full system with simulated MT5 responses. All signal parsing, execution logic, notifications, and dashboard work normally — orders just go to an in-memory simulator instead of real MT5.

---

## Troubleshooting

**`TELEGRAM_API_ID / TELEGRAM_API_HASH not set`**
Get them from [my.telegram.org/apps](https://my.telegram.org/apps).

**`MT5 EA status file not found`**
Make sure the `PythonFileBridge` EA is attached to a chart in MT5 and `AutoTrading` is enabled.

**`Ollama not running`**
Run `ollama serve` in a separate terminal, or install from [ollama.com](https://ollama.com).

**Channel not receiving messages**
Verify the channel ID is correct (negative number for channels). Make sure your Telegram account is a member of the channel.

**Two systems conflicting on MT5**
Ensure `MT5_SESSION_PREFIX=signal` in this bot's `.env` and the main system uses a different prefix (e.g. `main`).