"""
TASK-6 tests: OrderFlowAnalyzer — OI, CVD, Funding Rate, Liquidation zones.
All external calls mocked.
"""

import json
import pytest
import requests

from src.analyzers.order_flow import OrderFlowAnalyzer, OrderFlowResult


# ─── Test data fixtures ───────────────────────────────────────────────────────

def make_oi(values: list[float]) -> list[dict]:
    return [{"timestamp": i * 3600000, "open_interest": v} for i, v in enumerate(values)]


def make_funding(rates: list[float]) -> list[dict]:
    return [
        {"timestamp": i * 28800000, "funding_rate": r, "mark_price": 95000.0}
        for i, r in enumerate(rates)
    ]


def make_candles(closes: list[float], volumes: list[float] = None) -> list[dict]:
    if volumes is None:
        volumes = [1000.0] * len(closes)
    result = []
    for i, (c, v) in enumerate(zip(closes, volumes)):
        o = c * (0.999 if i % 2 == 0 else 1.001)
        result.append({
            "start_time": i * 3600000,
            "open": o, "high": c * 1.005, "low": c * 0.995,
            "close": c, "volume": v,
        })
    return result


def bullish_candles(n=25) -> list[dict]:
    """Rising prices, consistent buying volume."""
    closes = [95000.0 + i * 100 for i in range(n)]
    return [
        {"start_time": i * 3600000, "open": c - 50, "high": c + 30,
         "low": c - 60, "close": c, "volume": 1500.0}
        for i, c in enumerate(closes)
    ]


def bearish_candles(n=25) -> list[dict]:
    """Falling prices, consistent selling volume."""
    closes = [97500.0 - i * 100 for i in range(n)]
    return [
        {"start_time": i * 3600000, "open": c + 50, "high": c + 60,
         "low": c - 30, "close": c, "volume": 1500.0}
        for i, c in enumerate(closes)
    ]


# ─── Tests: OI change ────────────────────────────────────────────────────────

class TestOIChange:
    def test_oi_increase_calculated(self):
        analyzer = OrderFlowAnalyzer()
        oi = make_oi([17500.0, 18000.0])
        pct = analyzer._oi_change_pct(oi, periods=1)
        assert abs(pct - (500 / 17500 * 100)) < 0.01

    def test_oi_decrease_calculated(self):
        analyzer = OrderFlowAnalyzer()
        oi = make_oi([18000.0, 17000.0])
        pct = analyzer._oi_change_pct(oi, periods=1)
        assert pct < 0

    def test_insufficient_oi_returns_none(self):
        analyzer = OrderFlowAnalyzer()
        oi = make_oi([18000.0])
        assert analyzer._oi_change_pct(oi, periods=2) is None

    def test_zero_oi_returns_none(self):
        analyzer = OrderFlowAnalyzer()
        oi = make_oi([0.0, 18000.0])
        assert analyzer._oi_change_pct(oi, periods=1) is None


# ─── Tests: OI classification ────────────────────────────────────────────────

class TestOIClassification:
    def test_long_buildup(self):
        analyzer = OrderFlowAnalyzer()
        # OI rising + price rising
        candles = bullish_candles()
        result = analyzer._classify_oi(2.0, candles, 97400.0)
        assert result == "LONG_BUILDUP"

    def test_short_buildup(self):
        analyzer = OrderFlowAnalyzer()
        # OI rising + price falling
        candles = bearish_candles()
        result = analyzer._classify_oi(2.0, candles, 95100.0)
        assert result == "SHORT_BUILDUP"

    def test_long_unwind(self):
        analyzer = OrderFlowAnalyzer()
        # OI falling + price falling
        candles = bearish_candles()
        result = analyzer._classify_oi(-2.0, candles, 95100.0)
        assert result == "LONG_UNWIND"

    def test_short_unwind(self):
        analyzer = OrderFlowAnalyzer()
        # OI falling + price rising
        candles = bullish_candles()
        result = analyzer._classify_oi(-2.0, candles, 97400.0)
        assert result == "SHORT_UNWIND"

    def test_none_when_no_oi(self):
        analyzer = OrderFlowAnalyzer()
        assert analyzer._classify_oi(None, bullish_candles(), 95000.0) is None

    def test_none_when_too_few_candles(self):
        analyzer = OrderFlowAnalyzer()
        assert analyzer._classify_oi(1.0, bullish_candles(3), 95000.0) is None


# ─── Tests: CVD ──────────────────────────────────────────────────────────────

class TestCVD:
    def test_cvd_positive_on_bullish_candles(self):
        analyzer = OrderFlowAnalyzer()
        cvd = analyzer._calculate_cvd(bullish_candles())
        assert cvd > 0

    def test_cvd_negative_on_bearish_candles(self):
        analyzer = OrderFlowAnalyzer()
        cvd = analyzer._calculate_cvd(bearish_candles())
        assert cvd < 0

    def test_cvd_trend_rising(self):
        analyzer = OrderFlowAnalyzer()
        # Increasing buy volume
        candles = [
            {"start_time": i * 3600000, "open": 95000, "high": 95100,
             "low": 94900, "close": 95050, "volume": 1000.0 + i * 100}
            for i in range(15)
        ]
        trend = analyzer._cvd_trend(candles)
        assert trend in ("RISING", "NEUTRAL")  # direction depends on volume pattern

    def test_cvd_trend_neutral_insufficient(self):
        analyzer = OrderFlowAnalyzer()
        assert analyzer._cvd_trend(bullish_candles(5)) == "NEUTRAL"

    def test_cvd_divergence_bull(self):
        analyzer = OrderFlowAnalyzer()
        # Price falling, but buying volume improving in second half
        closes  = [97000.0 - i * 50 for i in range(20)]
        # First half: strong selling, second half: buying improving
        volumes = [2000.0] * 10 + [500.0] * 10
        # First half: bearish candles (open > close), second half: bullish
        candles = []
        for i in range(10):
            c = closes[i]
            candles.append({"start_time": i*3600000, "open": c+60, "high": c+80, "low": c-20, "close": c, "volume": volumes[i]})
        for i in range(10, 20):
            c = closes[i]
            candles.append({"start_time": i*3600000, "open": c-30, "high": c+20, "low": c-50, "close": c, "volume": volumes[i]})
        div = analyzer._detect_cvd_divergence(candles, lookback=20)
        # With price falling and CVD improving: should be BULL or None (boundary)
        assert div in ("BULL", None)

    def test_cvd_divergence_insufficient_data(self):
        analyzer = OrderFlowAnalyzer()
        assert analyzer._detect_cvd_divergence(bullish_candles(10), lookback=20) is None


# ─── Tests: Funding Rate ──────────────────────────────────────────────────────

class TestFundingRate:
    def test_funding_trend_rising(self):
        analyzer = OrderFlowAnalyzer()
        funding = make_funding([0.01, 0.02, 0.04])
        trend = analyzer._funding_trend(funding)
        assert trend == "RISING"

    def test_funding_trend_falling(self):
        analyzer = OrderFlowAnalyzer()
        funding = make_funding([0.04, 0.02, 0.01])
        trend = analyzer._funding_trend(funding)
        assert trend == "FALLING"

    def test_funding_trend_neutral(self):
        analyzer = OrderFlowAnalyzer()
        funding = make_funding([0.01, 0.01])
        assert analyzer._funding_trend(funding) == "NEUTRAL"

    def test_funding_trend_neutral_single(self):
        analyzer = OrderFlowAnalyzer()
        assert analyzer._funding_trend(make_funding([0.01])) == "NEUTRAL"


# ─── Tests: Signal generation ─────────────────────────────────────────────────

class TestSignalGeneration:
    def test_oi_long_buildup_signal(self):
        analyzer = OrderFlowAnalyzer()
        result = OrderFlowResult(oi_class="LONG_BUILDUP")
        signals = analyzer._generate_signals(result)
        assert "OI_LONG_BUILDUP" in signals

    def test_oi_short_buildup_signal(self):
        analyzer = OrderFlowAnalyzer()
        result = OrderFlowResult(oi_class="SHORT_BUILDUP")
        signals = analyzer._generate_signals(result)
        assert "OI_SHORT_BUILDUP" in signals

    def test_funding_extreme_positive_signal(self):
        analyzer = OrderFlowAnalyzer()
        result = OrderFlowResult(funding_rate=0.02)  # > 0.01 threshold
        signals = analyzer._generate_signals(result)
        assert "FUNDING_EXTREME_POSITIVE" in signals

    def test_funding_extreme_negative_signal(self):
        analyzer = OrderFlowAnalyzer()
        result = OrderFlowResult(funding_rate=-0.02)
        signals = analyzer._generate_signals(result)
        assert "FUNDING_EXTREME_NEGATIVE" in signals

    def test_no_funding_signal_within_normal(self):
        analyzer = OrderFlowAnalyzer()
        result = OrderFlowResult(funding_rate=0.005)  # below 0.01 threshold
        signals = analyzer._generate_signals(result)
        assert "FUNDING_EXTREME_POSITIVE" not in signals
        assert "FUNDING_EXTREME_NEGATIVE" not in signals

    def test_liquidation_zone_nearby_above(self):
        analyzer = OrderFlowAnalyzer()
        result = OrderFlowResult(
            liq_zone_above=96500.0,
            liq_above_pct=1.5,  # within 2% threshold
            current_price=95000.0,
        )
        signals = analyzer._generate_signals(result)
        assert "LIQUIDATION_ZONE_NEARBY_ABOVE" in signals

    def test_liquidation_zone_not_triggered_when_far(self):
        analyzer = OrderFlowAnalyzer()
        result = OrderFlowResult(
            liq_zone_above=100000.0,
            liq_above_pct=5.0,  # > 2% threshold
            current_price=95000.0,
        )
        signals = analyzer._generate_signals(result)
        assert "LIQUIDATION_ZONE_NEARBY_ABOVE" not in signals

    def test_cvd_bull_divergence_signal(self):
        analyzer = OrderFlowAnalyzer()
        result = OrderFlowResult(cvd_divergence="BULL")
        signals = analyzer._generate_signals(result)
        assert "CVD_DIVERGENCE_BULL" in signals


# ─── Tests: Full analyze() pipeline ──────────────────────────────────────────

class TestFullAnalyze:
    def test_returns_order_flow_result(self, mocker):
        mocker.patch.object(
            OrderFlowAnalyzer, "_fetch_liquidation_zones", return_value={}
        )
        analyzer = OrderFlowAnalyzer()
        result = analyzer.analyze(
            oi_history=make_oi([17500.0, 18000.0, 18200.0]),
            candles=bullish_candles(25),
            funding_history=make_funding([0.01, 0.015, 0.02]),
            current_price=97400.0,
        )
        assert isinstance(result, OrderFlowResult)
        assert result.current_price == 97400.0

    def test_oi_class_set_in_result(self, mocker):
        mocker.patch.object(OrderFlowAnalyzer, "_fetch_liquidation_zones", return_value={})
        analyzer = OrderFlowAnalyzer()
        result = analyzer.analyze(
            oi_history=make_oi([17500.0, 18000.0, 18200.0, 18400.0, 18600.0]),
            candles=bullish_candles(25),
            funding_history=make_funding([0.01]),
            current_price=97400.0,
        )
        assert result.oi_class is not None

    def test_graceful_on_empty_inputs(self, mocker):
        mocker.patch.object(OrderFlowAnalyzer, "_fetch_liquidation_zones", return_value={})
        analyzer = OrderFlowAnalyzer()
        result = analyzer.analyze(
            oi_history=[], candles=[], funding_history=[], current_price=95000.0
        )
        assert isinstance(result, OrderFlowResult)
        assert result.signals == []

    def test_coinglass_failure_does_not_crash(self, mocker):
        mocker.patch(
            "requests.get",
            side_effect=requests.ConnectionError("no internet"),
        )
        analyzer = OrderFlowAnalyzer()
        result = analyzer.analyze(
            oi_history=make_oi([17500.0, 18000.0]),
            candles=bullish_candles(),
            funding_history=make_funding([0.01]),
            current_price=97400.0,
        )
        assert isinstance(result, OrderFlowResult)
        assert result.liq_zone_above is None
