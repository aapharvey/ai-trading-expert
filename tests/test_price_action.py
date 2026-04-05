"""
TASK-4 tests: PriceActionAnalyzer — deterministic tests with synthetic candle data.
"""

import pytest
from src.analyzers.price_action import PriceActionAnalyzer, PriceActionResult


# ─── Candle builders ─────────────────────────────────────────────────────────

def candle(open_, high, low, close, volume=1000.0, t=0):
    return {"start_time": t, "open": open_, "high": high, "low": low, "close": close, "volume": volume}


def bullish_trend_candles(n=60, start=95000.0, step=200.0) -> list[dict]:
    """Steadily rising candles — clear bullish trend."""
    result = []
    price = start
    for i in range(n):
        o = price
        c = price + step * 0.8
        result.append(candle(o, c + step * 0.3, o - step * 0.1, c, t=i * 3600000))
        price += step
    return result


def bearish_trend_candles(n=60, start=105000.0, step=200.0) -> list[dict]:
    """Steadily falling candles — clear bearish trend."""
    result = []
    price = start
    for i in range(n):
        o = price
        c = price - step * 0.8
        result.append(candle(o, o + step * 0.1, c - step * 0.3, c, t=i * 3600000))
        price -= step
    return result


def range_candles(n=60, center=95000.0, amplitude=300.0) -> list[dict]:
    """
    Alternating up/down candles anchored to a fixed center — true range market.
    Even indices move up slightly, odd indices move down slightly.
    EMA stays flat → trend = RANGE.
    """
    result = []
    for i in range(n):
        # Alternate around the center so EMA stays flat
        offset = amplitude * (0.3 if i % 2 == 0 else -0.3)
        o = center + offset
        c = center - offset
        h = center + amplitude
        l = center - amplitude
        result.append(candle(o, h, l, c, volume=1000.0, t=i * 3600000))
    return result


# ─── Tests: Trend detection ───────────────────────────────────────────────────

class TestTrendDetection:
    def test_bullish_trend_detected(self):
        analyzer = PriceActionAnalyzer()
        result = analyzer.analyze(bullish_trend_candles())
        assert result.trend == "BULLISH"

    def test_bearish_trend_detected(self):
        analyzer = PriceActionAnalyzer()
        result = analyzer.analyze(bearish_trend_candles())
        assert result.trend == "BEARISH"

    def test_range_detected(self):
        analyzer = PriceActionAnalyzer()
        result = analyzer.analyze(range_candles())
        assert result.trend == "RANGE"

    def test_insufficient_candles_returns_range(self):
        analyzer = PriceActionAnalyzer()
        result = analyzer.analyze([candle(100, 110, 90, 105)] * 10)
        assert result.trend == "RANGE"
        assert result.signals == []


# ─── Tests: Key levels ────────────────────────────────────────────────────────

class TestKeyLevels:
    def test_supports_are_below_price(self):
        analyzer = PriceActionAnalyzer()
        candles = bullish_trend_candles()
        result = analyzer.analyze(candles)
        current = result.current_price
        for s in result.key_supports:
            assert s < current * 1.02, f"Support {s} is not below price {current}"

    def test_resistances_are_above_price(self):
        analyzer = PriceActionAnalyzer()
        candles = bearish_trend_candles()
        result = analyzer.analyze(candles)
        current = result.current_price
        for r in result.key_resistances:
            assert r > current * 0.98, f"Resistance {r} is not above price {current}"

    def test_max_5_supports_returned(self):
        analyzer = PriceActionAnalyzer()
        result = analyzer.analyze(bullish_trend_candles(100))
        assert len(result.key_supports) <= 5

    def test_max_5_resistances_returned(self):
        analyzer = PriceActionAnalyzer()
        result = analyzer.analyze(bearish_trend_candles(100))
        assert len(result.key_resistances) <= 5

    def test_nearest_support_is_closest_below(self):
        analyzer = PriceActionAnalyzer()
        result = analyzer.analyze(bullish_trend_candles())
        if result.nearest_support and result.key_supports:
            dist = result.current_price - result.nearest_support
            for s in result.key_supports:
                if s < result.current_price:
                    assert dist <= result.current_price - s + 0.01


# ─── Tests: Level clustering ─────────────────────────────────────────────────

class TestLevelClustering:
    def test_close_levels_merged(self):
        analyzer = PriceActionAnalyzer()
        # Two very close prices should merge into one
        prices = [95000.0, 95100.0, 95050.0]  # all within 0.5% of each other
        merged = analyzer._merge_levels(prices)
        assert len(merged) == 1
        assert abs(merged[0] - 95050.0) < 100

    def test_distant_levels_not_merged(self):
        analyzer = PriceActionAnalyzer()
        prices = [90000.0, 95000.0, 100000.0]
        merged = analyzer._merge_levels(prices)
        assert len(merged) == 3

    def test_empty_levels(self):
        analyzer = PriceActionAnalyzer()
        assert analyzer._merge_levels([]) == []


# ─── Tests: Candlestick patterns ──────────────────────────────────────────────

class TestCandlestickPatterns:
    def test_bullish_engulfing_detected(self):
        analyzer = PriceActionAnalyzer()
        prev = candle(100, 105, 95, 96)   # bearish
        curr = candle(94, 108, 93, 107)   # bullish, engulfs prev
        # Support right at current price (within 0.8% threshold)
        supports = [107.5]
        resistances = []
        patterns = analyzer._detect_patterns([prev, curr], supports, resistances, 107.0)
        assert "BULLISH_ENGULFING" in patterns

    def test_bearish_engulfing_detected(self):
        analyzer = PriceActionAnalyzer()
        prev = candle(96, 105, 94, 104)   # bullish
        curr = candle(106, 107, 93, 94)   # bearish, engulfs prev
        # Resistance right at current price (within 0.8%)
        resistances = [94.5]
        patterns = analyzer._detect_patterns([prev, curr], [], resistances, 94.0)
        assert "BEARISH_ENGULFING" in patterns

    def test_bullish_pin_bar_detected(self):
        analyzer = PriceActionAnalyzer()
        # Long lower wick: open=100, close=100.5, low=90, high=101 → wick=10, body=0.5
        c = candle(100, 101, 90, 100.5)
        # Support within 0.8% of 100.5
        supports = [100.8]
        patterns = analyzer._detect_patterns(
            [candle(100, 101, 99, 100), c], supports, [], 100.5
        )
        assert "BULLISH_PIN_BAR" in patterns

    def test_doji_detected(self):
        analyzer = PriceActionAnalyzer()
        # Almost equal open/close
        c = candle(100.0, 105.0, 95.0, 100.1)  # body = 0.1, range = 10
        supports = [100.0]
        patterns = analyzer._detect_patterns(
            [candle(99, 100, 98, 99), c], supports, [], 100.1
        )
        assert "DOJI" in patterns

    def test_no_pattern_far_from_levels(self):
        analyzer = PriceActionAnalyzer()
        prev = candle(100, 105, 95, 96)
        curr = candle(94, 108, 93, 107)
        # Level is far from current price
        patterns = analyzer._detect_patterns([prev, curr], [50000.0], [], 107.0)
        assert "BULLISH_ENGULFING" not in patterns


# ─── Tests: Breakout detection ────────────────────────────────────────────────

class TestBreakoutDetection:
    def test_bullish_breakout_detected(self):
        analyzer = PriceActionAnalyzer()
        # 3 candles: two below resistance, last breaks above
        resistances = [100.0]
        candles_ = [
            candle(96, 99, 95, 98),   # below resistance
            candle(97, 99, 96, 99),   # below resistance
            candle(99, 106, 98, 105), # strong bullish break above 100
        ]
        signals = analyzer._detect_breakouts(candles_, [], resistances)
        assert "BULLISH_BREAK" in signals

    def test_bearish_breakout_detected(self):
        analyzer = PriceActionAnalyzer()
        supports = [100.0]
        candles_ = [
            candle(104, 105, 101, 103),  # above support
            candle(103, 104, 101, 102),  # above support
            candle(101, 102, 95, 96),    # strong bearish break below 100
        ]
        signals = analyzer._detect_breakouts(candles_, supports, [])
        assert "BEARISH_BREAK" in signals

    def test_no_breakout_weak_body(self):
        analyzer = PriceActionAnalyzer()
        resistances = [100.0]
        # Last candle barely closes above — but has huge wick (weak body)
        candles_ = [
            candle(96, 99, 95, 98),
            candle(97, 99, 96, 99),
            candle(99, 110, 98, 100.5),  # close above but tiny body vs range
        ]
        signals = analyzer._detect_breakouts(candles_, [], resistances)
        assert "BULLISH_BREAK" not in signals


# ─── Tests: AT_KEY signals ────────────────────────────────────────────────────

class TestAtKeySignals:
    def test_at_key_support_signal(self):
        analyzer = PriceActionAnalyzer()
        # Price sitting right on support
        current = 95050.0
        supports = [95000.0]
        resistances = [98000.0]
        signals = analyzer._generate_signals(
            "BULLISH", current, supports, resistances, [], [], 95000.0, 98000.0
        )
        assert "AT_KEY_SUPPORT" in signals

    def test_at_key_resistance_signal(self):
        analyzer = PriceActionAnalyzer()
        current = 97950.0
        supports = [95000.0]
        resistances = [98000.0]
        signals = analyzer._generate_signals(
            "BULLISH", current, supports, resistances, [], [], 95000.0, 98000.0
        )
        assert "AT_KEY_RESISTANCE" in signals

    def test_range_bound_signal(self):
        analyzer = PriceActionAnalyzer()
        signals = analyzer._generate_signals(
            "RANGE", 95000.0, [], [], [], [], None, None
        )
        assert "RANGE_BOUND" in signals


# ─── Tests: Full pipeline ────────────────────────────────────────────────────

class TestFullPipeline:
    def test_result_has_required_fields(self):
        analyzer = PriceActionAnalyzer()
        result = analyzer.analyze(bullish_trend_candles())
        assert isinstance(result, PriceActionResult)
        assert result.current_price > 0
        assert isinstance(result.signals, list)
        assert isinstance(result.patterns, list)
        assert isinstance(result.key_supports, list)
        assert isinstance(result.key_resistances, list)

    def test_current_price_equals_last_close(self):
        analyzer = PriceActionAnalyzer()
        candles = bullish_trend_candles()
        result = analyzer.analyze(candles)
        assert result.current_price == candles[-1]["close"]

    def test_wyckoff_markup_in_bullish_expanding_volume(self):
        analyzer = PriceActionAnalyzer()
        candles = bullish_trend_candles(60, step=300)
        # Inflate recent volume
        for i in range(50, 60):
            candles[i]["volume"] = 5000.0
        result = analyzer.analyze(candles)
        assert result.wyckoff_phase == "MARKUP"
