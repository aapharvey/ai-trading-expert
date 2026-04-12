"""
Centralized configuration for BTC Trading Signal System.
All tunable parameters are here — no magic numbers in code.
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ─── Credentials ────────────────────────────────────────────────────────────

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Phase 2 API keys
CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")
# CoinMetrics Community Edition — no API key required (free, no registration)


# ─── Exchange ────────────────────────────────────────────────────────────────

SYMBOL = "BTCUSDT"
CATEGORY = "linear"          # Bybit perpetual futures
BYBIT_BASE_URL = "https://api.bybit.com"

# Candle limits per request
KLINE_LIMIT = 200


# ─── Timeframes ─────────────────────────────────────────────────────────────

TIMEFRAMES = {
    "15m":  "15",
    "1h":   "60",
    "4h":   "240",
    "1d":   "D",
}

# Timeframes used per indicator
TF_PRICE_ACTION  = ["1h", "4h", "1d"]
TF_EMA           = ["1h", "4h"]
TF_RSI           = ["1h", "4h"]
TF_MACD          = ["4h", "1d"]
TF_BB            = ["1h"]
TF_ATR           = ["1h"]


# ─── Indicators ─────────────────────────────────────────────────────────────

EMA_PERIODS      = [21, 55, 200]
RSI_PERIOD       = 14
RSI_OVERSOLD     = 30
RSI_OVERBOUGHT   = 70
MACD_FAST        = 12
MACD_SLOW        = 26
MACD_SIGNAL      = 9
BB_PERIOD        = 20
BB_STD           = 2.0
BB_SQUEEZE_THRESHOLD = 0.03   # BB width < 3% of price → squeeze
ATR_PERIOD       = 14

# SL/TP multipliers (ATR-based)
SL_ATR_MULTIPLIER   = 1.5
TP1_ATR_MULTIPLIER  = 2.0
TP2_ATR_MULTIPLIER  = 3.5


# ─── Liquidity Map (Block 7) ─────────────────────────────────────────────────

# Order wall: cluster within this % of price is scanned
LIQUIDITY_SCAN_RANGE_PCT    = 0.02    # 2% above/below price
# A cluster is a wall when its volume > this multiple of mean cluster volume
LIQUIDITY_WALL_MULTIPLIER   = 5.0
# Delta: buy_vol/total_vol threshold to signal BULL/BEAR
LIQUIDITY_DELTA_THRESHOLD   = 0.55    # >55% buys → BULL; <45% → BEAR


# ─── Volume Profile (Block 8) ─────────────────────────────────────────────────

# Candles used for current session profile (1h candles = 24h window)
VP_SESSION_CANDLES          = 24
# Fraction of volume defining the value area
VP_VALUE_AREA_PCT           = 0.70
# Price bucket size in USD for volume distribution
VP_BUCKET_SIZE              = 50.0
# How close price must be to POC to fire AT_POC signal (%)
VP_AT_POC_THRESHOLD_PCT     = 0.005   # 0.5%


# ─── Order Flow ──────────────────────────────────────────────────────────────

OI_CHANGE_THRESHOLD_PCT = 2.0     # OI change > 2% in 1h = significant
FUNDING_EXTREME_HIGH    = 0.01    # % per 8h — 95th percentile of Bybit BTC history (200 records, Apr 2026)
FUNDING_EXTREME_LOW     = -0.01
LIQUIDATION_ZONE_NEAR_PCT = 2.0   # Liquidation zone within 2% of price


# ─── Signal Engine ───────────────────────────────────────────────────────────

MIN_SIGNAL_STRENGTH     = 3       # Out of 5 — below this, no alert sent
MIN_RR_RATIO            = 1.5     # Minimum Risk:Reward to send signal
ANTI_SPAM_HOURS         = 4       # Same-direction signal cooldown (hours)

# Signal weights (contribution to confluence score)
SIGNAL_WEIGHTS = {
    # Price Action (Block 1)
    "BULLISH_BREAK":           1.0,
    "BEARISH_BREAK":           1.0,
    "AT_KEY_SUPPORT":          0.8,
    "AT_KEY_RESISTANCE":       0.8,
    # Technical (Block 2)
    "EMA_CROSS_UP":            0.8,
    "EMA_CROSS_DOWN":          0.8,
    "PRICE_ABOVE_EMA200":      0.5,
    "PRICE_BELOW_EMA200":      0.5,
    "RSI_OVERSOLD":            0.7,
    "RSI_OVERBOUGHT":          0.7,
    "MACD_CROSS_UP":           0.7,
    "MACD_CROSS_DOWN":         0.7,
    "MACD_DIVERGENCE_BULL":    1.0,
    "MACD_DIVERGENCE_BEAR":    1.0,
    "BB_SQUEEZE":              0.5,
    "BB_BREAKOUT_UP":          0.8,
    "BB_BREAKOUT_DOWN":        0.8,
    # Order Flow (Block 3)
    "OI_LONG_BUILDUP":         0.8,
    "OI_SHORT_BUILDUP":        0.8,
    "OI_LONG_UNWIND":          0.7,
    "OI_SHORT_UNWIND":         0.7,
    "CVD_DIVERGENCE_BULL":     0.6,
    "CVD_DIVERGENCE_BEAR":     0.6,
    "FUNDING_EXTREME_POSITIVE": 0.8,
    "FUNDING_EXTREME_NEGATIVE": 0.8,
    "LIQUIDATION_ZONE_NEARBY_ABOVE": 0.7,
    "LIQUIDATION_ZONE_NEARBY_BELOW": 0.7,
    # Sentiment — Fear & Greed (Block 4)
    "EXTREME_FEAR":                  0.9,
    "EXTREME_GREED":                 0.9,
    "FEAR":                          0.4,
    "GREED":                         0.4,
    # Liquidity Map (Block 7)
    "ORDER_WALL_BELOW":              0.9,   # large bid wall below — strong support magnet
    "ORDER_WALL_ABOVE":              0.9,   # large ask wall above — strong resistance magnet
    "DELTA_BULL":                    0.7,   # buyers dominating recent trades
    "DELTA_BEAR":                    0.7,   # sellers dominating recent trades
    # Volume Profile (Block 8)
    "PRICE_AT_POC_FROM_BELOW":       0.8,   # returning to POC from below → LONG
    "PRICE_AT_POC_FROM_ABOVE":       0.8,   # returning to POC from above → SHORT
    "ABOVE_VALUE_AREA":              0.7,   # price broke above VAH → momentum LONG
    "BELOW_VALUE_AREA":              0.7,   # price broke below VAL → momentum SHORT
    "NAKED_POC_ABOVE":               0.6,   # unvisited POC above → magnet LONG
    "NAKED_POC_BELOW":               0.6,   # unvisited POC below → magnet SHORT
    # News (Block 6)
    "NEWS_BULLISH_MAJOR":            0.8,
    "NEWS_BEARISH_MAJOR":            0.8,
    "HIGH_IMPACT_EVENT_APPROACHING": 0.5,
    # On-chain (Block 5)
    "EXCHANGE_INFLOW_SPIKE":         0.8,
    "EXCHANGE_OUTFLOW_SPIKE":        0.8,
    "WHALE_ACCUMULATION":            0.9,
    "MVRV_BOTTOM_SIGNAL":            0.8,
    "MVRV_TOP_SIGNAL":               0.8,
}


# ─── Fear & Greed ────────────────────────────────────────────────────────────

FEAR_GREED_EXTREME_FEAR   = 20    # index <= this → EXTREME_FEAR signal
FEAR_GREED_FEAR           = 40    # index <= this → FEAR signal
FEAR_GREED_GREED          = 60    # index >= this → GREED signal
FEAR_GREED_EXTREME_GREED  = 80    # index >= this → EXTREME_GREED signal


# ─── News (CryptoPanic) ───────────────────────────────────────────────────────

CRYPTOPANIC_BASE_URL      = "https://cryptopanic.com/api/developer/v2/posts/"
# Two separate calls: filter=bullish and filter=bearish (last 24h posts counted)
# Signal fires when one side has >= THRESHOLD posts vs the other
NEWS_DIRECTION_THRESHOLD  = 3     # bullish_count - bearish_count >= this → signal
NEWS_MIN_POSTS            = 2     # minimum posts needed to form an opinion
NEWS_POLL_INTERVAL_MIN    = 20    # Fetch news every 20 minutes


# ─── On-chain (CoinMetrics Community) ────────────────────────────────────────

COINMETRICS_BASE_URL         = "https://community-api.coinmetrics.io/v4"
# Exchange netflow: spike = current value deviates > N standard deviations
# from the 7-day rolling mean (avoids hardcoded absolute BTC thresholds)
EXCHANGE_FLOW_STD_MULTIPLIER = 2.0   # signal when |netflow| > mean ± 2σ
EXCHANGE_FLOW_HISTORY_DAYS   = 7     # rolling window for mean/std calculation
# MVRV thresholds (Expert-validated)
MVRV_BOTTOM_THRESHOLD        = 1.0   # < 1.0 → market cap below realized cap (capitulation)
MVRV_TOP_THRESHOLD           = 3.5   # > 3.5 → historically overbought (potential top)
ONCHAIN_POLL_INTERVAL_MIN    = 60    # Fetch on-chain data every 60 minutes


# ─── Scheduler ───────────────────────────────────────────────────────────────

MARKET_POLL_INTERVAL_SEC  = 60     # Main market data loop
HEARTBEAT_INTERVAL_MIN    = 60     # Telegram heartbeat


# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_DIR   = "logs"
LOG_FILE  = "logs/trading_bot.log"
LOG_LEVEL = "INFO"
