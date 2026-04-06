"""
Data models for trading signals and notifications.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    LONG    = "LONG"
    SHORT   = "SHORT"
    NEUTRAL = "NEUTRAL"


class Timeframe(str, Enum):
    INTRADAY = "Intraday (hours)"
    SWING    = "Swing (2-7 days)"


@dataclass
class TradeSignal:
    """Complete trading signal emitted by the Confluence Engine."""

    direction:   Direction
    strength:    int                     # 1–5
    entry_low:   float                   # entry zone lower bound
    entry_high:  float                   # entry zone upper bound
    tp1:         float
    tp2:         float
    stop_loss:   float
    rr_ratio:    float
    timeframe:   Timeframe
    factors:     list[str] = field(default_factory=list)   # active confluence factors
    created_at:  datetime  = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2

    @property
    def strength_stars(self) -> str:
        return "⭐" * self.strength + "☆" * (5 - self.strength)


# ─── Phase 2 result models ───────────────────────────────────────────────────

from dataclasses import dataclass as _dc
from typing import Optional as _Opt


@_dc
class SentimentResult:
    """Fear & Greed index + CryptoPanic news sentiment."""
    fear_greed_value:  _Opt[int]   = None   # 0–100
    fear_greed_label:  _Opt[str]   = None   # "Extreme Fear" … "Extreme Greed"
    news_sentiment:    _Opt[str]   = None   # "BULLISH", "BEARISH", "NEUTRAL"
    bullish_news_count: int        = 0
    bearish_news_count: int        = 0
    signals: list[str]             = field(default_factory=list)


@_dc
class OnChainResult:
    """CoinMetrics Community on-chain metrics."""
    exchange_netflow:  _Opt[float] = None   # BTC net change on exchanges (daily)
    mvrv:              _Opt[float] = None   # Market Value to Realized Value ratio
    signals: list[str]             = field(default_factory=list)
