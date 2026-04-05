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
