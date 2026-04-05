"""
Block 1: Price Action Analyzer.
Detects market structure, key levels, candlestick patterns, and breakouts.
"""

from dataclasses import dataclass, field
from typing import Optional

import config
from logger import get_logger

log = get_logger(__name__)


# ─── Data structures ─────────────────────────────────────────────────────────

@dataclass
class Level:
    price: float
    touches: int = 1
    is_support: bool = True  # False = resistance


@dataclass
class PriceActionResult:
    trend: str                          # "BULLISH", "BEARISH", "RANGE"
    key_supports: list[float]
    key_resistances: list[float]
    patterns: list[str]                 # detected candle patterns
    signals: list[str]                  # generated signal names
    current_price: float
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    wyckoff_phase: Optional[str] = None  # "ACCUMULATION","MARKUP","DISTRIBUTION","MARKDOWN"


# ─── Analyzer ────────────────────────────────────────────────────────────────

class PriceActionAnalyzer:
    """
    Analyzes OHLCV candle data to detect:
    - Market structure (HH/HL vs LH/LL vs Range)
    - Key support/resistance levels (swing high/low clustering)
    - Candlestick patterns (engulfing, pin bar, doji) at key levels
    - Breakout + retest confirmation
    - Wyckoff phase (basic)
    """

    # Tolerance for clustering nearby levels (% of price)
    LEVEL_CLUSTER_PCT = 0.005   # 0.5%
    # Min candles between swing points
    SWING_LOOKBACK = 5
    # How close price must be to a level to trigger AT_KEY_* signal (%)
    AT_LEVEL_THRESHOLD_PCT = 0.008  # 0.8%
    # Body-to-wick ratio for pin bar detection
    PIN_BAR_WICK_RATIO = 2.5
    # Max body size (% of candle range) for doji
    DOJI_BODY_PCT = 0.1

    def analyze(self, candles: list[dict]) -> PriceActionResult:
        """
        Main entry point. Accepts list of OHLCV dicts (oldest first).
        Returns PriceActionResult with all detected signals.
        """
        if len(candles) < 20:
            log.warning("Price action: insufficient candles (%d < 20)", len(candles))
            return PriceActionResult(
                trend="RANGE", key_supports=[], key_resistances=[],
                patterns=[], signals=[], current_price=0.0
            )

        closes = [c["close"] for c in candles]
        highs  = [c["high"]  for c in candles]
        lows   = [c["low"]   for c in candles]
        current_price = closes[-1]

        # Core analysis
        swing_highs, swing_lows = self._find_swings(highs, lows)
        trend = self._determine_trend(swing_highs, swing_lows, closes)
        supports, resistances = self._cluster_levels(swing_lows, swing_highs, current_price)
        patterns = self._detect_patterns(candles[-5:], supports, resistances, current_price)
        wyckoff  = self._detect_wyckoff(candles, trend, swing_highs, swing_lows)

        nearest_sup = self._nearest_below(supports, current_price)
        nearest_res = self._nearest_above(resistances, current_price)

        signals = self._generate_signals(
            trend, current_price, supports, resistances,
            patterns, candles, nearest_sup, nearest_res,
        )

        log.debug(
            "PriceAction: trend=%s sup=%s res=%s signals=%s",
            trend, nearest_sup, nearest_res, signals,
        )

        return PriceActionResult(
            trend=trend,
            key_supports=supports,
            key_resistances=resistances,
            patterns=patterns,
            signals=signals,
            current_price=current_price,
            nearest_support=nearest_sup,
            nearest_resistance=nearest_res,
            wyckoff_phase=wyckoff,
        )

    # ─── Swing detection ─────────────────────────────────────────────────────

    def _find_swings(
        self, highs: list[float], lows: list[float]
    ) -> tuple[list[float], list[float]]:
        """
        Identify swing highs and swing lows.
        A swing high: candle whose high is the highest in ±SWING_LOOKBACK candles.
        A swing low: candle whose low is the lowest in ±SWING_LOOKBACK candles.
        """
        n = len(highs)
        lb = self.SWING_LOOKBACK
        swing_highs, swing_lows = [], []

        for i in range(lb, n - lb):
            window_h = highs[i - lb: i + lb + 1]
            if highs[i] == max(window_h):
                swing_highs.append(highs[i])

            window_l = lows[i - lb: i + lb + 1]
            if lows[i] == min(window_l):
                swing_lows.append(lows[i])

        return swing_highs, swing_lows

    # ─── Trend determination ─────────────────────────────────────────────────

    def _determine_trend(
        self,
        swing_highs: list[float],
        swing_lows: list[float],
        closes: list[float],
    ) -> str:
        """
        Determine trend from swing structure.
        Bullish:  last 2 swing highs rising AND last 2 swing lows rising (HH + HL)
        Bearish:  last 2 swing highs falling AND last 2 swing lows falling (LH + LL)
        Range:    otherwise
        """
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            hh = swing_highs[-1] > swing_highs[-2]
            hl = swing_lows[-1]  > swing_lows[-2]
            lh = swing_highs[-1] < swing_highs[-2]
            ll = swing_lows[-1]  < swing_lows[-2]

            if hh and hl:
                return "BULLISH"
            if lh and ll:
                return "BEARISH"

        # Fallback: 50-candle EMA slope
        if len(closes) >= 50:
            ema_now  = sum(closes[-10:]) / 10
            ema_past = sum(closes[-50:-40]) / 10
            if ema_now > ema_past * 1.005:
                return "BULLISH"
            if ema_now < ema_past * 0.995:
                return "BEARISH"

        return "RANGE"

    # ─── Level clustering ────────────────────────────────────────────────────

    def _cluster_levels(
        self,
        swing_lows: list[float],
        swing_highs: list[float],
        current_price: float,
    ) -> tuple[list[float], list[float]]:
        """
        Group nearby swing lows → support levels, swing highs → resistance levels.
        Levels within LEVEL_CLUSTER_PCT of each other are merged (average).
        Returns top-5 supports and top-5 resistances, sorted by distance to price.
        """
        supports    = self._merge_levels(swing_lows)
        resistances = self._merge_levels(swing_highs)

        # Keep only relevant levels (within 20% of current price)
        supports    = [l for l in supports    if l < current_price * 1.02]
        resistances = [l for l in resistances if l > current_price * 0.98]

        # Sort by distance to current price
        supports    = sorted(supports,    key=lambda l: abs(current_price - l))[:5]
        resistances = sorted(resistances, key=lambda l: abs(current_price - l))[:5]

        return supports, resistances

    def _merge_levels(self, prices: list[float]) -> list[float]:
        """Merge price points that are within LEVEL_CLUSTER_PCT of each other."""
        if not prices:
            return []
        sorted_prices = sorted(prices)
        clusters: list[list[float]] = [[sorted_prices[0]]]

        for price in sorted_prices[1:]:
            last_cluster = clusters[-1]
            cluster_center = sum(last_cluster) / len(last_cluster)
            if abs(price - cluster_center) / cluster_center <= self.LEVEL_CLUSTER_PCT:
                last_cluster.append(price)
            else:
                clusters.append([price])

        return [sum(c) / len(c) for c in clusters]

    # ─── Candlestick patterns ────────────────────────────────────────────────

    def _detect_patterns(
        self,
        recent_candles: list[dict],
        supports: list[float],
        resistances: list[float],
        current_price: float,
    ) -> list[str]:
        """Detect candlestick patterns on the last few candles at key levels."""
        patterns = []
        if len(recent_candles) < 2:
            return patterns

        # Only flag patterns if near a key level
        near_level = self._is_near_any_level(
            current_price, supports + resistances
        )

        last  = recent_candles[-1]
        prev  = recent_candles[-2]

        if self._is_bullish_engulfing(prev, last) and near_level:
            patterns.append("BULLISH_ENGULFING")

        if self._is_bearish_engulfing(prev, last) and near_level:
            patterns.append("BEARISH_ENGULFING")

        if self._is_pin_bar(last, bullish=True) and near_level:
            patterns.append("BULLISH_PIN_BAR")

        if self._is_pin_bar(last, bullish=False) and near_level:
            patterns.append("BEARISH_PIN_BAR")

        if self._is_doji(last) and near_level:
            patterns.append("DOJI")

        return patterns

    def _is_near_any_level(self, price: float, levels: list[float]) -> bool:
        if not levels:
            return False
        return any(
            abs(price - lvl) / lvl <= self.AT_LEVEL_THRESHOLD_PCT
            for lvl in levels
        )

    def _is_bullish_engulfing(self, prev: dict, curr: dict) -> bool:
        prev_bearish = prev["close"] < prev["open"]
        curr_bullish = curr["close"] > curr["open"]
        curr_engulfs = curr["open"] <= prev["close"] and curr["close"] >= prev["open"]
        return prev_bearish and curr_bullish and curr_engulfs

    def _is_bearish_engulfing(self, prev: dict, curr: dict) -> bool:
        prev_bullish = prev["close"] > prev["open"]
        curr_bearish = curr["close"] < curr["open"]
        curr_engulfs = curr["open"] >= prev["close"] and curr["close"] <= prev["open"]
        return prev_bullish and curr_bearish and curr_engulfs

    def _is_pin_bar(self, candle: dict, bullish: bool) -> bool:
        body = abs(candle["close"] - candle["open"])
        total_range = candle["high"] - candle["low"]
        if total_range == 0:
            return False
        if bullish:
            lower_wick = min(candle["open"], candle["close"]) - candle["low"]
            return lower_wick >= body * self.PIN_BAR_WICK_RATIO
        else:
            upper_wick = candle["high"] - max(candle["open"], candle["close"])
            return upper_wick >= body * self.PIN_BAR_WICK_RATIO

    def _is_doji(self, candle: dict) -> bool:
        body = abs(candle["close"] - candle["open"])
        total_range = candle["high"] - candle["low"]
        if total_range == 0:
            return False
        return body / total_range <= self.DOJI_BODY_PCT

    # ─── Wyckoff phase ───────────────────────────────────────────────────────

    def _detect_wyckoff(
        self,
        candles: list[dict],
        trend: str,
        swing_highs: list[float],
        swing_lows: list[float],
    ) -> Optional[str]:
        """Basic Wyckoff phase detection based on trend + volume pattern."""
        if len(candles) < 30:
            return None

        volumes = [c["volume"] for c in candles]
        avg_vol_recent = sum(volumes[-10:]) / 10
        avg_vol_past   = sum(volumes[-30:-20]) / 10

        vol_expanding = avg_vol_recent > avg_vol_past * 1.2
        vol_contracting = avg_vol_recent < avg_vol_past * 0.8

        if trend == "RANGE" and vol_contracting:
            return "ACCUMULATION"
        if trend == "BULLISH" and vol_expanding:
            return "MARKUP"
        if trend == "RANGE" and vol_expanding:
            return "DISTRIBUTION"
        if trend == "BEARISH" and vol_expanding:
            return "MARKDOWN"

        return None

    # ─── Signal generation ───────────────────────────────────────────────────

    def _generate_signals(
        self,
        trend: str,
        current_price: float,
        supports: list[float],
        resistances: list[float],
        patterns: list[str],
        candles: list[dict],
        nearest_sup: Optional[float],
        nearest_res: Optional[float],
    ) -> list[str]:
        signals = []

        # Range signal
        if trend == "RANGE":
            signals.append("RANGE_BOUND")

        # At key support
        if nearest_sup and abs(current_price - nearest_sup) / nearest_sup <= self.AT_LEVEL_THRESHOLD_PCT:
            signals.append("AT_KEY_SUPPORT")

        # At key resistance
        if nearest_res and abs(current_price - nearest_res) / nearest_res <= self.AT_LEVEL_THRESHOLD_PCT:
            signals.append("AT_KEY_RESISTANCE")

        # Breakout detection (last candle closed beyond a level with strong body)
        if len(candles) >= 3:
            signals += self._detect_breakouts(candles, supports, resistances)

        return signals

    def _detect_breakouts(
        self,
        candles: list[dict],
        supports: list[float],
        resistances: list[float],
    ) -> list[str]:
        """Detect confirmed breakout: close beyond level + previous candle was near it."""
        signals = []
        last   = candles[-1]
        prev   = candles[-2]
        pprev  = candles[-3]

        # Bullish breakout: closed above resistance; previous candle was below it
        for res in resistances:
            was_below = pprev["close"] < res and prev["close"] < res
            now_above = last["close"] > res
            strong_body = (last["close"] - last["open"]) > (last["high"] - last["low"]) * 0.5
            if was_below and now_above and strong_body:
                signals.append("BULLISH_BREAK")
                break

        # Bearish breakout: closed below support; previous candle was above it
        for sup in supports:
            was_above = pprev["close"] > sup and prev["close"] > sup
            now_below = last["close"] < sup
            strong_body = (last["open"] - last["close"]) > (last["high"] - last["low"]) * 0.5
            if was_above and now_below and strong_body:
                signals.append("BEARISH_BREAK")
                break

        return signals

    # ─── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _nearest_below(levels: list[float], price: float) -> Optional[float]:
        below = [l for l in levels if l < price]
        return max(below) if below else None

    @staticmethod
    def _nearest_above(levels: list[float], price: float) -> Optional[float]:
        above = [l for l in levels if l > price]
        return min(above) if above else None
