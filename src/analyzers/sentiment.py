"""
Block 4+6: Sentiment Analyzer.
Combines Fear & Greed Index (alternative.me) and CryptoPanic news sentiment.

Fear & Greed:
  - Free, no API key. Cached 60 min (daily data).
  - Contrarian signal: Extreme Fear → LONG, Extreme Greed → SHORT.

CryptoPanic:
  - Requires CRYPTOPANIC_API_KEY (.env). Gracefully skipped if absent.
  - Makes TWO calls per poll: filter=bullish and filter=bearish.
  - Compares count of tagged posts in last 24h. No vote-counting.
  - Cached 20 min (NEWS_POLL_INTERVAL_MIN).
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import config
from logger import get_logger
from src.models.signals import SentimentResult

log = get_logger(__name__)

_FEAR_GREED_URL  = "https://api.alternative.me/fng/?limit=1"
_REQUEST_TIMEOUT = 8


class SentimentAnalyzer:
    """
    Fetches and analyzes Fear & Greed Index and CryptoPanic news.
    Results are cached internally — safe to call every 60s cycle.
    """

    def __init__(self):
        self._fg_cache:       Optional[SentimentResult] = None
        self._fg_cached_at:   Optional[datetime]        = None
        self._news_cache:     Optional[SentimentResult] = None
        self._news_cached_at: Optional[datetime]        = None

        self._fg_ttl_min   = 60
        self._news_ttl_min = config.NEWS_POLL_INTERVAL_MIN

    def analyze(self) -> SentimentResult:
        """
        Returns merged SentimentResult (F&G + news).
        Uses cache when TTL has not expired. Never raises.
        """
        fg   = self._get_fear_greed()
        news = self._get_news()

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
            entry = resp.json()["data"][0]
            value = int(entry["value"])
            label = entry["value_classification"]
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
            return ["EXTREME_FEAR"]
        if value <= config.FEAR_GREED_FEAR:
            return ["FEAR"]
        if value >= config.FEAR_GREED_EXTREME_GREED:
            return ["EXTREME_GREED"]
        if value >= config.FEAR_GREED_GREED:
            return ["GREED"]
        return []

    # ─── CryptoPanic News ─────────────────────────────────────────────────────

    def _get_news(self) -> SentimentResult:
        if not config.CRYPTOPANIC_API_KEY:
            log.debug("CryptoPanic: API key not set — skipping")
            return SentimentResult()
        if self._is_fresh(self._news_cached_at, self._news_ttl_min):
            return self._news_cache  # type: ignore[return-value]
        result = self._fetch_news()
        self._news_cache     = result
        self._news_cached_at = datetime.now(timezone.utc)
        return result

    def _fetch_news(self) -> SentimentResult:
        """
        Fetch bullish-tagged and bearish-tagged BTC posts separately.
        Signal = directional dominance by post count (last 24h).
        No vote counting — CryptoPanic's filter=bullish/bearish tags
        are applied by their editorial team and are far more reliable.
        """
        try:
            bullish_count = self._count_posts(filter_="bullish")
            bearish_count = self._count_posts(filter_="bearish")
            sentiment, signals = self._news_signals(bullish_count, bearish_count)

            log.info(
                "CryptoPanic: bullish=%d bearish=%d → %s",
                bullish_count, bearish_count, sentiment,
            )
            return SentimentResult(
                news_sentiment=sentiment,
                bullish_news_count=bullish_count,
                bearish_news_count=bearish_count,
                signals=signals,
            )
        except Exception as exc:
            log.warning("CryptoPanic fetch failed: %s", exc)
            return SentimentResult()

    def _count_posts(self, filter_: str) -> int:
        """
        Fetch posts with a given filter and count those from the last 24 hours.
        Uses public=true so auth_token only needs free-tier access.
        """
        resp = requests.get(
            config.CRYPTOPANIC_BASE_URL,
            params={
                "auth_token": config.CRYPTOPANIC_API_KEY,
                "currencies": "BTC",
                "public":     "true",
                "filter":     filter_,
                "kind":       "news",
            },
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        posts = resp.json().get("results", [])

        # Count only posts from the last 24 hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        count = 0
        for post in posts:
            published = post.get("published_at", "")
            if not published:
                continue
            try:
                # CryptoPanic returns ISO format: "2024-01-15T10:30:00Z"
                ts = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if ts >= cutoff:
                    count += 1
            except ValueError:
                count += 1  # if parse fails, count it anyway
        return count

    def _news_signals(self, bullish: int, bearish: int) -> tuple[str, list[str]]:
        """
        Signal fires when directional dominance exceeds threshold.
        Requires minimum posts on the dominant side to avoid false signals
        on very quiet news days.
        """
        diff = bullish - bearish

        if bullish >= config.NEWS_MIN_POSTS and diff >= config.NEWS_DIRECTION_THRESHOLD:
            return "BULLISH", ["NEWS_BULLISH_MAJOR"]
        if bearish >= config.NEWS_MIN_POSTS and -diff >= config.NEWS_DIRECTION_THRESHOLD:
            return "BEARISH", ["NEWS_BEARISH_MAJOR"]
        return "NEUTRAL", []

    # ─── Cache helper ─────────────────────────────────────────────────────────

    @staticmethod
    def _is_fresh(cached_at: Optional[datetime], ttl_min: int) -> bool:
        if cached_at is None:
            return False
        return datetime.now(timezone.utc) - cached_at < timedelta(minutes=ttl_min)
