"""
Confluence Engine — aggregates signals from all blocks and emits TradeSignals.

Logic:
  1. Collect all raw signals from PriceAction, Technical, OrderFlow,
     Sentiment (optional), OnChain (optional)
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
from src.analyzers.liquidity import LiquidityResult
from src.analyzers.volume_profile import VolumeProfileResult
from src.models.signals import Direction, Timeframe, TradeSignal, SentimentResult, OnChainResult

log = get_logger(__name__)


# Signals that contribute to LONG direction
_LONG_SIGNALS = {
    # Block 1 — Price Action
    "BULLISH_BREAK", "AT_KEY_SUPPORT",
    # Block 2 — Technical
    "EMA_CROSS_UP", "PRICE_ABOVE_EMA200",
    "RSI_OVERSOLD", "MACD_CROSS_UP", "MACD_DIVERGENCE_BULL",
    "BB_BREAKOUT_UP",
    # Block 3 — Order Flow
    "OI_LONG_BUILDUP", "OI_SHORT_UNWIND",
    "CVD_DIVERGENCE_BULL",
    "FUNDING_EXTREME_NEGATIVE",
    "LIQUIDATION_ZONE_NEARBY_ABOVE",
    # Block 4 — Sentiment (contrarian: fear → buy)
    "EXTREME_FEAR", "FEAR",
    # Block 5 — On-chain
    "EXCHANGE_OUTFLOW_SPIKE", "WHALE_ACCUMULATION", "MVRV_BOTTOM_SIGNAL",
    # Block 6 — News
    "NEWS_BULLISH_MAJOR",
    # Block 7 — Liquidity Map
    "ORDER_WALL_BELOW", "DELTA_BULL",
    # Block 8 — Volume Profile
    "PRICE_AT_POC_FROM_BELOW", "ABOVE_VALUE_AREA", "NAKED_POC_ABOVE",
}

# Signals that contribute to SHORT direction
_SHORT_SIGNALS = {
    # Block 1
    "BEARISH_BREAK", "AT_KEY_RESISTANCE",
    # Block 2
    "EMA_CROSS_DOWN", "PRICE_BELOW_EMA200",
    "RSI_OVERBOUGHT", "MACD_CROSS_DOWN", "MACD_DIVERGENCE_BEAR",
    "BB_BREAKOUT_DOWN",
    # Block 3
    "OI_SHORT_BUILDUP", "OI_LONG_UNWIND",
    "CVD_DIVERGENCE_BEAR",
    "FUNDING_EXTREME_POSITIVE",
    "LIQUIDATION_ZONE_NEARBY_BELOW",
    # Block 4 — Sentiment (contrarian: greed → sell)
    "EXTREME_GREED", "GREED",
    # Block 5 — On-chain
    "EXCHANGE_INFLOW_SPIKE", "MVRV_TOP_SIGNAL",
    # Block 6 — News
    "NEWS_BEARISH_MAJOR",
    # Block 7 — Liquidity Map
    "ORDER_WALL_ABOVE", "DELTA_BEAR",
    # Block 8 — Volume Profile
    "PRICE_AT_POC_FROM_ABOVE", "BELOW_VALUE_AREA", "NAKED_POC_BELOW",
}

# Maximum possible score (sum of all weights for one direction) — for reference only
_MAX_SCORE = sum(
    w for sig, w in config.SIGNAL_WEIGHTS.items()
    if sig in _LONG_SIGNALS
)

# Realistic normalization scale: expected max raw score per cycle (top 5 concurrent signals)
_NORM_SCALE = 4.0


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
        pa:             PriceActionResult,
        tech:           TechnicalResult,
        of:             OrderFlowResult,
        sentiment:      Optional[SentimentResult]     = None,
        on_chain:       Optional[OnChainResult]       = None,
        liquidity:      Optional[LiquidityResult]     = None,
        volume_profile: Optional[VolumeProfileResult] = None,
        now:            Optional[datetime]            = None,
        norm_scale:     Optional[float]               = None,
        tp2_multiplier: Optional[float]               = None,
        min_strength:   Optional[int]                 = None,
        trend_filter_1d: bool                         = False,
    ) -> Optional[TradeSignal]:
        """
        Main entry point. Returns a TradeSignal or None.
        All args after `of` are optional — backward compatible.

        now: override "current time" for anti-spam checks (pass candle time in backtest).
             If None, uses datetime.now(timezone.utc) — production behavior unchanged.
        norm_scale: override normalization scale for scoring (pass 2.5 in backtest).
                    If None, uses module-level _NORM_SCALE=4.0 — production behavior unchanged.
        tp2_multiplier: override TP2 ATR multiplier (pass 2.5 in backtest).
                        If None, uses config.TP2_ATR_MULTIPLIER=3.5 — production behavior unchanged.
        min_strength: override minimum signal strength threshold (pass 4 in backtest).
                      If None, uses config.MIN_SIGNAL_STRENGTH=3 — production behavior unchanged.
        trend_filter_1d: when True, also require 1D EMA200 alignment (backtest only).
                         Expects PRICE_ABOVE_EMA200_1D / PRICE_BELOW_EMA200_1D in signals.
        """
        now = now or datetime.now(timezone.utc)
        all_signals = (
            pa.signals
            + tech.signals
            + of.signals
            + (sentiment.signals      if sentiment      else [])
            + (on_chain.signals       if on_chain       else [])
            + (liquidity.signals      if liquidity      else [])
            + (volume_profile.signals if volume_profile else [])
        )

        long_score, long_factors   = self._score(all_signals, _LONG_SIGNALS)
        short_score, short_factors = self._score(all_signals, _SHORT_SIGNALS)

        long_strength  = self._normalize_score(long_score, norm_scale)
        short_strength = self._normalize_score(short_score, norm_scale)

        log.debug(
            "Confluence: LONG=%.2f(%d/5) SHORT=%.2f(%d/5) signals=%s",
            long_score, long_strength, short_score, short_strength, all_signals,
        )

        # Determine winning direction
        _min_strength = min_strength if min_strength is not None else config.MIN_SIGNAL_STRENGTH
        if long_strength >= short_strength and long_strength >= _min_strength:
            direction = Direction.LONG
            strength  = long_strength
            factors   = long_factors
        elif short_strength > long_strength and short_strength >= _min_strength:
            direction = Direction.SHORT
            strength  = short_strength
            factors   = short_factors
        else:
            return None  # No strong enough signal

        # Trend filter: only trade in direction of EMA200 trend (1H)
        if direction == Direction.LONG and "PRICE_ABOVE_EMA200" not in all_signals:
            log.debug("Confluence: LONG suppressed — price not above EMA200 (1H)")
            return None
        if direction == Direction.SHORT and "PRICE_BELOW_EMA200" not in all_signals:
            log.debug("Confluence: SHORT suppressed — price not below EMA200 (1H)")
            return None

        # Trend filter: 1D EMA200 alignment (backtest only, opt-in)
        if trend_filter_1d:
            if direction == Direction.LONG and "PRICE_ABOVE_EMA200_1D" not in all_signals:
                log.debug("Confluence: LONG suppressed — price not above EMA200 (1D)")
                return None
            if direction == Direction.SHORT and "PRICE_BELOW_EMA200_1D" not in all_signals:
                log.debug("Confluence: SHORT suppressed — price not below EMA200 (1D)")
                return None

        # Anti-spam check
        if not self._can_send(direction, now):
            log.debug("Confluence: anti-spam suppressed %s signal", direction)
            return None

        # Build trade levels
        signal = self._build_signal(
            direction, strength, factors, pa, tech, liquidity, volume_profile,
            tp2_multiplier=tp2_multiplier,
        )
        if signal is None:
            return None

        # R:R check
        if signal.rr_ratio < config.MIN_RR_RATIO:
            log.debug(
                "Confluence: R:R %.2f < minimum %.2f — signal suppressed",
                signal.rr_ratio, config.MIN_RR_RATIO,
            )
            return None

        # Record last signal time (uses candle time in backtest, real time in production)
        self._last_signal[direction] = now
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

    def _normalize_score(self, raw: float, norm_scale: Optional[float] = None) -> int:
        """Map raw weighted score to 1–5 strength.

        norm_scale: override for backtest (e.g. 2.5 for 4-block runs).
                    Production always uses _NORM_SCALE=4.0.
        TODO: compute norm_scale dynamically from sum of weights of active blocks.
        """
        if raw <= 0:
            return 0
        scale = norm_scale if norm_scale is not None else _NORM_SCALE
        pct = raw / scale
        return min(5, max(1, round(pct * 5)))

    # ─── Trade level construction ─────────────────────────────────────────────

    def _build_signal(
        self,
        direction:      Direction,
        strength:       int,
        factors:        list[str],
        pa:             PriceActionResult,
        tech:           TechnicalResult,
        liquidity:      Optional[LiquidityResult]     = None,
        volume_profile: Optional[VolumeProfileResult] = None,
        tp2_multiplier: Optional[float]               = None,
    ) -> Optional[TradeSignal]:
        """Construct entry zone, SL, TP using liquidity/VP levels + price action."""

        price = pa.current_price or tech.current_price
        if not price:
            return None

        atr = tech.atr or price * 0.005  # fallback: 0.5% of price
        _tp2_mult = tp2_multiplier if tp2_multiplier is not None else config.TP2_ATR_MULTIPLIER

        # ── Entry mid: priority chain ──────────────────────────────────────────
        # 1. Nearest Order Wall within 1.5% of price (liquidity magnet)
        # 2. POC or Naked POC within 2% of price (volume profile)
        # 3. Nearest support / resistance (legacy)
        # 4. Current price (final fallback)
        entry_mid, entry_source = self._resolve_entry_mid(
            price, direction, pa, liquidity, volume_profile
        )
        log.debug(
            "Confluence: entry_mid=$%.0f source=%s direction=%s",
            entry_mid, entry_source, direction,
        )

        entry_low  = entry_mid * 0.9985
        entry_high = entry_mid * 1.0015

        if direction == Direction.LONG:
            stop_loss = entry_mid - atr * config.SL_ATR_MULTIPLIER
            tp1       = entry_mid + atr * config.TP1_ATR_MULTIPLIER
            tp2       = entry_mid + atr * _tp2_mult
        else:
            stop_loss = entry_mid + atr * config.SL_ATR_MULTIPLIER
            tp1       = entry_mid - atr * config.TP1_ATR_MULTIPLIER
            tp2       = entry_mid - atr * _tp2_mult

        # Guard: entry zone must bracket current price
        if direction == Direction.LONG and entry_high < price:
            log.debug(
                "Confluence: entry zone $%.0f–$%.0f below current price $%.0f — suppressed",
                entry_low, entry_high, price,
            )
            return None
        if direction == Direction.SHORT and entry_low > price:
            log.debug(
                "Confluence: entry zone $%.0f–$%.0f above current price $%.0f — suppressed",
                entry_low, entry_high, price,
            )
            return None

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

    # ─── Entry mid resolution ────────────────────────────────────────────────

    def _resolve_entry_mid(
        self,
        price:          float,
        direction:      Direction,
        pa:             PriceActionResult,
        liquidity:      Optional[LiquidityResult],
        volume_profile: Optional[VolumeProfileResult],
    ) -> tuple[float, str]:
        """
        Resolve entry_mid using priority chain.
        Returns (entry_mid, source_label).
        """
        scan_pct_wall = 0.015   # 1.5% for order walls
        scan_pct_vp   = 0.02    # 2.0% for volume profile levels

        # 1. Order Wall
        if liquidity:
            walls = liquidity.bid_walls if direction == Direction.LONG else liquidity.ask_walls
            for wall in walls:
                if abs(wall.price - price) / price <= scan_pct_wall:
                    return wall.price, "order_wall"

        # 2. Volume Profile — POC first, then Naked POC
        if volume_profile:
            for level, label in [
                (volume_profile.poc,       "poc"),
                (volume_profile.naked_poc, "naked_poc"),
            ]:
                if level and abs(level - price) / price <= scan_pct_vp:
                    return level, label

        # 3. Nearest support / resistance (only if within 1.5% of price)
        if direction == Direction.LONG and pa.nearest_support:
            if abs(pa.nearest_support - price) / price <= 0.015:
                return pa.nearest_support, "support"
        if direction == Direction.SHORT and pa.nearest_resistance:
            if abs(pa.nearest_resistance - price) / price <= 0.015:
                return pa.nearest_resistance, "resistance"

        # 4. Current price fallback
        return price, "current_price"

    # ─── Anti-spam ────────────────────────────────────────────────────────────

    def _can_send(self, direction: Direction, now: datetime) -> bool:
        last = self._last_signal.get(direction)
        if last is None:
            return True
        elapsed = now - last
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
            # Sentiment (Block 4)
            "EXTREME_FEAR":                  "Fear & Greed: Extreme Fear (contrarian long)",
            "FEAR":                           "Fear & Greed: Fear zone",
            "EXTREME_GREED":                 "Fear & Greed: Extreme Greed (contrarian short)",
            "GREED":                          "Fear & Greed: Greed zone",
            # On-chain (Block 5)
            "EXCHANGE_INFLOW_SPIKE":         "Exchange inflow spike (sell pressure)",
            "EXCHANGE_OUTFLOW_SPIKE":        "Exchange outflow spike (accumulation)",
            "WHALE_ACCUMULATION":            "Whale accumulation detected",
            "MVRV_BOTTOM_SIGNAL":            "MVRV < 1.0 (market below realized cap — capitulation)",
            "MVRV_TOP_SIGNAL":               "MVRV > 3.5 (historically overbought — potential top)",
            # News (Block 6)
            "NEWS_BULLISH_MAJOR":            "Majority of recent news is bullish",
            "NEWS_BEARISH_MAJOR":            "Majority of recent news is bearish",
            "HIGH_IMPACT_EVENT_APPROACHING": "High-impact macro event approaching",
            # Liquidity Map (Block 7)
            "ORDER_WALL_BELOW":              "Large bid wall below price (liquidity support)",
            "ORDER_WALL_ABOVE":              "Large ask wall above price (liquidity resistance)",
            "DELTA_BULL":                    "Buyers dominating recent trades (positive delta)",
            "DELTA_BEAR":                    "Sellers dominating recent trades (negative delta)",
            # Volume Profile (Block 8)
            "PRICE_AT_POC_FROM_BELOW":       "Price returning to POC from below (mean reversion LONG)",
            "PRICE_AT_POC_FROM_ABOVE":       "Price returning to POC from above (mean reversion SHORT)",
            "ABOVE_VALUE_AREA":              "Price broke above Value Area High (bullish breakout)",
            "BELOW_VALUE_AREA":              "Price broke below Value Area Low (bearish breakdown)",
            "NAKED_POC_ABOVE":               "Unvisited POC above price (upside magnet)",
            "NAKED_POC_BELOW":               "Unvisited POC below price (downside magnet)",
        }
        return labels.get(signal, signal)
