"""
Phase 2 tests: SentimentAnalyzer — Fear & Greed + CryptoPanic news.
All external calls mocked.
"""

import json
from datetime import datetime, timedelta, timezone
import pytest
import requests

from src.analyzers.sentiment import SentimentAnalyzer
from src.models.signals import SentimentResult


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_fg_response(value: int, label: str = "Fear") -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps({
        "data": [
            {"value": str(value), "value_classification": label},
            {"value": str(value - 5), "value_classification": label},
        ]
    }).encode()
    return resp


def make_news_response(posts: list[dict]) -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps({"results": posts}).encode()
    return resp


def bullish_post():
    return {"title": "BTC bullish", "votes": {"positive": 10, "negative": 2}}


def bearish_post():
    return {"title": "BTC crash", "votes": {"positive": 1, "negative": 8}}


def neutral_post():
    return {"title": "BTC update", "votes": {"positive": 3, "negative": 3}}


# ─── Tests: Fear & Greed ────────────────────────────────────────────────────

class TestFearGreed:
    def test_extreme_fear_signal(self, mocker):
        mocker.patch("requests.get", return_value=make_fg_response(15, "Extreme Fear"))
        result = SentimentAnalyzer()._fetch_fear_greed()
        assert result.fear_greed_value == 15
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

    def test_label_stored_correctly(self, mocker):
        mocker.patch("requests.get", return_value=make_fg_response(10, "Extreme Fear"))
        result = SentimentAnalyzer()._fetch_fear_greed()
        assert result.fear_greed_label == "Extreme Fear"


# ─── Tests: Fear & Greed caching ─────────────────────────────────────────────

class TestFearGreedCaching:
    def test_result_cached_after_first_fetch(self, mocker):
        mock_get = mocker.patch("requests.get", return_value=make_fg_response(25, "Fear"))
        analyzer = SentimentAnalyzer()
        analyzer._get_fear_greed()
        analyzer._get_fear_greed()  # Second call should use cache
        assert mock_get.call_count == 1

    def test_cache_expires_after_ttl(self, mocker):
        mock_get = mocker.patch("requests.get", return_value=make_fg_response(25, "Fear"))
        analyzer = SentimentAnalyzer()
        analyzer._get_fear_greed()
        # Expire cache
        analyzer._fg_cached_at = datetime.now(timezone.utc) - timedelta(minutes=61)
        analyzer._get_fear_greed()
        assert mock_get.call_count == 2


# ─── Tests: CryptoPanic news ─────────────────────────────────────────────────

class TestCryptoPanicNews:
    def test_bullish_news_signal(self, mocker):
        mocker.patch("requests.get", return_value=make_news_response([bullish_post()] * 8 + [neutral_post()] * 2))
        mocker.patch("config.CRYPTOPANIC_API_KEY", "test_key")
        import importlib, src.analyzers.sentiment as m
        importlib.reload(m)
        analyzer = m.SentimentAnalyzer()
        import config as cfg
        cfg.CRYPTOPANIC_API_KEY = "test_key"
        result = analyzer._fetch_news()
        assert result.bullish_news_count >= 6
        assert "NEWS_BULLISH_MAJOR" in result.signals

    def test_bearish_news_signal(self, mocker):
        import config as cfg
        cfg.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch("requests.get", return_value=make_news_response([bearish_post()] * 8 + [neutral_post()] * 2))
        analyzer = SentimentAnalyzer()
        result = analyzer._fetch_news()
        assert result.bearish_news_count >= 6
        assert "NEWS_BEARISH_MAJOR" in result.signals

    def test_mixed_news_neutral(self, mocker):
        import config as cfg
        cfg.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch("requests.get", return_value=make_news_response(
            [bullish_post()] * 4 + [bearish_post()] * 4 + [neutral_post()] * 2
        ))
        analyzer = SentimentAnalyzer()
        result = analyzer._fetch_news()
        assert result.signals == []

    def test_skipped_when_no_api_key(self, mocker):
        import config as cfg
        original = cfg.CRYPTOPANIC_API_KEY
        cfg.CRYPTOPANIC_API_KEY = ""
        try:
            mock_get = mocker.patch("requests.get")
            result = SentimentAnalyzer()._get_news()
            mock_get.assert_not_called()
            assert result.signals == []
        finally:
            cfg.CRYPTOPANIC_API_KEY = original

    def test_api_failure_returns_empty(self, mocker):
        import config as cfg
        cfg.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch("requests.get", side_effect=requests.ConnectionError())
        result = SentimentAnalyzer()._fetch_news()
        assert isinstance(result, SentimentResult)
        assert result.signals == []


# ─── Tests: News signal logic ─────────────────────────────────────────────────

class TestNewsSignalLogic:
    def test_bullish_threshold_exact(self):
        analyzer = SentimentAnalyzer()
        sentiment, signals = analyzer._news_signals(bullish=6, bearish=2, total=10)
        assert "NEWS_BULLISH_MAJOR" in signals

    def test_bullish_below_threshold(self):
        analyzer = SentimentAnalyzer()
        sentiment, signals = analyzer._news_signals(bullish=4, bearish=1, total=10)
        assert "NEWS_BULLISH_MAJOR" not in signals

    def test_empty_posts(self):
        analyzer = SentimentAnalyzer()
        sentiment, signals = analyzer._news_signals(0, 0, 0)
        assert sentiment == "NEUTRAL"
        assert signals == []


# ─── Tests: Full analyze() pipeline ──────────────────────────────────────────

class TestFullAnalyze:
    def test_returns_sentiment_result(self, mocker):
        mocker.patch("requests.get", return_value=make_fg_response(25, "Fear"))
        import config as cfg
        cfg.CRYPTOPANIC_API_KEY = ""
        result = SentimentAnalyzer().analyze()
        assert isinstance(result, SentimentResult)

    def test_signals_merged_from_fg_and_news(self, mocker):
        import config as cfg
        cfg.CRYPTOPANIC_API_KEY = "test_key"
        mocker.patch(
            "requests.get",
            side_effect=[
                make_fg_response(15, "Extreme Fear"),
                make_news_response([bullish_post()] * 8 + [neutral_post()] * 2),
            ],
        )
        result = SentimentAnalyzer().analyze()
        assert "EXTREME_FEAR" in result.signals
        assert "NEWS_BULLISH_MAJOR" in result.signals

    def test_graceful_on_all_failures(self, mocker):
        mocker.patch("requests.get", side_effect=Exception("total failure"))
        result = SentimentAnalyzer().analyze()
        assert isinstance(result, SentimentResult)
        assert result.signals == []
