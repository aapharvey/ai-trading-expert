"""
Phase 2 tests: SentimentAnalyzer — Fear & Greed + CryptoPanic news.
CryptoPanic: filter=bullish/bearish approach (no vote counting).
All external calls mocked.
"""

import json
from datetime import datetime, timedelta, timezone
import pytest
import requests

from src.analyzers.sentiment import SentimentAnalyzer
from src.models.signals import SentimentResult
import config


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_fg_response(value: int, label: str = "Fear") -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps({
        "data": [{"value": str(value), "value_classification": label}]
    }).encode()
    return resp


def make_news_response(posts: list[dict]) -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps({"results": posts}).encode()
    return resp


def recent_post(hours_ago: int = 1) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {"title": "BTC news", "published_at": ts}


def old_post() -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {"title": "Old BTC news", "published_at": ts}


# ─── Tests: Fear & Greed ────────────────────────────────────────────────────

class TestFearGreed:
    def test_extreme_fear_signal(self, mocker):
        mocker.patch("requests.get", return_value=make_fg_response(10, "Extreme Fear"))
        result = SentimentAnalyzer()._fetch_fear_greed()
        assert result.fear_greed_value == 10
        assert "EXTREME_FEAR" in result.signals

    def test_boundary_extreme_fear(self, mocker):
        # Exactly at threshold (20) → EXTREME_FEAR
        mocker.patch("requests.get", return_value=make_fg_response(20, "Extreme Fear"))
        result = SentimentAnalyzer()._fetch_fear_greed()
        assert "EXTREME_FEAR" in result.signals

    def test_fear_signal(self, mocker):
        mocker.patch("requests.get", return_value=make_fg_response(35, "Fear"))
        result = SentimentAnalyzer()._fetch_fear_greed()
        assert "FEAR" in result.signals
        assert "EXTREME_FEAR" not in result.signals

    def test_extreme_greed_signal(self, mocker):
        mocker.patch("requests.get", return_value=make_fg_response(85, "Extreme Greed"))
        result = SentimentAnalyzer()._fetch_fear_greed()
        assert "EXTREME_GREED" in result.signals

    def test_greed_signal(self, mocker):
        mocker.patch("requests.get", return_value=make_fg_response(65, "Greed"))
        result = SentimentAnalyzer()._fetch_fear_greed()
        assert "GREED" in result.signals
        assert "EXTREME_GREED" not in result.signals

    def test_neutral_no_signal(self, mocker):
        mocker.patch("requests.get", return_value=make_fg_response(50, "Neutral"))
        result = SentimentAnalyzer()._fetch_fear_greed()
        assert result.signals == []

    def test_api_failure_returns_empty(self, mocker):
        mocker.patch("requests.get", side_effect=requests.ConnectionError())
        result = SentimentAnalyzer()._fetch_fear_greed()
        assert isinstance(result, SentimentResult)
        assert result.fear_greed_value is None
        assert result.signals == []

    def test_label_stored(self, mocker):
        mocker.patch("requests.get", return_value=make_fg_response(10, "Extreme Fear"))
        result = SentimentAnalyzer()._fetch_fear_greed()
        assert result.fear_greed_label == "Extreme Fear"


# ─── Tests: F&G caching ──────────────────────────────────────────────────────

class TestFearGreedCaching:
    def test_cached_after_first_fetch(self, mocker):
        mock_get = mocker.patch("requests.get", return_value=make_fg_response(25, "Fear"))
        analyzer = SentimentAnalyzer()
        analyzer._get_fear_greed()
        analyzer._get_fear_greed()
        assert mock_get.call_count == 1

    def test_cache_expires(self, mocker):
        mock_get = mocker.patch("requests.get", return_value=make_fg_response(25, "Fear"))
        analyzer = SentimentAnalyzer()
        analyzer._get_fear_greed()
        analyzer._fg_cached_at = datetime.now(timezone.utc) - timedelta(minutes=61)
        analyzer._get_fear_greed()
        assert mock_get.call_count == 2


# ─── Tests: CryptoPanic — new filter-based logic ──────────────────────────────

class TestCryptoPanicFilterLogic:
    def test_bullish_dominant_signal(self, mocker):
        """5 recent bullish posts, 1 bearish → bullish dominance."""
        config.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_news_response([recent_post()] * 5),  # filter=bullish → 5 posts
                make_news_response([recent_post()] * 1),  # filter=bearish → 1 post
            ],
        )
        analyzer = SentimentAnalyzer()
        result = analyzer._fetch_news()
        assert result.bullish_news_count == 5
        assert result.bearish_news_count == 1
        assert "NEWS_BULLISH_MAJOR" in result.signals

    def test_bearish_dominant_signal(self, mocker):
        """1 bullish, 5 bearish → bearish dominance."""
        config.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_news_response([recent_post()] * 1),  # bullish
                make_news_response([recent_post()] * 5),  # bearish
            ],
        )
        result = SentimentAnalyzer()._fetch_news()
        assert "NEWS_BEARISH_MAJOR" in result.signals

    def test_balanced_no_signal(self, mocker):
        """3 bullish, 3 bearish → no dominant signal."""
        config.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_news_response([recent_post()] * 3),
                make_news_response([recent_post()] * 3),
            ],
        )
        result = SentimentAnalyzer()._fetch_news()
        assert result.signals == []

    def test_old_posts_not_counted(self, mocker):
        """Posts older than 24h are excluded from count."""
        config.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                # 2 recent + 5 old = only 2 counted
                make_news_response([recent_post()] * 2 + [old_post()] * 5),
                make_news_response([]),
            ],
        )
        result = SentimentAnalyzer()._fetch_news()
        assert result.bullish_news_count == 2

    def test_skipped_when_no_api_key(self, mocker):
        original = config.CRYPTOPANIC_API_KEY
        config.CRYPTOPANIC_API_KEY = ""
        try:
            mock_get = mocker.patch("requests.get")
            result = SentimentAnalyzer()._get_news()
            mock_get.assert_not_called()
            assert result.signals == []
        finally:
            config.CRYPTOPANIC_API_KEY = original

    def test_api_failure_returns_empty(self, mocker):
        config.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch("requests.get", side_effect=requests.ConnectionError())
        result = SentimentAnalyzer()._fetch_news()
        assert isinstance(result, SentimentResult)
        assert result.signals == []

    def test_minimum_posts_required(self, mocker):
        """0 bullish posts with dominance still blocked by NEWS_MIN_POSTS."""
        config.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_news_response([]),   # 0 bullish
                make_news_response([recent_post()] * 4),  # 4 bearish
            ],
        )
        result = SentimentAnalyzer()._fetch_news()
        # bearish=4 but bullish=0 < NEWS_MIN_POSTS → bearish should fire
        assert "NEWS_BEARISH_MAJOR" in result.signals


# ─── Tests: _news_signals logic ───────────────────────────────────────────────

class TestNewsSignalLogic:
    def test_bullish_exactly_at_threshold(self):
        analyzer = SentimentAnalyzer()
        # diff = 3 (= NEWS_DIRECTION_THRESHOLD), bullish=3 (= NEWS_MIN_POSTS)
        sentiment, signals = analyzer._news_signals(bullish=3, bearish=0)
        assert "NEWS_BULLISH_MAJOR" in signals

    def test_bullish_below_threshold(self):
        analyzer = SentimentAnalyzer()
        sentiment, signals = analyzer._news_signals(bullish=2, bearish=1)
        assert signals == []

    def test_bearish_exactly_at_threshold(self):
        analyzer = SentimentAnalyzer()
        sentiment, signals = analyzer._news_signals(bullish=0, bearish=3)
        assert "NEWS_BEARISH_MAJOR" in signals

    def test_zero_posts_neutral(self):
        analyzer = SentimentAnalyzer()
        sentiment, signals = analyzer._news_signals(0, 0)
        assert sentiment == "NEUTRAL"
        assert signals == []


# ─── Tests: Full analyze() ────────────────────────────────────────────────────

class TestFullAnalyze:
    def test_returns_sentiment_result(self, mocker):
        mocker.patch("requests.get", return_value=make_fg_response(25, "Fear"))
        config.CRYPTOPANIC_API_KEY = ""
        result = SentimentAnalyzer().analyze()
        assert isinstance(result, SentimentResult)

    def test_signals_merged(self, mocker):
        config.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_fg_response(10, "Extreme Fear"),
                make_news_response([recent_post()] * 5),   # bullish
                make_news_response([recent_post()] * 1),   # bearish
            ],
        )
        result = SentimentAnalyzer().analyze()
        assert "EXTREME_FEAR" in result.signals
        assert "NEWS_BULLISH_MAJOR" in result.signals

    def test_graceful_on_total_failure(self, mocker):
        mocker.patch("requests.get", side_effect=Exception("total failure"))
        result = SentimentAnalyzer().analyze()
        assert isinstance(result, SentimentResult)
        assert result.signals == []
