"""
Phase 2 tests: OnChainAnalyzer — Glassnode exchange netflow (z-score) and SOPR.
All external calls mocked.
"""

import json
from datetime import datetime, timedelta, timezone
import pytest
import requests

from src.analyzers.on_chain import OnChainAnalyzer
from src.models.signals import OnChainResult
import config


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_glassnode_history(values: list) -> requests.Response:
    """Build a Glassnode-style response with multiple daily data points."""
    resp = requests.Response()
    resp.status_code = 200
    data = [{"t": 1700000000 + i * 86400, "v": float(v)} for i, v in enumerate(values)]
    resp._content = json.dumps(data).encode()
    return resp


def make_glassnode_empty() -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps([]).encode()
    return resp


def flat_history(baseline: float, today: float, n: int = 8) -> list:
    """
    Returns n values: (n-1) baseline entries + 1 today.
    When baseline values are all equal, std=0 → fallback std=1.0,
    so z_score = (today - baseline) / 1.0 — easy to reason about in tests.
    """
    return [baseline] * (n - 1) + [today]


# ─── Tests: Skipped when no API key ──────────────────────────────────────────

class TestNoAPIKey:
    def test_returns_empty_when_no_key(self, mocker):
        original = config.GLASSNODE_API_KEY
        config.GLASSNODE_API_KEY = ""
        try:
            mock_get = mocker.patch("requests.get")
            result = OnChainAnalyzer().analyze()
            mock_get.assert_not_called()
            assert isinstance(result, OnChainResult)
            assert result.signals == []
            assert result.exchange_netflow is None
            assert result.sopr is None
        finally:
            config.GLASSNODE_API_KEY = original


# ─── Tests: Exchange Netflow (z-score based) ──────────────────────────────────

class TestExchangeNetflow:
    def test_inflow_spike_signal(self, mocker):
        """Today's netflow far above baseline (z >> 2) → INFLOW_SPIKE."""
        config.GLASSNODE_API_KEY = "test_key"
        # inflow baseline=1000, today=5000; outflow constant 1000
        # netflows: [0]*7 + [4000]; z_score = 4000/1.0 >> 2.0 → spike
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history(flat_history(1000.0, 5000.0)),  # inflow
                make_glassnode_history(flat_history(1000.0, 1000.0)),  # outflow
                make_glassnode_history([1.0, 1.01]),                   # SOPR neutral
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_INFLOW_SPIKE" in result.signals

    def test_outflow_spike_signal(self, mocker):
        """Today's netflow far below baseline (z << -2) → OUTFLOW_SPIKE."""
        config.GLASSNODE_API_KEY = "test_key"
        # inflow constant 1000; outflow baseline=1000, today=5000
        # netflows: [0]*7 + [-4000]; z_score = -4000 << -2.0 → spike
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history(flat_history(1000.0, 1000.0)),  # inflow
                make_glassnode_history(flat_history(1000.0, 5000.0)),  # outflow
                make_glassnode_history([1.0, 1.01]),                   # SOPR neutral
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_OUTFLOW_SPIKE" in result.signals

    def test_normal_flow_no_signal(self, mocker):
        """All days equal netflow → z_score=0, no spike signal."""
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([1000.0] * 8),   # inflow constant
                make_glassnode_history([1000.0] * 8),   # outflow constant
                make_glassnode_history([1.0, 1.01]),    # SOPR neutral
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_INFLOW_SPIKE" not in result.signals
        assert "EXCHANGE_OUTFLOW_SPIKE" not in result.signals

    def test_netflow_value_stored(self, mocker):
        """Today's netflow = inflow - outflow is stored on the result."""
        config.GLASSNODE_API_KEY = "test_key"
        # inflow=1000, outflow=800 every day → netflow=200, no spike
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([1000.0] * 8),   # inflow
                make_glassnode_history([800.0] * 8),    # outflow
                make_glassnode_history([1.0, 1.01]),    # SOPR
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert result.exchange_netflow is not None
        assert abs(result.exchange_netflow - 200.0) < 0.1

    def test_insufficient_history_no_spike(self, mocker):
        """Less than 3 netflow data points → spike detection skipped."""
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([5000.0, 5000.0]),   # only 2 points
                make_glassnode_history([1000.0, 1000.0]),
                make_glassnode_history([1.0, 1.01]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_INFLOW_SPIKE" not in result.signals
        assert result.exchange_netflow is None


# ─── Tests: SOPR ─────────────────────────────────────────────────────────────

class TestSOPR:
    def test_sopr_bottom_signal(self, mocker):
        """SOPR < 0.95 → SOPR_BOTTOM_SIGNAL."""
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([1000.0] * 8),   # inflow neutral
                make_glassnode_history([1000.0] * 8),   # outflow neutral
                make_glassnode_history([1.0, 0.94]),    # SOPR < 0.95
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "SOPR_BOTTOM_SIGNAL" in result.signals

    def test_sopr_top_signal(self, mocker):
        """SOPR > 1.07 → SOPR_TOP_SIGNAL."""
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1.0, 1.08]),    # SOPR > 1.07
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "SOPR_TOP_SIGNAL" in result.signals

    def test_sopr_neutral(self, mocker):
        """SOPR in 0.95–1.07 range → no signal."""
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1.0, 1.01]),    # SOPR in neutral range
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "SOPR_BOTTOM_SIGNAL" not in result.signals
        assert "SOPR_TOP_SIGNAL" not in result.signals

    def test_sopr_value_stored(self, mocker):
        """SOPR value from API is stored on the result."""
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1.0, 0.94]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert result.sopr is not None
        assert abs(result.sopr - 0.94) < 0.001

    def test_sopr_boundary_bottom(self, mocker):
        """SOPR exactly at 0.95 → no signal (threshold is strictly < 0.95)."""
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1.0, 0.95]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "SOPR_BOTTOM_SIGNAL" not in result.signals

    def test_sopr_boundary_top(self, mocker):
        """SOPR exactly at 1.07 → no signal (threshold is strictly > 1.07)."""
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1.0, 1.07]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "SOPR_TOP_SIGNAL" not in result.signals


# ─── Tests: _sopr_signals unit tests ─────────────────────────────────────────

class TestSoprSignals:
    def test_below_bottom_threshold(self):
        assert OnChainAnalyzer()._sopr_signals(0.94) == ["SOPR_BOTTOM_SIGNAL"]

    def test_above_top_threshold(self):
        assert OnChainAnalyzer()._sopr_signals(1.08) == ["SOPR_TOP_SIGNAL"]

    def test_neutral_range(self):
        assert OnChainAnalyzer()._sopr_signals(1.01) == []

    def test_none_returns_empty(self):
        assert OnChainAnalyzer()._sopr_signals(None) == []


# ─── Tests: Caching ──────────────────────────────────────────────────────────

class TestCaching:
    def test_result_cached(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        mock_get = mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1.0, 1.01]),
            ],
        )
        analyzer = OnChainAnalyzer()
        analyzer.analyze()
        analyzer.analyze()  # Second call → should use cache
        assert mock_get.call_count == 3  # Only 3 calls from first analyze()

    def test_cache_expires(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        mock_get = mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1.0, 1.01]),
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1000.0] * 8),
                make_glassnode_history([1.0, 1.01]),
            ],
        )
        analyzer = OnChainAnalyzer()
        analyzer.analyze()
        analyzer._cached_at = datetime.now(timezone.utc) - timedelta(minutes=61)
        analyzer.analyze()
        assert mock_get.call_count == 6  # 3 per analyze() call


# ─── Tests: Graceful failure ──────────────────────────────────────────────────

class TestGracefulFailure:
    def test_network_error_returns_empty(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch("requests.get", side_effect=requests.ConnectionError())
        result = OnChainAnalyzer().analyze()
        assert isinstance(result, OnChainResult)
        assert result.signals == []

    def test_empty_api_response_handled(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_empty(),
                make_glassnode_empty(),
                make_glassnode_empty(),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert isinstance(result, OnChainResult)
        assert result.exchange_netflow is None
        assert result.sopr is None
