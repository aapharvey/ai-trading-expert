# BTC Trading Signal System

Automated trading signal monitor for BTC/USDT perpetual futures on Bybit.
Sends signals to Telegram. No auto-trading — manual execution only.

## Features

- Price Action analysis (structure, key levels, patterns)
- Technical indicators (EMA, RSI, MACD, Bollinger Bands, ATR)
- Order Flow analysis (OI, CVD, Funding Rate, Liquidation zones)
- Confluence engine (min 3/5 signals before alert)
- Telegram notifications with entry/TP/SL/R:R
- Continuous monitoring (1-minute cycle)

## Setup

### 1. Clone & install dependencies

```bash
git clone <repo-url>
cd AI-trading-expert
python -m venv venv
venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

### 2. Configure credentials

Copy `.env.example` to `.env` and fill in your values:

```bash
copy .env.example .env
```

```env
BYBIT_API_KEY=your_bybit_api_key
BYBIT_API_SECRET=your_bybit_api_secret
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

**Getting credentials:**
- Bybit API: bybit.com → Account → API Management → Create (Read-Only)
- Telegram Bot: message @BotFather → /newbot
- Chat ID: message @userinfobot

### 3. Run

```bash
python main.py
```

## Project Structure

```
├── config.py              # All configuration & thresholds
├── logger.py              # Centralized logging
├── main.py                # Entry point & main loop
├── src/
│   ├── bybit_client.py    # Bybit REST API client
│   ├── telegram_notifier.py
│   ├── analyzers/
│   │   ├── price_action.py   # Block 1: Price Action
│   │   ├── technical.py      # Block 2: Indicators
│   │   └── order_flow.py     # Block 3: OI/CVD/Funding
│   ├── engine/
│   │   └── confluence.py     # Signal aggregation engine
│   └── models/
│       └── signals.py        # Data models
└── tests/                 # Unit tests (pytest)
```

## Running Tests

```bash
pytest tests/ -v
```

## ⚠️ Disclaimer

This system provides trading signals for informational purposes only.
All trades are executed manually by the user.
Not financial advice. Trade at your own risk.
