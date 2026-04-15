"""
TASK-3 tests: TelegramNotifier — all methods tested with mocked HTTP.
No real Telegram messages sent.
"""

import json
import pytest
import requests

from src.telegram_notifier import TelegramNotifier, TelegramError
from src.models.signals import TradeSignal, Direction, Timeframe


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_tg_response(ok: bool = True, status: int = 200, message_id: int = 42) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(
        {"ok": ok, "description": "Bad Request"} if not ok
        else {"ok": True, "result": {"message_id": message_id}}
    ).encode()
    return resp


def make_signal(direction=Direction.LONG, strength=4) -> TradeSignal:
    return TradeSignal(
        direction=direction,
        strength=strength,
        entry_low=95000.0,
        entry_high=95500.0,
        tp1=97000.0,
        tp2=99000.0,
        stop_loss=93500.0,
        rr_ratio=2.3,
        timeframe=Timeframe.SWING,
        factors=["Price at key support", "RSI oversold", "Funding negative", "OI long unwind"],
    )


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestTelegramNotifierInit:
    def test_creates_with_defaults(self):
        notifier = TelegramNotifier()
        assert notifier._token
        assert notifier._chat_id

    def test_creates_with_custom_params(self):
        notifier = TelegramNotifier(token="test_token", chat_id="12345")
        assert notifier._token == "test_token"
        assert notifier._chat_id == "12345"


class TestSendSignal:
    def test_returns_message_id_on_success(self, mocker):
        mocker.patch("requests.Session.post", return_value=make_tg_response(ok=True, message_id=99))
        notifier = TelegramNotifier(token="tok", chat_id="123")
        result = notifier.send_signal(make_signal())
        assert result == 99

    def test_message_contains_direction(self, mocker):
        mock_post = mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        notifier.send_signal(make_signal(direction=Direction.LONG))
        payload = mock_post.call_args[1]["json"]
        assert "LONG" in payload["text"]

    def test_message_contains_entry_price(self, mocker):
        mock_post = mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        notifier.send_signal(make_signal())
        payload = mock_post.call_args[1]["json"]
        assert "95,000" in payload["text"]

    def test_message_contains_stop_loss(self, mocker):
        mock_post = mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        notifier.send_signal(make_signal())
        payload = mock_post.call_args[1]["json"]
        assert "93,500" in payload["text"]

    def test_message_contains_rr_ratio(self, mocker):
        mock_post = mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        notifier.send_signal(make_signal())
        payload = mock_post.call_args[1]["json"]
        assert "2.3" in payload["text"]

    def test_message_contains_confluence_factors(self, mocker):
        mock_post = mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        notifier.send_signal(make_signal())
        payload = mock_post.call_args[1]["json"]
        assert "RSI oversold" in payload["text"]

    def test_short_signal_has_correct_emoji(self, mocker):
        mock_post = mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        notifier.send_signal(make_signal(direction=Direction.SHORT))
        payload = mock_post.call_args[1]["json"]
        assert "📉" in payload["text"]

    def test_returns_none_on_telegram_error(self, mocker):
        mocker.patch("requests.Session.post", return_value=make_tg_response(ok=False))
        notifier = TelegramNotifier(token="tok", chat_id="123")
        result = notifier.send_signal(make_signal())
        assert result is None

    def test_returns_none_on_network_error(self, mocker):
        mocker.patch("requests.Session.post", side_effect=requests.ConnectionError())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        result = notifier.send_signal(make_signal())
        assert result is None

    def test_strength_stars_correct(self, mocker):
        mock_post = mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        notifier.send_signal(make_signal(strength=3))
        payload = mock_post.call_args[1]["json"]
        assert "⭐⭐⭐☆☆" in payload["text"]


class TestSendAlert:
    def test_returns_true_on_success(self, mocker):
        mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        assert notifier.send_alert("test message") is True

    def test_message_contains_text(self, mocker):
        mock_post = mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        notifier.send_alert("System started")
        payload = mock_post.call_args[1]["json"]
        assert "System started" in payload["text"]

    def test_returns_false_on_failure(self, mocker):
        mocker.patch("requests.Session.post", side_effect=requests.ConnectionError())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        assert notifier.send_alert("test") is False


class TestSendHeartbeat:
    def test_returns_true_on_success(self, mocker):
        mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        assert notifier.send_heartbeat(95000.0, "BULLISH", 55.3, 0.012) is True

    def test_message_contains_price(self, mocker):
        mock_post = mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        notifier.send_heartbeat(95000.0, "BULLISH", 55.3, 0.012)
        payload = mock_post.call_args[1]["json"]
        assert "95,000" in payload["text"]


class TestMessageTruncation:
    def test_long_message_truncated(self, mocker):
        mock_post = mocker.patch("requests.Session.post", return_value=make_tg_response())
        notifier = TelegramNotifier(token="tok", chat_id="123")
        long_text = "x" * 5000
        notifier._send(long_text)
        payload = mock_post.call_args[1]["json"]
        assert len(payload["text"]) <= 4096
        assert "[truncated]" in payload["text"]
