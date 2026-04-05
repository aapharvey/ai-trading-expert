"""
Bybit REST API client (read-only).
Handles all market data fetching with retry logic and rate limiting.
"""

import time
from typing import Optional

import requests

import config
from logger import get_logger

log = get_logger(__name__)

# Bybit API limits: 120 req/min for market endpoints (public, no auth needed)
_REQUEST_TIMEOUT = 10        # seconds
_MAX_RETRIES = 3
_RETRY_BACKOFF = [1, 2, 4]  # seconds between retries


class BybitAPIError(Exception):
    """Raised when Bybit API returns a non-zero retCode."""

    def __init__(self, ret_code: int, message: str):
        self.ret_code = ret_code
        self.message = message
        super().__init__(f"Bybit API error {ret_code}: {message}")


class BybitClient:
    """
    Read-only Bybit V5 market data client.

    All methods return validated Python dicts/lists.
    Raises BybitAPIError on API-level errors, requests.RequestException on network errors.
    """

    def __init__(self, base_url: str = config.BYBIT_BASE_URL):
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ─── Internal helpers ────────────────────────────────────────────────────

    def _get(self, path: str, params: dict) -> dict:
        """GET with retry + exponential backoff. Returns parsed JSON body."""
        url = f"{self._base_url}{path}"

        for attempt, wait in enumerate(_RETRY_BACKOFF, start=1):
            try:
                response = self._session.get(
                    url, params=params, timeout=_REQUEST_TIMEOUT
                )
                response.raise_for_status()
                body = response.json()

                ret_code = body.get("retCode", -1)
                if ret_code != 0:
                    raise BybitAPIError(ret_code, body.get("retMsg", "unknown"))

                return body

            except BybitAPIError:
                raise  # Don't retry API-level errors
            except requests.RequestException as exc:
                if attempt == _MAX_RETRIES:
                    log.error("Network error after %d retries: %s", _MAX_RETRIES, exc)
                    raise
                log.warning(
                    "Network error (attempt %d/%d): %s — retrying in %ds",
                    attempt, _MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)

    @staticmethod
    def _result_list(body: dict) -> list:
        """Extract result.list from Bybit response, raise if missing."""
        try:
            return body["result"]["list"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Unexpected Bybit response structure: {exc}") from exc

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_klines(
        self,
        symbol: str = config.SYMBOL,
        interval: str = "60",
        limit: int = config.KLINE_LIMIT,
        category: str = config.CATEGORY,
    ) -> list[dict]:
        """
        Fetch OHLCV candlestick data.

        Returns list of dicts with keys:
          start_time, open, high, low, close, volume, turnover
        Sorted oldest → newest.

        interval: Bybit interval string — "1","3","5","15","30","60","120","240","360","720","D","W","M"
        """
        body = self._get(
            "/v5/market/kline",
            params={
                "category": category,
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
        )
        raw = self._result_list(body)

        # Bybit returns newest first — reverse to oldest-first for indicator calcs
        candles = [
            {
                "start_time": int(row[0]),
                "open":       float(row[1]),
                "high":       float(row[2]),
                "low":        float(row[3]),
                "close":      float(row[4]),
                "volume":     float(row[5]),
                "turnover":   float(row[6]),
            }
            for row in reversed(raw)
        ]
        log.debug("get_klines(%s, %s): %d candles", symbol, interval, len(candles))
        return candles

    def get_ticker(
        self,
        symbol: str = config.SYMBOL,
        category: str = config.CATEGORY,
    ) -> dict:
        """
        Fetch current ticker for a symbol.

        Returns dict with keys:
          symbol, last_price, mark_price, index_price,
          price_24h_pct, volume_24h, turnover_24h, bid1_price, ask1_price
        """
        body = self._get(
            "/v5/market/tickers",
            params={"category": category, "symbol": symbol},
        )
        raw = self._result_list(body)
        if not raw:
            raise ValueError(f"No ticker data for {symbol}")

        t = raw[0]
        result = {
            "symbol":          t.get("symbol", symbol),
            "last_price":      float(t.get("lastPrice", 0)),
            "mark_price":      float(t.get("markPrice", 0)),
            "index_price":     float(t.get("indexPrice", 0)),
            "price_24h_pct":   float(t.get("price24hPcnt", 0)) * 100,
            "volume_24h":      float(t.get("volume24h", 0)),
            "turnover_24h":    float(t.get("turnover24h", 0)),
            "bid1_price":      float(t.get("bid1Price", 0)),
            "ask1_price":      float(t.get("ask1Price", 0)),
        }
        log.debug("get_ticker(%s): $%.2f", symbol, result["last_price"])
        return result

    def get_open_interest(
        self,
        symbol: str = config.SYMBOL,
        interval_time: str = "1h",
        limit: int = 50,
        category: str = config.CATEGORY,
    ) -> list[dict]:
        """
        Fetch Open Interest history.

        interval_time: "5min","15min","30min","1h","4h","1d"
        Returns list of dicts (oldest first): timestamp, open_interest
        """
        body = self._get(
            "/v5/market/open-interest",
            params={
                "category": category,
                "symbol": symbol,
                "intervalTime": interval_time,
                "limit": limit,
            },
        )
        raw = self._result_list(body)
        result = [
            {
                "timestamp":     int(row["timestamp"]),
                "open_interest": float(row["openInterest"]),
            }
            for row in reversed(raw)
        ]
        log.debug("get_open_interest(%s): %d records", symbol, len(result))
        return result

    def get_funding_rate(
        self,
        symbol: str = config.SYMBOL,
        limit: int = 10,
        category: str = config.CATEGORY,
    ) -> list[dict]:
        """
        Fetch funding rate history (most recent first → reversed to oldest first).

        Returns list of dicts: timestamp, funding_rate, mark_price
        """
        body = self._get(
            "/v5/market/funding/history",
            params={
                "category": category,
                "symbol": symbol,
                "limit": limit,
            },
        )
        raw = self._result_list(body)
        result = [
            {
                "timestamp":    int(row["fundingRateTimestamp"]),
                "funding_rate": float(row["fundingRate"]) * 100,  # as percentage
                "mark_price":   float(row["markPrice"]),
            }
            for row in reversed(raw)
        ]
        log.debug(
            "get_funding_rate(%s): latest=%.4f%%",
            symbol,
            result[-1]["funding_rate"] if result else 0,
        )
        return result

    def get_orderbook(
        self,
        symbol: str = config.SYMBOL,
        limit: int = 50,
        category: str = config.CATEGORY,
    ) -> dict:
        """
        Fetch order book snapshot.

        Returns dict:
          bids: list of [price, qty] (descending by price)
          asks: list of [price, qty] (ascending by price)
          timestamp: int (ms)
          best_bid: float
          best_ask: float
          spread: float (absolute)
          spread_pct: float (%)
        """
        body = self._get(
            "/v5/market/orderbook",
            params={
                "category": category,
                "symbol": symbol,
                "limit": limit,
            },
        )
        raw = body.get("result", {})
        bids = [[float(p), float(q)] for p, q in raw.get("b", [])]
        asks = [[float(p), float(q)] for p, q in raw.get("a", [])]

        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        spread = best_ask - best_bid
        spread_pct = (spread / best_bid * 100) if best_bid else 0.0

        result = {
            "bids":       bids,
            "asks":       asks,
            "timestamp":  int(raw.get("ts", 0)),
            "best_bid":   best_bid,
            "best_ask":   best_ask,
            "spread":     spread,
            "spread_pct": spread_pct,
        }
        log.debug(
            "get_orderbook(%s): bid=$%.2f ask=$%.2f spread=%.4f%%",
            symbol, best_bid, best_ask, spread_pct,
        )
        return result
