"""
Confluence Engine — aggregates signals from all 3 blocks and emits TradeSignals.

Logic:
  1. Collect all raw signals from PriceAction, Technical, OrderFlow
  2. Score each direction (LONG / SHORT) using signal weights from config
  3. Normalize score to 1–5
  4. If score >= MIN_SIGNAL_STRENGTH and R:R >= MIN_RR_RATIO → emit TradeSignal
  5. Anti-spam: suppress same-direction signals within ANTI_SPAM_HOURS
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from logger import get_logger
from src.analyzers.price_action import PriceActionResult
from src.analyzers.technical import TechnicalResult
from src.analyzers.order_flow import OrderFlowResult
from src.models.signals import Direction, Timeframe, TradeSignal

log = get_logger(__name__)


# Signals that contribute to LONG direction
_LONG_SIGNALS = {
    "BULLISH_BREAK", "AT_KEY_SUPPORT",
    "EMA_CROSS_UP", "PRICE_ABOVE_EMA200",
    "RSI_OVERSOLD", "MACD_CROSS_UP", "MACD_DIVERGENCE_BULL",
    "BB_BREAKOUT_UP",
    "OI_LONG_BUILDUP", "OI_SHORT_UNWIND",
    "CVD_DIVERGENCE_BULL",
    "FUNDING_EXTREME_NEGATIVE",    # oversold shorts → squeeze up
    "LIQUIDATION_ZONE_NEARBY_ABOVE",  # price hunting liq above → move up
}

# Signals that contribute to SHORT direction
_SHORT_SIGNALS = {
    "BEARISH_BREAK", "AT_KEY_RESISTANCE",
    "EMA_CROSS_DOWN", "PRICE_BELOW_EMA200",
    "RSI_OVERBOUGHT", "MACD_CROSS_DOWN", "MACD_DIVERGENCE_BEAR",
    "BB_BREAKOUT_DOWN",
    "OI_SHORT_BUILDUP", "OI_LONG_UNWIND",
    "CVD_DIVERGENCE_BEAR",
    "FUNDING_EXTREME_POSITIVE",    # oversold longs → squeeze down
    "LIQUIDATION_ZONE_NEARBY_BELOW",
}

# Maximum possible score (sum of all weights for one direction)
_MAX_SCORE = sum(
    w for sig, w in config.SIGNAL_WEIGHTS.items()
    if sig in _LONG_SIGNALS
)


class ConfluenceEngine:
    """
    Combines signals from all analyzers into actionable TradeSignals.
    Stateful: tracks last signal per direction for anti-spam.
    """

    def __init__(self):
        self._last_signal: dict[str, Optional[datetime]] = {
            Direction.LONG:  None,
            Direction.SHORT: None,
        }

    def evaluate(
        self,
        pa:    PriceActionResult,
        tech:  TechnicalResult,
        of:    OrderFlowResult,
    ) -> Optional[TradeSignal]:
        """
        Main entry point. Returns a TradeSignal or None.
        """
        all_signals = pa.signals + tech.signals + of.signals

        long_score, long_factors   = self._score(all_signals, _LONG_SIGNALS)
        short_score, short_factors = self._score(all_signals, _SHORT_SIGNALS)

        long_strength  = self._normalize_score(long_score)
        short_strength = self._normalize_score(short_score)

        log.debug(
            "Confluence: LONG=%.2f(%d/5) SHORT=%.2f(%d/5) signals=%s",
            long_score, long_strength, short_score, short_strength, all_signals,
        )

        # Determine winning direction
        if long_strength >= short_strength and long_strength >= config.MIN_SIGNAL_STRENGTH:
            direction = Direction.LONG
            strength  = long_strength
            factors   = long_factors
        elif short_strength > long_strength and short_strength >= config.MIN_SIGNAL_STRENGTH:
            direction = Direction.SHORT
            strength  = short_strength
            factors   = short_factors
        else:
            return None  # No strong enough signal

        # Anti-spam check
        if not self._can_send(direction):
            log.debug("Confluence: anti-spam suppressed %s signal", direction)
            return None

        # Build trade levels
        signal = self._build_signal(direction, strength, factors, pa, tech)
        if signal is None:
            return None

        # R:R check
        if signal.rr_ratio < config.MIN_RR_RATIO:
            log.debug(
                "Confluence: R:R %.2f < minimum %.2f — signal suppressed",
                signal.rr_ratio, config.MIN_RR_RATIO,
            )
            return None

        # Record last signal time
        self._last_signal[direction] = datetime.now(timezone.utc)
        log.info(
            "Confluence: emitting %s signal strength=%d/5 rr=%.2f",
            direction, strength, signal.rr_ratio,
        )
        return signal

    # ─── Scoring ─────────────────────────────────────────────────────────────

    def _score(
        self, signals: list[str], direction_set: set[str]
    ) -> tuple[float, list[str]]:
        """Sum weights for all signals belonging to `direction_set`."""
        total = 0.0
        factors = []
        for sig in signals:
            if sig in direction_set:
                weight = config.SIGNAL_WEIGHTS.get(sig, 0.5)
                total += weight
                factors.append(self._signal_to_label(sig))
        return total, factors

    def _normalize_score(self, raw: float) -> int:
        """Map raw weighted score to 1–5 strength."""
        if raw <= 0:
            return 0
        # Scale relative to ~40% of max score = strength 5
        pct = raw / (_MAX_SCORE * 0.4)
        return min(5, max(1, round(pct * 5)))

    # ─── Trade level construction ─────────────────────────────────────────────

    def _build_signal(
        self,
        direction: Direction,
        strength: int,
        factors: list[str],
        pa: PriceActionResult,
        tech: TechnicalResult,
    ) -> Optional[TradeSignal]:
        """Construct entry zone, SL, TP using ATR levels + price action."""

        price = pa.current_price or tech.current_price
        if not price:
            return None

        # Entry zone: based on nearest PA level; SL/TP: ATR distance from entry
        atr = tech.atr or price * 0.005  # fallback: 0.5% of price

        if direction == Direction.LONG:
            entry_mid  = pa.nearest_support or price
            entry_low  = entry_mid * 0.9985
            entry_high = entry_mid * 1.0015
            stop_loss  = entry_mid - atr * config.SL_ATR_MULTIPLIER
            tp1        = entry_mid + atr * config.TP1_ATR_MULTIPLIER
            tp2        = entry_mid + atr * config.TP2_ATR_MULTIPLIER
        else:
            entry_mid  = pa.nearest_resistance or price
            entry_low  = entry_mid * 0.9985
            entry_high = entry_mid * 1.0015
            stop_loss  = entry_mid + atr * config.SL_ATR_MULTIPLIER
            tp1        = entry_mid - atr * config.TP1_ATR_MULTIPLIER
            tp2        = entry_mid - atr * config.TP2_ATR_MULTIPLIER

        # Calculate R:R using TP2 (final target) for accurate ratio
        risk   = abs(entry_mid - stop_loss)
        reward = abs(tp2 - entry_mid)
        rr     = reward / risk if risk > 0 else 0.0

        # Determine timeframe from strength
        timeframe = Timeframe.SWING if strength >= 4 else Timeframe.INTRADAY

        return TradeSignal(
            direction=direction,
            strength=strength,
            entry_low=entry_low,
            entry_high=entry_high,
            tp1=tp1,
            tp2=tp2,
            stop_loss=stop_loss,
            rr_ratio=round(rr, 2),
            timeframe=timeframe,
            factors=factors,
        )

    # ─── Anti-spam ────────────────────────────────────────────────────────────

    def _can_send(self, direction: Direction) -> bool:
        last = self._last_signal.get(direction)
        if last is None:
            return True
        elapsed = datetime.now(timezone.utc) - last
        return elapsed >= timedelta(hours=config.ANTI_SPAM_HOURS)

    def reset_cooldown(self, direction: Optional[Direction] = None) -> None:
        """Reset anti-spam timer (for testing or manual override)."""
        if direction:
            self._last_signal[direction] = None
        else:
            self._last_signal = {Direction.LONG: None, Direction.SHORT: None}

    # ─── Signal labels ────────────────────────────────────────────────────────

    @staticmethod
    def _signal_to_label(signal: str) -> str:
        labels = {
            "BULLISH_BREAK":              "Price broke key resistance",
            "BEARISH_BREAK":              "Price broke key support",
            "AT_KEY_SUPPORT":             "Price at key support level",
            "AT_KEY_RESISTANCE":          "Price at key resistance level",
            "EMA_CROSS_UP":               "EMA 21 crossed above EMA 55",
            "EMA_CROSS_DOWN":             "EMA 21 crossed below EMA 55",
            "PRICE_ABOVE_EMA200":         "Price above EMA 200 (bullish bias)",
            "PRICE_BELOW_EMA200":         "Price below EMA 200 (bearish bias)",
            "RSI_OVERSOLD":               "RSI oversold (<30)",
            "RSI_OVERBOUGHT":             "RSI overbought (>70)",
            "MACD_CROSS_UP":              "MACD bullish crossover",
            "MACD_CROSS_DOWN":            "MACD bearish crossover",
            "MACD_DIVERGENCE_BULL":       "Bullish MACD divergence",
            "MACD_DIVERGENCE_BEAR":       "Bearish MACD divergence",
            "BB_SQUEEZE":                 "Bollinger Band squeeze (breakout incoming)",
            "BB_BREAKOUT_UP":             "Price broke above upper Bollinger Band",
            "BB_BREAKOUT_DOWN":           "Price broke below lower Bollinger Band",
            "OI_LONG_BUILDUP":            "OI rising + price rising (long build-up)",
            "OI_SHORT_BUILDUP":           "OI rising + price falling (short build-up)",
            "OI_LONG_UNWIND":             "OI falling + price falling (long unwind)",
            "OI_SHORT_UNWIND":            "OI falling + price rising (short squeeze)",
            "CVD_DIVERGENCE_BULL":        "Bullish CVD divergence (buyers absorbing)",
            "CVD_DIVERGENCE_BEAR":        "Bearish CVD divergence (sellers dominating)",
            "FUNDING_EXTREME_POSITIVE":   "Funding rate extreme high (longs overextended)",
            "FUNDING_EXTREME_NEGATIVE":   "Funding rate extreme low (shorts overextended)",
            "LIQUIDATION_ZONE_NEARBY_ABOVE": "Liquidation cluster above price",
            "LIQUIDATION_ZONE_NEARBY_BELOW": "Liquidation cluster below price",
        }
        return labels.get(signal, signal)
