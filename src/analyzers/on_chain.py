"""
Block 5: On-Chain Analyzer.
Glassnode metrics: Exchange Netflow (dynamic threshold) + SOPR.

Exchange Netflow spike detection:
  - Fetches 7-day daily netflow history.
  - Spike = today's value deviates > EXCHANGE_FLOW_STD_MULTIPLIER * std
    from the 7-day rolling mean. No hardcoded BTC absolute thresholds.

SOPR thresholds (Expert-validated):
  - Bottom signal: SOPR < 0.95  (capitulation, real dip)
  - Top signal:    SOPR > 1.07  (extended profit taking)

Requires GLASSNODE_API_KEY in .env. Gracefully returns empty if absent.
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
        if not config.GLASSNODE_API_KEY:
            log.debug("Glassnode: API key not set — skipping on-chain analysis")
            return OnChainResult()

        if self._is_fresh():
            return self._cache  # type: ignore[return-value]

        netflow, netflow_signal = self._fetch_exchange_netflow()
        sopr                   = self._fetch_sopr()

        signals = []
        if netflow_signal:
            signals.append(netflow_signal)
        signals += self._sopr_signals(sopr)

        result = OnChainResult(
            exchange_netflow=netflow,
            sopr=sopr,
            signals=signals,
        )

        self._cache     = result
        self._cached_at = datetime.now(timezone.utc)

        log.info(
            "On-chain: netflow=%s sopr=%s signals=%s",
            f"{netflow:.1f}" if netflow is not None else "N/A",
            f"{sopr:.4f}"    if sopr    is not None else "N/A",
            signals,
        )
        return result

    # ─── Exchange Netflow ─────────────────────────────────────────────────────

    def _fetch_exchange_netflow(self) -> tuple[Optional[float], Optional[str]]:
        """
        Fetches 7-day daily exchange inflow and outflow history.
        Computes netflow = inflow - outflow per day.
        Detects spike: |today - 7d_mean| > EXCHANGE_FLOW_STD_MULTIPLIER * 7d_std.
        Returns (today_netflow, signal_name_or_None).
        """
        try:
            inflows  = self._glassnode_history(
                "transactions/transfers_to_exchanges_sum",
                days=config.EXCHANGE_FLOW_HISTORY_DAYS + 1,
            )
            outflows = self._glassnode_history(
                "transactions/transfers_from_exchanges_sum",
                days=config.EXCHANGE_FLOW_HISTORY_DAYS + 1,
            )
            if not inflows or not outflows:
                return None, None

            # Align by timestamp — zip by position (same daily cadence)
            n = min(len(inflows), len(outflows))
            netflows = [
                inflows[i]["v"] - outflows[i]["v"]
                for i in range(n)
            ]

            if len(netflows) < 3:
                return None, None

            today   = netflows[-1]
            history = netflows[:-1]   # exclude today from baseline

            mean    = sum(history) / len(history)
            variance = sum((x - mean) ** 2 for x in history) / len(history)
            std     = math.sqrt(variance) if variance > 0 else 1.0

            z_score = (today - mean) / std
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

    # ─── SOPR ────────────────────────────────────────────────────────────────

    def _fetch_sopr(self) -> Optional[float]:
        try:
            data = self._glassnode_history("indicators/sopr", days=2)
            if not data:
                return None
            value = data[-1]["v"]
            log.debug("SOPR: %.4f", value)
            return float(value)
        except Exception as exc:
            log.debug("SOPR fetch error: %s", exc)
            return None

    def _sopr_signals(self, sopr: Optional[float]) -> list[str]:
        if sopr is None:
            return []
        if sopr < config.SOPR_BOTTOM_THRESHOLD:
            return ["SOPR_BOTTOM_SIGNAL"]
        if sopr > config.SOPR_TOP_THRESHOLD:
            return ["SOPR_TOP_SIGNAL"]
        return []

    # ─── Glassnode HTTP helper ────────────────────────────────────────────────

    def _glassnode_history(self, endpoint: str, days: int) -> list[dict]:
        """
        Fetch daily time series from Glassnode.
        Returns list of {t: unix_ts, v: float}, oldest first.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)

        resp = requests.get(
            f"{config.GLASSNODE_BASE_URL}/{endpoint}",
            params={
                "a":       "BTC",
                "api_key": config.GLASSNODE_API_KEY,
                "i":       "24h",
                "s":       int(since.timestamp()),
            },
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        # Filter out entries where v is None
        return [row for row in data if row.get("v") is not None]

    # ─── Cache ────────────────────────────────────────────────────────────────

    def _is_fresh(self) -> bool:
        if self._cached_at is None:
            return False
        return datetime.now(timezone.utc) - self._cached_at < timedelta(minutes=self._ttl_min)
