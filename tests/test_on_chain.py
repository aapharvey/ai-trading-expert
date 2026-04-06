"""
Phase 2 tests: OnChainAnalyzer — CoinMetrics exchange netflow (z-score) and MVRV.
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

def make_coinmetrics_flows(pairs: list) -> requests.Response:
    """
    Build a CoinMetrics-style response for FlowInExNtv + FlowOutExNtv.
    pairs = list of (inflow, outflow) tuples.
    CoinMetrics returns values as strings.
    """
    resp = requests.Response()
    resp.status_code = 200
    data = [
        {
            "asset": "btc",
            "time": f"2026-0{(i % 9) + 1}-01T00:00:00.000000000Z",
            "FlowInExNtv":  str(float(p[0])),
            "FlowOutExNtv": str(float(p[1])),
        }
        for i, p in enumerate(pairs)
    ]
    resp._content = json.dumps({"data": data}).encode()
    return resp


def make_coinmetrics_mvrv(values: list) -> requests.Response:
    """Build a CoinMetrics-style response for CapMVRVCur."""
    resp = requests.Response()
    resp.status_code = 200
    data = [
        {
            "asset": "btc",
            "time": f"2026-0{(i % 9) + 1}-01T00:00:00.000000000Z",
            "CapMVRVCur": str(float(v)),
        }
        for i, v in enumerate(values)
    ]
    resp._content = json.dumps({"data": data}).encode()
    return resp


def make_coinmetrics_empty() -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps({"data": []}).encode()
    return resp


def flat_flows(baseline_in: float, baseline_out: float,
               today_in: float, today_out: float, n: int = 8) -> list:
    """
    Returns n (inflow, outflow) pairs: (n-1) baseline + 1 today.
    Flat baseline → std=0 → fallback std=1.0, z_score = (today_net - base_net) / 1.0
    """
    return [(baseline_in, baseline_out)] * (n - 1) + [(today_in, today_out)]


# ─── Tests: Exchange Netflow (z-score based) ──────────────────────────────────

class TestExchangeNetflow:
    def test_inflow_spike_signal(self, mocker):
        """Today's netflow far above baseline → INFLOW_SPIKE."""
        # baseline netflow = 0, today netflow = 4000 → z = 4000 >> 2.0
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows(flat_flows(1000, 1000, 5000, 1000)),
                make_coinmetrics_mvrv([1.2, 1.2]),   # MVRV neutral
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_INFLOW_SPIKE" in result.signals

    def test_outflow_spike_signal(self, mocker):
        """Today's netflow far below baseline → OUTFLOW_SPIKE."""
        # baseline netflow = 0, today netflow = -4000 → z = -4000 << -2.0
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows(flat_flows(1000, 1000, 1000, 5000)),
                make_coinmetrics_mvrv([1.2, 1.2]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_OUTFLOW_SPIKE" in result.signals

    def test_normal_flow_no_signal(self, mocker):
        """All days equal netflow → z_score=0, no spike signal."""
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(1000, 1000)] * 8),
                make_coinmetrics_mvrv([1.2, 1.2]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_INFLOW_SPIKE" not in result.signals
        assert "EXCHANGE_OUTFLOW_SPIKE" not in result.signals

    def test_netflow_value_stored(self, mocker):
        """Today's netflow = inflow - outflow is stored on the result."""
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(1000, 800)] * 8),   # netflow=200 every day
                make_coinmetrics_mvrv([1.2, 1.2]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert result.exchange_netflow is not None
        assert abs(result.exchange_netflow - 200.0) < 0.1

    def test_insufficient_history_no_spike(self, mocker):
        """Less than 3 netflow data points → spike detection skipped."""
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(5000, 1000), (5000, 1000)]),   # only 2 rows
                make_coinmetrics_mvrv([1.2, 1.2]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "EXCHANGE_INFLOW_SPIKE" not in result.signals
        assert result.exchange_netflow is None


# ─── Tests: MVRV ─────────────────────────────────────────────────────────────

class TestMVRV:
    def test_mvrv_bottom_signal(self, mocker):
        """MVRV < 1.0 → MVRV_BOTTOM_SIGNAL."""
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(1000, 1000)] * 8),   # neutral netflow
                make_coinmetrics_mvrv([1.1, 0.95]),           # MVRV < 1.0
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "MVRV_BOTTOM_SIGNAL" in result.signals

    def test_mvrv_top_signal(self, mocker):
        """MVRV > 3.5 → MVRV_TOP_SIGNAL."""
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(1000, 1000)] * 8),
                make_coinmetrics_mvrv([3.0, 3.6]),            # MVRV > 3.5
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "MVRV_TOP_SIGNAL" in result.signals

    def test_mvrv_neutral(self, mocker):
        """MVRV in 1.0–3.5 range → no signal."""
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(1000, 1000)] * 8),
                make_coinmetrics_mvrv([1.5, 1.8]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "MVRV_BOTTOM_SIGNAL" not in result.signals
        assert "MVRV_TOP_SIGNAL" not in result.signals

    def test_mvrv_value_stored(self, mocker):
        """MVRV value is stored on the result."""
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(1000, 1000)] * 8),
                make_coinmetrics_mvrv([1.1, 0.92]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert result.mvrv is not None
        assert abs(result.mvrv - 0.92) < 0.001

    def test_mvrv_boundary_bottom(self, mocker):
        """MVRV exactly at 1.0 → no signal (threshold is strictly < 1.0)."""
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(1000, 1000)] * 8),
                make_coinmetrics_mvrv([1.1, 1.0]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "MVRV_BOTTOM_SIGNAL" not in result.signals

    def test_mvrv_boundary_top(self, mocker):
        """MVRV exactly at 3.5 → no signal (threshold is strictly > 3.5)."""
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(1000, 1000)] * 8),
                make_coinmetrics_mvrv([3.0, 3.5]),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert "MVRV_TOP_SIGNAL" not in result.signals


# ─── Tests: _mvrv_signals unit tests ─────────────────────────────────────────

class TestMvrvSignals:
    def test_below_bottom_threshold(self):
        assert OnChainAnalyzer()._mvrv_signals(0.95) == ["MVRV_BOTTOM_SIGNAL"]

    def test_above_top_threshold(self):
        assert OnChainAnalyzer()._mvrv_signals(3.6) == ["MVRV_TOP_SIGNAL"]

    def test_neutral_range(self):
        assert OnChainAnalyzer()._mvrv_signals(1.8) == []

    def test_none_returns_empty(self):
        assert OnChainAnalyzer()._mvrv_signals(None) == []


# ─── Tests: Caching ──────────────────────────────────────────────────────────

class TestCaching:
    def test_result_cached(self, mocker):
        """Second analyze() call uses cache — no extra HTTP requests."""
        mock_get = mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(1000, 1000)] * 8),
                make_coinmetrics_mvrv([1.5, 1.5]),
            ],
        )
        analyzer = OnChainAnalyzer()
        analyzer.analyze()
        analyzer.analyze()   # Should hit cache
        assert mock_get.call_count == 2   # Only 2 calls from first analyze()

    def test_cache_expires(self, mocker):
        """After TTL expires, next analyze() makes fresh HTTP requests."""
        mock_get = mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_flows([(1000, 1000)] * 8),
                make_coinmetrics_mvrv([1.5, 1.5]),
                make_coinmetrics_flows([(1000, 1000)] * 8),
                make_coinmetrics_mvrv([1.5, 1.5]),
            ],
        )
        analyzer = OnChainAnalyzer()
        analyzer.analyze()
        analyzer._cached_at = datetime.now(timezone.utc) - timedelta(minutes=61)
        analyzer.analyze()
        assert mock_get.call_count == 4   # 2 per analyze() call


# ─── Tests: Graceful failure ──────────────────────────────────────────────────

class TestGracefulFailure:
    def test_network_error_returns_empty(self, mocker):
        mocker.patch("requests.get", side_effect=requests.ConnectionError())
        result = OnChainAnalyzer().analyze()
        assert isinstance(result, OnChainResult)
        assert result.signals == []

    def test_empty_api_response_handled(self, mocker):
        mocker.patch(
            "requests.get",
            side_effect=[
                make_coinmetrics_empty(),
                make_coinmetrics_empty(),
            ],
        )
        result = OnChainAnalyzer().analyze()
        assert isinstance(result, OnChainResult)
        assert result.exchange_netflow is None
        assert result.mvrv is None
