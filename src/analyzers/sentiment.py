"""
Block 4+6: Sentiment Analyzer.
Combines Fear & Greed Index (alternative.me) and CryptoPanic news sentiment.

- Fear & Greed: free, no API key. Polled every 60 min (daily data).
- CryptoPanic: free API key required (CRYPTOPANIC_API_KEY in .env).
  Polled every 15 min. Gracefully skipped if key absent.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import config
from logger import get_logger
from src.models.signals import SentimentResult

log = get_logger(__name__)

_FEAR_GREED_URL  = "https://api.alternative.me/fng/?limit=2"
_REQUEST_TIMEOUT = 8


class SentimentAnalyzer:
    """
    Fetches and analyzes Fear & Greed Index and crypto news sentiment.
    Results are cached to avoid hammering external APIs on every 60s cycle.
    """

    def __init__(self):
        self._fg_cache:     Optional[SentimentResult] = None
        self._fg_cached_at: Optional[datetime]        = None
        self._news_cache:     Optional[SentimentResult] = None
        self._news_cached_at: Optional[datetime]        = None

        self._fg_ttl_min   = 60                              # F&G updates daily
        self._news_ttl_min = config.NEWS_POLL_INTERVAL_MIN  # 15 min

    def analyze(self) -> SentimentResult:
        """
        Returns a merged SentimentResult from F&G + news.
        Uses cached values when TTL has not expired.
        Always returns a valid result (never raises).
        """
        fg   = self._get_fear_greed()
        news = self._get_news()

        # Merge signals
        result = SentimentResult(
            fear_greed_value   = fg.fear_greed_value,
            fear_greed_label   = fg.fear_greed_label,
            news_sentiment     = news.news_sentiment,
            bullish_news_count = news.bullish_news_count,
            bearish_news_count = news.bearish_news_count,
            signals            = fg.signals + news.signals,
        )

        log.debug(
            "Sentiment: F&G=%s(%s) news=%s signals=%s",
            fg.fear_greed_value, fg.fear_greed_label,
            news.news_sentiment, result.signals,
        )
        return result

    # ─── Fear & Greed ────────────────────────────────────────────────────────

    def _get_fear_greed(self) -> SentimentResult:
        if self._is_fresh(self._fg_cached_at, self._fg_ttl_min):
            return self._fg_cache  # type: ignore[return-value]

        result = self._fetch_fear_greed()
        self._fg_cache     = result
        self._fg_cached_at = datetime.now(timezone.utc)
        return result

    def _fetch_fear_greed(self) -> SentimentResult:
        try:
            resp = requests.get(_FEAR_GREED_URL, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            entry = data["data"][0]
            value = int(entry["value"])
            label = entry["value_classification"]   # e.g. "Extreme Fear"

            signals = self._fg_signals(value)

            log.info("Fear & Greed: %d (%s)", value, label)
            return SentimentResult(
                fear_greed_value=value,
                fear_greed_label=label,
                signals=signals,
            )
        except Exception as exc:
            log.warning("Fear & Greed fetch failed: %s", exc)
            return SentimentResult()

    def _fg_signals(self, value: int) -> list[str]:
        if value <= config.FEAR_GREED_EXTREME_FEAR:
            return ["EXTREME_FEAR"]     # Contrarian LONG
        if value <= config.FEAR_GREED_FEAR:
            return ["FEAR"]
        if value >= config.FEAR_GREED_EXTREME_GREED:
            return ["EXTREME_GREED"]    # Contrarian SHORT
        if value >= config.FEAR_GREED_GREED:
            return ["GREED"]
        return []

    # ─── CryptoPanic News ─────────────────────────────────────────────────────

    def _get_news(self) -> SentimentResult:
        if not config.CRYPTOPANIC_API_KEY:
            log.debug("CryptoPanic: API key not set — skipping news")
            return SentimentResult()

        if self._is_fresh(self._news_cached_at, self._news_ttl_min):
            return self._news_cache  # type: ignore[return-value]

        result = self._fetch_news()
        self._news_cache     = result
        self._news_cached_at = datetime.now(timezone.utc)
        return result

    def _fetch_news(self) -> SentimentResult:
        try:
            resp = requests.get(
                config.CRYPTOPANIC_BASE_URL,
                params={
                    "auth_token": config.CRYPTOPANIC_API_KEY,
                    "currencies": "BTC",
                    "public":     "true",
                    "filter":     "hot",
                    "kind":       "news",
                },
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            posts   = data.get("results", [])[:15]
            bullish = sum(1 for p in posts if p.get("votes", {}).get("positive", 0) > p.get("votes", {}).get("negative", 0))
            bearish = sum(1 for p in posts if p.get("votes", {}).get("negative", 0) > p.get("votes", {}).get("positive", 0))

            sentiment, signals = self._news_signals(bullish, bearish, len(posts))

            log.info(
                "CryptoPanic: %d posts — bullish=%d bearish=%d → %s",
                len(posts), bullish, bearish, sentiment,
            )
            return SentimentResult(
                news_sentiment=sentiment,
                bullish_news_count=bullish,
                bearish_news_count=bearish,
                signals=signals,
            )
        except Exception as exc:
            log.warning("CryptoPanic fetch failed: %s", exc)
            return SentimentResult()

    def _news_signals(
        self, bullish: int, bearish: int, total: int
    ) -> tuple[str, list[str]]:
        if total == 0:
            return "NEUTRAL", []

        bull_ratio = bullish / total
        bear_ratio = bearish / total

        if bullish >= config.NEWS_BULLISH_THRESHOLD and bull_ratio >= 0.5:
            return "BULLISH", ["NEWS_BULLISH_MAJOR"]
        if bearish >= config.NEWS_BEARISH_THRESHOLD and bear_ratio >= 0.5:
            return "BEARISH", ["NEWS_BEARISH_MAJOR"]
        return "NEUTRAL", []

    # ─── Cache helper ─────────────────────────────────────────────────────────

    @staticmethod
    def _is_fresh(cached_at: Optional[datetime], ttl_min: int) -> bool:
        if cached_at is None:
            return False
        return datetime.now(timezone.utc) - cached_at < timedelta(minutes=ttl_min)
