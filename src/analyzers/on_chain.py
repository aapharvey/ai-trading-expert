"""
Block 5: On-Chain Analyzer.
CoinMetrics Community API: Exchange Netflow + MVRV Ratio.
No API key required — free, no registration.

Exchange Netflow spike detection:
  - Fetches 7-day daily netflow history (FlowInExNtv - FlowOutExNtv).
  - Spike = today's value deviates > EXCHANGE_FLOW_STD_MULTIPLIER * std
    from the 7-day rolling mean. No hardcoded BTC absolute thresholds.

MVRV thresholds (Expert-validated):
  - Bottom signal: MVRV < 1.0  (market below realized cap — capitulation)
  - Top signal:    MVRV > 3.5  (historically overbought — potential top)

Results cached for ONCHAIN_POLL_INTERVAL_MIN (60 min).
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import config
from logger import get_logger
from src.models.signals import OnChainResult

log = get_logger(__name__)

_REQUEST_TIMEOUT = 10
_METRICS_PATH    = "/timeseries/asset-metrics"


class OnChainAnalyzer:

    def __init__(self):
        self._cache:     Optional[OnChainResult] = None
        self._cached_at: Optional[datetime]      = None
        self._ttl_min = config.ONCHAIN_POLL_INTERVAL_MIN

    def analyze(self) -> OnChainResult:
        """
        Returns OnChainResult with signals. Uses cache.
        Always returns a valid result (never raises).
        """
        if self._is_fresh():
            return self._cache  # type: ignore[return-value]

        netflow, netflow_signal = self._fetch_exchange_netflow()
        mvrv                   = self._fetch_mvrv()

        signals = []
        if netflow_signal:
            signals.append(netflow_signal)
        signals += self._mvrv_signals(mvrv)

        result = OnChainResult(
            exchange_netflow=netflow,
            mvrv=mvrv,
            signals=signals,
        )

        self._cache     = result
        self._cached_at = datetime.now(timezone.utc)

        log.info(
            "On-chain: netflow=%s mvrv=%s signals=%s",
            f"{netflow:.1f}" if netflow is not None else "N/A",
            f"{mvrv:.4f}"    if mvrv    is not None else "N/A",
            signals,
        )
        return result

    # ─── Exchange Netflow ─────────────────────────────────────────────────────

    def _fetch_exchange_netflow(self) -> tuple[Optional[float], Optional[str]]:
        """
        Fetches EXCHANGE_FLOW_HISTORY_DAYS+1 days of BTC inflow and outflow
        in a single CoinMetrics call.
        Computes netflow = FlowInExNtv - FlowOutExNtv per day.
        Detects spike: |today - 7d_mean| > EXCHANGE_FLOW_STD_MULTIPLIER * 7d_std.
        Returns (today_netflow, signal_name_or_None).
        """
        try:
            rows = self._coinmetrics_fetch(
                metrics="FlowInExNtv,FlowOutExNtv",
                limit=config.EXCHANGE_FLOW_HISTORY_DAYS + 1,
            )
            if not rows:
                return None, None

            netflows = []
            for row in rows:
                inflow  = row.get("FlowInExNtv")
                outflow = row.get("FlowOutExNtv")
                if inflow is None or outflow is None:
                    continue
                netflows.append(float(inflow) - float(outflow))

            if len(netflows) < 3:
                return None, None

            today   = netflows[-1]
            history = netflows[:-1]   # exclude today from baseline

            mean     = sum(history) / len(history)
            variance = sum((x - mean) ** 2 for x in history) / len(history)
            std      = math.sqrt(variance) if variance > 0 else 1.0

            z_score   = (today - mean) / std
            threshold = config.EXCHANGE_FLOW_STD_MULTIPLIER

            log.debug(
                "Exchange netflow today=%.1f mean=%.1f std=%.1f z=%.2f",
                today, mean, std, z_score,
            )

            if z_score > threshold:
                return today, "EXCHANGE_INFLOW_SPIKE"
            if z_score < -threshold:
                return today, "EXCHANGE_OUTFLOW_SPIKE"
            return today, None

        except Exception as exc:
            log.debug("Exchange netflow fetch error: %s", exc)
            return None, None

    # ─── MVRV ─────────────────────────────────────────────────────────────────

    def _fetch_mvrv(self) -> Optional[float]:
        try:
            rows = self._coinmetrics_fetch("CapMVRVCur", limit=2)
            if not rows:
                return None
            value = rows[-1].get("CapMVRVCur")
            if value is None:
                return None
            result = float(value)
            log.debug("MVRV: %.4f", result)
            return result
        except Exception as exc:
            log.debug("MVRV fetch error: %s", exc)
            return None

    def _mvrv_signals(self, mvrv: Optional[float]) -> list[str]:
        if mvrv is None:
            return []
        if mvrv < config.MVRV_BOTTOM_THRESHOLD:
            return ["MVRV_BOTTOM_SIGNAL"]
        if mvrv > config.MVRV_TOP_THRESHOLD:
            return ["MVRV_TOP_SIGNAL"]
        return []

    # ─── CoinMetrics HTTP helper ───────────────────────────────────────────────

    def _coinmetrics_fetch(self, metrics: str, limit: int) -> list[dict]:
        """
        Fetch daily time series from CoinMetrics Community API.
        Returns list of data rows (dicts), oldest first.
        No API key required.
        """
        resp = requests.get(
            config.COINMETRICS_BASE_URL + _METRICS_PATH,
            params={
                "assets":          "btc",
                "metrics":         metrics,
                "frequency":       "1d",
                "limit_per_asset": limit,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    # ─── Cache ────────────────────────────────────────────────────────────────

    def _is_fresh(self) -> bool:
        if self._cached_at is None:
            return False
        return datetime.now(timezone.utc) - self._cached_at < timedelta(minutes=self._ttl_min)
