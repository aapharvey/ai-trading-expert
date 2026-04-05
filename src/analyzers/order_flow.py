"""
Block 3: Order Flow Analyzer.
Analyzes Open Interest, CVD, Funding Rate, and Liquidation zones.
"""

from dataclasses import dataclass, field
from typing import Optional

import requests

import config
from logger import get_logger

log = get_logger(__name__)

_COINGLASS_LIQUIDATION_URL = "https://open-api.coinglass.com/public/v2/liquidation_map"
_REQUEST_TIMEOUT = 8


@dataclass
class OrderFlowResult:
    # Open Interest
    oi_current:    Optional[float] = None
    oi_change_1h:  Optional[float] = None   # % change over last hour
    oi_change_4h:  Optional[float] = None
    oi_class:      Optional[str]   = None   # LONG_BUILDUP, SHORT_BUILDUP, etc.

    # CVD
    cvd_current:   Optional[float] = None   # cumulative sum of (buy_vol - sell_vol)
    cvd_trend:     Optional[str]   = None   # "RISING", "FALLING", "NEUTRAL"
    cvd_divergence: Optional[str]  = None   # "BULL", "BEAR", None

    # Funding Rate
    funding_rate:  Optional[float] = None   # latest, in %
    funding_trend: Optional[str]   = None   # "RISING", "FALLING", "NEUTRAL"

    # Liquidation zones
    liq_zone_above: Optional[float] = None  # nearest liq zone above price
    liq_zone_below: Optional[float] = None  # nearest liq zone below price
    liq_above_pct:  Optional[float] = None  # % distance above
    liq_below_pct:  Optional[float] = None  # % distance below

    signals: list[str] = field(default_factory=list)
    current_price: float = 0.0


class OrderFlowAnalyzer:
    """
    Analyzes OI, CVD, Funding Rate, Liquidation zones.

    Inputs:
      - oi_history: list of {timestamp, open_interest} — from BybitClient.get_open_interest()
      - candles:    list of OHLCV dicts with volume — for CVD approximation
      - funding_history: list of {timestamp, funding_rate, mark_price} — from BybitClient
      - current_price: float

    Liquidation zones: fetched from Coinglass public API (graceful fallback on failure).
    """

    def analyze(
        self,
        oi_history: list[dict],
        candles: list[dict],
        funding_history: list[dict],
        current_price: float,
    ) -> OrderFlowResult:

        result = OrderFlowResult(current_price=current_price)

        # OI analysis
        if oi_history:
            result.oi_current   = oi_history[-1]["open_interest"] if oi_history else None
            result.oi_change_1h = self._oi_change_pct(oi_history, periods=1)
            result.oi_change_4h = self._oi_change_pct(oi_history, periods=4)
            result.oi_class     = self._classify_oi(result.oi_change_1h, candles, current_price)

        # CVD analysis
        if candles:
            result.cvd_current  = self._calculate_cvd(candles)
            result.cvd_trend    = self._cvd_trend(candles)
            result.cvd_divergence = self._detect_cvd_divergence(candles)

        # Funding rate analysis
        if funding_history:
            result.funding_rate  = funding_history[-1]["funding_rate"]
            result.funding_trend = self._funding_trend(funding_history)

        # Liquidation zones (Coinglass — graceful fallback)
        liq = self._fetch_liquidation_zones(current_price)
        result.liq_zone_above = liq.get("above")
        result.liq_zone_below = liq.get("below")
        if result.liq_zone_above and current_price:
            result.liq_above_pct = (result.liq_zone_above - current_price) / current_price * 100
        if result.liq_zone_below and current_price:
            result.liq_below_pct = (current_price - result.liq_zone_below) / current_price * 100

        result.signals = self._generate_signals(result)

        log.debug(
            "OrderFlow: OI=%s (%.2f%% 1h) fund=%.4f%% signals=%s",
            result.oi_class,
            result.oi_change_1h or 0,
            result.funding_rate or 0,
            result.signals,
        )
        return result

    # ─── Open Interest ────────────────────────────────────────────────────────

    def _oi_change_pct(self, oi_history: list[dict], periods: int) -> Optional[float]:
        """Calculate OI % change over last `periods` data points."""
        if len(oi_history) <= periods:
            return None
        current = oi_history[-1]["open_interest"]
        past    = oi_history[-1 - periods]["open_interest"]
        if past == 0:
            return None
        return (current - past) / past * 100

    def _classify_oi(
        self,
        oi_change_1h: Optional[float],
        candles: list[dict],
        current_price: float,
    ) -> Optional[str]:
        """
        OI classification:
          OI rising  + price rising  = LONG_BUILDUP
          OI rising  + price falling = SHORT_BUILDUP
          OI falling + price falling = LONG_UNWIND
          OI falling + price rising  = SHORT_UNWIND
        """
        if oi_change_1h is None or not candles:
            return None

        # Price direction: compare last close to 4 candles ago
        if len(candles) < 5:
            return None
        price_change = (candles[-1]["close"] - candles[-5]["close"]) / candles[-5]["close"] * 100

        oi_rising    = oi_change_1h > 0
        price_rising = price_change > 0

        if oi_rising and price_rising:
            return "LONG_BUILDUP"
        if oi_rising and not price_rising:
            return "SHORT_BUILDUP"
        if not oi_rising and not price_rising:
            return "LONG_UNWIND"
        return "SHORT_UNWIND"

    # ─── CVD ─────────────────────────────────────────────────────────────────

    def _calculate_cvd(self, candles: list[dict]) -> float:
        """
        Approximate CVD from OHLCV:
        Bullish candle (close > open): +volume
        Bearish candle (close < open): -volume
        Returns cumulative sum of last 20 candles.
        """
        recent = candles[-20:]
        delta = sum(
            c["volume"] if c["close"] >= c["open"] else -c["volume"]
            for c in recent
        )
        return delta

    def _cvd_trend(self, candles: list[dict]) -> str:
        """Compare CVD of last 5 candles vs previous 5."""
        if len(candles) < 10:
            return "NEUTRAL"

        recent_cvd = sum(
            c["volume"] if c["close"] >= c["open"] else -c["volume"]
            for c in candles[-5:]
        )
        prior_cvd = sum(
            c["volume"] if c["close"] >= c["open"] else -c["volume"]
            for c in candles[-10:-5]
        )
        if recent_cvd > prior_cvd * 1.1:
            return "RISING"
        if recent_cvd < prior_cvd * 0.9:
            return "FALLING"
        return "NEUTRAL"

    def _detect_cvd_divergence(self, candles: list[dict], lookback: int = 20) -> Optional[str]:
        """
        Detect CVD/price divergence:
        Bullish: price making lower lows, but CVD making higher lows (buyers absorbing).
        Bearish: price making higher highs, but CVD making lower highs.
        """
        if len(candles) < lookback:
            return None

        recent = candles[-lookback:]
        mid = lookback // 2

        # Price trend
        price_first_half_close  = [c["close"] for c in recent[:mid]]
        price_second_half_close = [c["close"] for c in recent[mid:]]

        # CVD per candle
        def cvd_val(c: dict) -> float:
            return c["volume"] if c["close"] >= c["open"] else -c["volume"]

        cvd_first_half  = [cvd_val(c) for c in recent[:mid]]
        cvd_second_half = [cvd_val(c) for c in recent[mid:]]

        price_falling = min(price_second_half_close) < min(price_first_half_close)
        price_rising  = max(price_second_half_close) > max(price_first_half_close)

        cvd_improving  = min(cvd_second_half) > min(cvd_first_half)
        cvd_weakening  = max(cvd_second_half) < max(cvd_first_half)

        if price_falling and cvd_improving:
            return "BULL"
        if price_rising and cvd_weakening:
            return "BEAR"
        return None

    # ─── Funding Rate ─────────────────────────────────────────────────────────

    def _funding_trend(self, funding_history: list[dict]) -> str:
        if len(funding_history) < 2:
            return "NEUTRAL"
        delta = funding_history[-1]["funding_rate"] - funding_history[-2]["funding_rate"]
        if delta > 0.005:
            return "RISING"
        if delta < -0.005:
            return "FALLING"
        return "NEUTRAL"

    # ─── Liquidation zones ───────────────────────────────────────────────────

    def _fetch_liquidation_zones(self, current_price: float) -> dict:
        """
        Attempt to fetch liquidation zones from Coinglass.
        Returns {"above": float_or_None, "below": float_or_None}.
        On any error — returns empty dict (graceful fallback).
        """
        try:
            resp = requests.get(
                _COINGLASS_LIQUIDATION_URL,
                params={"symbol": "BTC", "range": "12"},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("success"):
                log.debug("Coinglass liquidation: API returned success=false")
                return {}

            # Coinglass returns price levels with liq volume
            # Find nearest clusters above and below current price
            liq_map = data.get("data", {})
            prices_above = [
                float(p) for p in liq_map
                if float(p) > current_price * 1.001
            ]
            prices_below = [
                float(p) for p in liq_map
                if float(p) < current_price * 0.999
            ]

            return {
                "above": min(prices_above) if prices_above else None,
                "below": max(prices_below) if prices_below else None,
            }

        except Exception as exc:
            log.debug("Coinglass liquidation unavailable: %s", exc)
            return {}

    # ─── Signal generation ────────────────────────────────────────────────────

    def _generate_signals(self, result: OrderFlowResult) -> list[str]:
        signals = []

        # OI signals
        if result.oi_class == "LONG_BUILDUP":
            signals.append("OI_LONG_BUILDUP")
        elif result.oi_class == "SHORT_BUILDUP":
            signals.append("OI_SHORT_BUILDUP")
        elif result.oi_class == "LONG_UNWIND":
            signals.append("OI_LONG_UNWIND")
        elif result.oi_class == "SHORT_UNWIND":
            signals.append("OI_SHORT_UNWIND")

        # CVD divergence
        if result.cvd_divergence == "BULL":
            signals.append("CVD_DIVERGENCE_BULL")
        elif result.cvd_divergence == "BEAR":
            signals.append("CVD_DIVERGENCE_BEAR")

        # Funding rate extremes
        if result.funding_rate is not None:
            if result.funding_rate > config.FUNDING_EXTREME_HIGH:
                signals.append("FUNDING_EXTREME_POSITIVE")
            elif result.funding_rate < config.FUNDING_EXTREME_LOW:
                signals.append("FUNDING_EXTREME_NEGATIVE")

        # Liquidation zone proximity
        near_pct = config.LIQUIDATION_ZONE_NEAR_PCT
        if result.liq_above_pct is not None and result.liq_above_pct <= near_pct:
            signals.append("LIQUIDATION_ZONE_NEARBY_ABOVE")
        if result.liq_below_pct is not None and result.liq_below_pct <= near_pct:
            signals.append("LIQUIDATION_ZONE_NEARBY_BELOW")

        return signals
