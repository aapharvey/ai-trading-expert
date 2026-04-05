"""
Block 2: Technical Indicators Analyzer.
Calculates EMA, RSI, MACD, Bollinger Bands, ATR and generates signals.
Uses the 'ta' library for indicator calculations.
"""

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import ta

import config
from logger import get_logger

log = get_logger(__name__)


@dataclass
class TechnicalResult:
    # Current indicator values
    ema_21:    Optional[float] = None
    ema_55:    Optional[float] = None
    ema_200:   Optional[float] = None
    rsi:       Optional[float] = None
    macd:      Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    bb_upper:  Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower:  Optional[float] = None
    bb_width:  Optional[float] = None   # (upper-lower)/middle
    atr:       Optional[float] = None

    # Computed trade levels (ATR-based)
    sl_long:   Optional[float] = None   # SL for long entry
    sl_short:  Optional[float] = None   # SL for short entry
    tp1_long:  Optional[float] = None
    tp2_long:  Optional[float] = None
    tp1_short: Optional[float] = None
    tp2_short: Optional[float] = None

    signals: list[str] = field(default_factory=list)
    current_price: float = 0.0


class TechnicalAnalyzer:
    """
    Calculates technical indicators from OHLCV candle data and emits signals.

    Signal list:
      EMA_CROSS_UP, EMA_CROSS_DOWN          — EMA 21 crosses EMA 55
      PRICE_ABOVE_EMA200, PRICE_BELOW_EMA200 — trend filter
      RSI_OVERSOLD, RSI_OVERBOUGHT           — momentum extremes
      MACD_CROSS_UP, MACD_CROSS_DOWN        — MACD histogram sign change
      MACD_DIVERGENCE_BULL, MACD_DIVERGENCE_BEAR
      BB_SQUEEZE                             — bands tightening
      BB_BREAKOUT_UP, BB_BREAKOUT_DOWN       — price outside bands
    """

    def analyze(self, candles: list[dict]) -> TechnicalResult:
        if len(candles) < 26:
            log.warning("Technical: insufficient candles (%d)", len(candles))
            return TechnicalResult()

        df = self._to_dataframe(candles)
        df = self._add_indicators(df)

        current_price = float(df["close"].iloc[-1])
        result = self._extract_values(df, current_price)
        result.signals = self._generate_signals(df, result, current_price)

        log.debug(
            "Technical: price=%.2f rsi=%.1f ema21=%.2f signals=%s",
            current_price,
            result.rsi or 0,
            result.ema_21 or 0,
            result.signals,
        )
        return result

    # ─── DataFrame ───────────────────────────────────────────────────────────

    @staticmethod
    def _to_dataframe(candles: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(candles)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.reset_index(drop=True)

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # EMAs
        df["ema_21"]  = ta.trend.ema_indicator(close, window=21)
        df["ema_55"]  = ta.trend.ema_indicator(close, window=55)
        df["ema_200"] = ta.trend.ema_indicator(close, window=200)

        # RSI
        df["rsi"] = ta.momentum.rsi(close, window=config.RSI_PERIOD)

        # MACD
        macd_obj = ta.trend.MACD(
            close,
            window_fast=config.MACD_FAST,
            window_slow=config.MACD_SLOW,
            window_sign=config.MACD_SIGNAL,
        )
        df["macd"]        = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        df["macd_hist"]   = macd_obj.macd_diff()

        # Bollinger Bands
        bb = ta.volatility.BollingerBands(
            close, window=config.BB_PERIOD, window_dev=config.BB_STD
        )
        df["bb_upper"]  = bb.bollinger_hband()
        df["bb_middle"] = bb.bollinger_mavg()
        df["bb_lower"]  = bb.bollinger_lband()

        # ATR
        df["atr"] = ta.volatility.average_true_range(
            high, low, close, window=config.ATR_PERIOD
        )

        return df

    # ─── Value extraction ────────────────────────────────────────────────────

    def _extract_values(self, df: pd.DataFrame, price: float) -> TechnicalResult:
        def last(col: str) -> Optional[float]:
            s = df[col].dropna()
            return float(s.iloc[-1]) if not s.empty else None

        atr = last("atr")

        result = TechnicalResult(
            ema_21      = last("ema_21"),
            ema_55      = last("ema_55"),
            ema_200     = last("ema_200"),
            rsi         = last("rsi"),
            macd        = last("macd"),
            macd_signal = last("macd_signal"),
            macd_hist   = last("macd_hist"),
            bb_upper    = last("bb_upper"),
            bb_middle   = last("bb_middle"),
            bb_lower    = last("bb_lower"),
            atr         = atr,
            current_price = price,
        )

        if result.bb_upper and result.bb_lower and result.bb_middle:
            result.bb_width = (result.bb_upper - result.bb_lower) / result.bb_middle

        if atr:
            result.sl_long   = price - atr * config.SL_ATR_MULTIPLIER
            result.sl_short  = price + atr * config.SL_ATR_MULTIPLIER
            result.tp1_long  = price + atr * config.TP1_ATR_MULTIPLIER
            result.tp2_long  = price + atr * config.TP2_ATR_MULTIPLIER
            result.tp1_short = price - atr * config.TP1_ATR_MULTIPLIER
            result.tp2_short = price - atr * config.TP2_ATR_MULTIPLIER

        return result

    # ─── Signal generation ───────────────────────────────────────────────────

    def _generate_signals(
        self, df: pd.DataFrame, result: TechnicalResult, price: float
    ) -> list[str]:
        signals = []

        # EMA cross (21 / 55)
        ema_cross = self._detect_ema_cross(df)
        if ema_cross:
            signals.append(ema_cross)

        # EMA 200 trend filter
        if result.ema_200:
            if price > result.ema_200:
                signals.append("PRICE_ABOVE_EMA200")
            else:
                signals.append("PRICE_BELOW_EMA200")

        # RSI
        if result.rsi is not None:
            if result.rsi < config.RSI_OVERSOLD:
                signals.append("RSI_OVERSOLD")
            elif result.rsi > config.RSI_OVERBOUGHT:
                signals.append("RSI_OVERBOUGHT")

        # MACD cross
        macd_cross = self._detect_macd_cross(df)
        if macd_cross:
            signals.append(macd_cross)

        # MACD divergence
        div = self._detect_macd_divergence(df)
        if div:
            signals.append(div)

        # Bollinger Bands
        if result.bb_width is not None:
            if result.bb_width < config.BB_SQUEEZE_THRESHOLD:
                signals.append("BB_SQUEEZE")

        if result.bb_upper and price > result.bb_upper:
            signals.append("BB_BREAKOUT_UP")
        if result.bb_lower and price < result.bb_lower:
            signals.append("BB_BREAKOUT_DOWN")

        return signals

    def _detect_ema_cross(self, df: pd.DataFrame) -> Optional[str]:
        """Detect EMA 21/55 crossover on last two candles."""
        ema21 = df["ema_21"].dropna()
        ema55 = df["ema_55"].dropna()
        if len(ema21) < 2 or len(ema55) < 2:
            return None

        # Align by index
        common = ema21.index.intersection(ema55.index)
        if len(common) < 2:
            return None

        prev_21, curr_21 = ema21.loc[common[-2]], ema21.loc[common[-1]]
        prev_55, curr_55 = ema55.loc[common[-2]], ema55.loc[common[-1]]

        if prev_21 < prev_55 and curr_21 > curr_55:
            return "EMA_CROSS_UP"
        if prev_21 > prev_55 and curr_21 < curr_55:
            return "EMA_CROSS_DOWN"
        return None

    def _detect_macd_cross(self, df: pd.DataFrame) -> Optional[str]:
        """Detect MACD histogram sign change (proxy for MACD/signal line cross)."""
        hist = df["macd_hist"].dropna()
        if len(hist) < 2:
            return None

        prev_h = hist.iloc[-2]
        curr_h = hist.iloc[-1]

        if prev_h < 0 and curr_h >= 0:
            return "MACD_CROSS_UP"
        if prev_h > 0 and curr_h <= 0:
            return "MACD_CROSS_DOWN"
        return None

    def _detect_macd_divergence(self, df: pd.DataFrame, lookback: int = 20) -> Optional[str]:
        """
        Detect regular divergence between price and MACD histogram.
        Bullish: price making lower lows, MACD making higher lows.
        Bearish: price making higher highs, MACD making lower highs.
        """
        close = df["close"].iloc[-lookback:]
        hist  = df["macd_hist"].iloc[-lookback:].dropna()

        if len(close) < lookback or len(hist) < lookback // 2:
            return None

        price_ll = close.iloc[-1] < close.min() * 1.005  # near lowest
        price_hh = close.iloc[-1] > close.max() * 0.995  # near highest

        # MACD direction vs price direction
        macd_min_idx = hist.idxmin()
        macd_max_idx = hist.idxmax()

        # Bullish divergence: price at new low, MACD histogram making higher low
        if price_ll:
            mid = len(hist) // 2
            hist_first_half_min  = hist.iloc[:mid].min()
            hist_second_half_min = hist.iloc[mid:].min()
            if hist_second_half_min > hist_first_half_min:
                return "MACD_DIVERGENCE_BULL"

        # Bearish divergence: price at new high, MACD histogram making lower high
        if price_hh:
            mid = len(hist) // 2
            hist_first_half_max  = hist.iloc[:mid].max()
            hist_second_half_max = hist.iloc[mid:].max()
            if hist_second_half_max < hist_first_half_max:
                return "MACD_DIVERGENCE_BEAR"

        return None
