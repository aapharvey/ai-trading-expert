"""
Phase 2 tests: OnChainAnalyzer — Glassnode exchange netflow and SOPR.
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

def make_glassnode_response(value: float) -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps([
        {"t": 1700000000, "v": value - 100},
        {"t": 1700086400, "v": value},
    ]).encode()
    return resp


def make_glassnode_empty() -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps([]).encode()
    return resp


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


# ─── Tests: Exchange Netflow ──────────────────────────────────────────────────

class TestExchangeNetflow:
    def test_inflow_spike_signal(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        # inflow=5000, outflow=1000 → netflow=+4000 → INFLOW_SPIKE
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_response(5000.0),   # inflow
                make_glassnode_response(1000.0),   # outflow
                make_glassnode_response(1.01),     # SOPR neutral
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_INFLOW_SPIKE" in result.signals

    def test_outflow_spike_signal(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        # inflow=500, outflow=5000 → netflow=-4500 → OUTFLOW_SPIKE (bullish)
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_response(500.0),    # inflow
                make_glassnode_response(5000.0),   # outflow
                make_glassnode_response(1.01),     # SOPR neutral
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_OUTFLOW_SPIKE" in result.signals

    def test_normal_flow_no_signal(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        # inflow=1200, outflow=1000 → netflow=+200 (below threshold)
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_response(1200.0),
                make_glassnode_response(1000.0),
                make_glassnode_response(1.01),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_INFLOW_SPIKE" not in result.signals
        assert "EXCHANGE_OUTFLOW_SPIKE" not in result.signals

    def test_netflow_value_stored(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_response(3000.0),
                make_glassnode_response(1000.0),
                make_glassnode_response(1.01),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert result.exchange_netflow is not None
        assert abs(result.exchange_netflow - 2000.0) < 0.1


# ─── Tests: SOPR ─────────────────────────────────────────────────────────────

class TestSOPR:
    def test_sopr_bottom_signal(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_response(1000.0),  # inflow (neutral)
                make_glassnode_response(1000.0),  # outflow (neutral)
                make_glassnode_response(0.97),    # SOPR < 0.98 → BOTTOM
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "SOPR_BOTTOM_SIGNAL" in result.signals

    def test_sopr_top_signal(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_response(1000.0),
                make_glassnode_response(1000.0),
                make_glassnode_response(1.06),   # SOPR > 1.04 → TOP
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "SOPR_TOP_SIGNAL" in result.signals

    def test_sopr_neutral(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_response(1000.0),
                make_glassnode_response(1000.0),
                make_glassnode_response(1.01),   # SOPR neutral range
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "SOPR_BOTTOM_SIGNAL" not in result.signals
        assert "SOPR_TOP_SIGNAL" not in result.signals

    def test_sopr_value_stored(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_response(1000.0),
                make_glassnode_response(1000.0),
                make_glassnode_response(0.95),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert result.sopr is not None
        assert abs(result.sopr - 0.95) < 0.001


# ─── Tests: Signal generation ─────────────────────────────────────────────────

class TestSignalGeneration:
    def test_inflow_spike_signal_generated(self):
        analyzer = OnChainAnalyzer()
        signals = analyzer._generate_signals(netflow=2000.0, sopr=1.01)
        assert "EXCHANGE_INFLOW_SPIKE" in signals

    def test_outflow_spike_signal_generated(self):
        analyzer = OnChainAnalyzer()
        signals = analyzer._generate_signals(netflow=-2000.0, sopr=1.01)
        assert "EXCHANGE_OUTFLOW_SPIKE" in signals

    def test_sopr_bottom_signal_generated(self):
        analyzer = OnChainAnalyzer()
        signals = analyzer._generate_signals(netflow=0, sopr=0.97)
        assert "SOPR_BOTTOM_SIGNAL" in signals

    def test_sopr_top_signal_generated(self):
        analyzer = OnChainAnalyzer()
        signals = analyzer._generate_signals(netflow=0, sopr=1.05)
        assert "SOPR_TOP_SIGNAL" in signals

    def test_no_signals_when_none_values(self):
        analyzer = OnChainAnalyzer()
        signals = analyzer._generate_signals(netflow=None, sopr=None)
        assert signals == []

    def test_multiple_signals_possible(self):
        analyzer = OnChainAnalyzer()
        # Both netflow spike AND sopr signal
        signals = analyzer._generate_signals(netflow=-3000.0, sopr=0.96)
        assert "EXCHANGE_OUTFLOW_SPIKE" in signals
        assert "SOPR_BOTTOM_SIGNAL" in signals


# ─── Tests: Caching ──────────────────────────────────────────────────────────

class TestCaching:
    def test_result_cached(self, mocker):
        config.GLASSNODE_API_KEY = "test_key"
        mock_get = mocker.patch(
            "requests.get",
            side_effect=[
                make_glassnode_response(1000.0),
                make_glassnode_response(1000.0),
                make_glassnode_response(1.01),
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
                make_glassnode_response(1000.0),
                make_glassnode_response(1000.0),
                make_glassnode_response(1.01),
                make_glassnode_response(1000.0),
                make_glassnode_response(1000.0),
                make_glassnode_response(1.01),
            ],
        )
        analyzer = OnChainAnalyzer()
        analyzer.analyze()
        # Expire cache
        analyzer._cached_at = datetime.now(timezone.utc) - timedelta(minutes=61)
        analyzer.analyze()
        assert mock_get.call_count == 6  # 3 per analyze call


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
