"""
Block 5: On-Chain Analyzer.
Fetches Glassnode metrics: Exchange Netflow and SOPR.

- Requires GLASSNODE_API_KEY in .env (free tier sufficient).
- Gracefully returns empty result if key absent or API unavailable.
- Results cached for ONCHAIN_POLL_INTERVAL_MIN (60 min) — daily resolution data.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import config
from logger import get_logger
from src.models.signals import OnChainResult

log = get_logger(__name__)

_REQUEST_TIMEOUT = 10


class OnChainAnalyzer:
    """
    Fetches and interprets on-chain metrics from Glassnode.

    Available on free tier:
      - Exchange Net Position Change (net BTC flowing to/from exchanges)
      - SOPR (Spent Output Profit Ratio)
    """

    def __init__(self):
        self._cache:     Optional[OnChainResult] = None
        self._cached_at: Optional[datetime]      = None
        self._ttl_min = config.ONCHAIN_POLL_INTERVAL_MIN

    def analyze(self) -> OnChainResult:
        """
        Returns OnChainResult with signals.
        Uses cache to avoid excessive API calls.
        Always returns a valid result (never raises).
        """
        if not config.GLASSNODE_API_KEY:
            log.debug("Glassnode: API key not set — skipping on-chain analysis")
            return OnChainResult()

        if self._is_fresh():
            return self._cache  # type: ignore[return-value]

        netflow = self._fetch_exchange_netflow()
        sopr    = self._fetch_sopr()

        signals = self._generate_signals(netflow, sopr)

        result = OnChainResult(
            exchange_netflow=netflow,
            sopr=sopr,
            signals=signals,
        )

        self._cache     = result
        self._cached_at = datetime.now(timezone.utc)

        log.info(
            "On-chain: netflow=%.2f sopr=%s signals=%s",
            netflow or 0,
            f"{sopr:.4f}" if sopr else "N/A",
            signals,
        )
        return result

    # ─── Glassnode fetchers ──────────────────────────────────────────────────

    def _fetch_exchange_netflow(self) -> Optional[float]:
        """
        Exchange Net Position Change — net BTC inflow/outflow (daily).
        Positive = more BTC moving TO exchanges (sell pressure).
        Negative = more BTC moving FROM exchanges (accumulation).
        Endpoint: /v1/metrics/transactions/transfers_to_exchanges_sum
        """
        try:
            # We compare inflow to outflow via exchange_net_position_change
            inflow  = self._glassnode_latest("transactions/transfers_to_exchanges_sum")
            outflow = self._glassnode_latest("transactions/transfers_from_exchanges_sum")
            if inflow is None or outflow is None:
                return None
            netflow = inflow - outflow
            log.debug("Exchange netflow: %.2f BTC (in=%.2f out=%.2f)", netflow, inflow, outflow)
            return netflow
        except Exception as exc:
            log.debug("Exchange netflow fetch error: %s", exc)
            return None

    def _fetch_sopr(self) -> Optional[float]:
        """
        SOPR — ratio of realized value to value at time of creation.
        >1: coins moved at profit (potential top/resistance).
        <1: coins moved at loss (potential bottom/support).
        Endpoint: /v1/metrics/indicators/sopr
        """
        try:
            value = self._glassnode_latest("indicators/sopr")
            log.debug("SOPR: %s", value)
            return value
        except Exception as exc:
            log.debug("SOPR fetch error: %s", exc)
            return None

    def _glassnode_latest(self, endpoint: str) -> Optional[float]:
        """
        Fetch the most recent value from a Glassnode endpoint.
        Returns the `v` field of the most recent data point.
        """
        url = f"{config.GLASSNODE_BASE_URL}/{endpoint}"
        resp = requests.get(
            url,
            params={
                "a":           "BTC",
                "api_key":     config.GLASSNODE_API_KEY,
                "i":           "24h",
                "limit":       2,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data:
            return None

        # Glassnode returns list of {t: timestamp, v: value}
        latest = data[-1]
        val = latest.get("v")
        return float(val) if val is not None else None

    # ─── Signal generation ───────────────────────────────────────────────────

    def _generate_signals(
        self,
        netflow: Optional[float],
        sopr:    Optional[float],
    ) -> list[str]:
        signals = []

        if netflow is not None:
            if netflow > config.EXCHANGE_INFLOW_THRESHOLD:
                # Large inflow → potential sell pressure
                signals.append("EXCHANGE_INFLOW_SPIKE")
            elif netflow < -config.EXCHANGE_OUTFLOW_THRESHOLD:
                # Large outflow → accumulation / bullish
                signals.append("EXCHANGE_OUTFLOW_SPIKE")

        if sopr is not None:
            if sopr < config.SOPR_BOTTOM_THRESHOLD:
                # Holders selling at loss → often near a bottom
                signals.append("SOPR_BOTTOM_SIGNAL")
            elif sopr > config.SOPR_TOP_THRESHOLD:
                # Profit taking → often near a top
                signals.append("SOPR_TOP_SIGNAL")

        return signals

    # ─── Cache helper ────────────────────────────────────────────────────────

    def _is_fresh(self) -> bool:
        if self._cached_at is None:
            return False
        return datetime.now(timezone.utc) - self._cached_at < timedelta(minutes=self._ttl_min)
