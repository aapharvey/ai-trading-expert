"""
TASK-2 tests: BybitClient — all endpoints tested with mocked HTTP responses.
No real network calls are made.
"""

import json
import pytest
import requests

from src.bybit_client import BybitClient, BybitAPIError


# ─── Fixtures / helpers ───────────────────────────────────────────────────────

def make_response(body: dict, status_code: int = 200):
    """Create a mock requests.Response."""
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = json.dumps(body).encode()
    return resp


KLINE_ROW = ["1700000000000", "35000.0", "35500.0", "34800.0", "35200.0", "1200.5", "42000000.0"]

KLINE_BODY = {
    "retCode": 0, "retMsg": "OK",
    "result": {"list": [KLINE_ROW, KLINE_ROW]},
}

TICKER_BODY = {
    "retCode": 0, "retMsg": "OK",
    "result": {"list": [{
        "symbol": "BTCUSDT",
        "lastPrice": "35000.5",
        "markPrice": "35001.0",
        "indexPrice": "35000.0",
        "price24hPcnt": "0.025",
        "volume24h": "50000.0",
        "turnover24h": "1750000000.0",
        "bid1Price": "35000.0",
        "ask1Price": "35001.0",
    }]},
}

OI_BODY = {
    "retCode": 0, "retMsg": "OK",
    "result": {"list": [
        {"timestamp": "1700003600000", "openInterest": "18000.5"},
        {"timestamp": "1700000000000", "openInterest": "17500.0"},
    ]},
}

FUNDING_BODY = {
    "retCode": 0, "retMsg": "OK",
    "result": {"list": [
        {"fundingRateTimestamp": "1700000000000", "fundingRate": "0.0001", "markPrice": "35000.0"},
        {"fundingRateTimestamp": "1699971200000", "fundingRate": "-0.0002", "markPrice": "34800.0"},
    ]},
}

ORDERBOOK_BODY = {
    "retCode": 0, "retMsg": "OK",
    "result": {
        "b": [["35000.0", "1.5"], ["34999.0", "2.0"]],
        "a": [["35001.0", "1.2"], ["35002.0", "0.8"]],
        "ts": 1700000000000,
    },
}

ERROR_BODY = {"retCode": 10001, "retMsg": "Invalid symbol"}
HTTP_ERROR_BODY = {"retCode": 0, "retMsg": "OK", "result": {"list": []}}


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestGetKlines:
    def test_returns_candle_list(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(KLINE_BODY))
        client = BybitClient()
        candles = client.get_klines()
        assert isinstance(candles, list)
        assert len(candles) == 2

    def test_candle_has_required_keys(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(KLINE_BODY))
        client = BybitClient()
        candle = client.get_klines()[0]
        for key in ["start_time", "open", "high", "low", "close", "volume", "turnover"]:
            assert key in candle

    def test_candles_are_float(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(KLINE_BODY))
        client = BybitClient()
        c = client.get_klines()[0]
        assert isinstance(c["open"], float)
        assert isinstance(c["close"], float)

    def test_candles_sorted_oldest_first(self, mocker):
        # Bybit returns newest first — client must reverse
        body = {
            "retCode": 0, "retMsg": "OK",
            "result": {"list": [
                ["1700003600000", "35200.0", "35500.0", "34900.0", "35400.0", "100.0", "1000.0"],
                ["1700000000000", "35000.0", "35300.0", "34800.0", "35200.0", "120.0", "1200.0"],
            ]},
        }
        mocker.patch("requests.Session.get", return_value=make_response(body))
        candles = BybitClient().get_klines()
        assert candles[0]["start_time"] < candles[1]["start_time"]

    def test_api_error_raises(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(ERROR_BODY))
        with pytest.raises(BybitAPIError) as exc_info:
            BybitClient().get_klines()
        assert exc_info.value.ret_code == 10001

    def test_network_error_retries_and_raises(self, mocker):
        mocker.patch("requests.Session.get", side_effect=requests.ConnectionError("timeout"))
        mocker.patch("time.sleep")  # don't actually sleep in tests
        with pytest.raises(requests.RequestException):
            BybitClient().get_klines()


class TestGetTicker:
    def test_returns_ticker_dict(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(TICKER_BODY))
        ticker = BybitClient().get_ticker()
        assert ticker["last_price"] == 35000.5
        assert ticker["symbol"] == "BTCUSDT"

    def test_price_24h_pct_is_percentage(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(TICKER_BODY))
        ticker = BybitClient().get_ticker()
        assert abs(ticker["price_24h_pct"] - 2.5) < 0.001

    def test_empty_result_raises(self, mocker):
        body = {"retCode": 0, "retMsg": "OK", "result": {"list": []}}
        mocker.patch("requests.Session.get", return_value=make_response(body))
        with pytest.raises(ValueError):
            BybitClient().get_ticker()

    def test_all_required_keys_present(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(TICKER_BODY))
        ticker = BybitClient().get_ticker()
        for key in ["symbol", "last_price", "mark_price", "index_price",
                    "price_24h_pct", "volume_24h", "bid1_price", "ask1_price"]:
            assert key in ticker


class TestGetOpenInterest:
    def test_returns_oi_list(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(OI_BODY))
        oi = BybitClient().get_open_interest()
        assert isinstance(oi, list)
        assert len(oi) == 2

    def test_sorted_oldest_first(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(OI_BODY))
        oi = BybitClient().get_open_interest()
        assert oi[0]["timestamp"] < oi[1]["timestamp"]

    def test_open_interest_is_float(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(OI_BODY))
        oi = BybitClient().get_open_interest()
        assert isinstance(oi[0]["open_interest"], float)

    def test_api_error_raises(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(ERROR_BODY))
        with pytest.raises(BybitAPIError):
            BybitClient().get_open_interest()


class TestGetFundingRate:
    def test_returns_list(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(FUNDING_BODY))
        rates = BybitClient().get_funding_rate()
        assert len(rates) == 2

    def test_funding_rate_converted_to_pct(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(FUNDING_BODY))
        rates = BybitClient().get_funding_rate()
        # 0.0001 raw → 0.01 percent
        assert abs(rates[-1]["funding_rate"] - 0.01) < 0.0001

    def test_negative_funding_rate(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(FUNDING_BODY))
        rates = BybitClient().get_funding_rate()
        # -0.0002 raw → -0.02 percent (oldest first, so index 0)
        assert rates[0]["funding_rate"] < 0

    def test_sorted_oldest_first(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(FUNDING_BODY))
        rates = BybitClient().get_funding_rate()
        assert rates[0]["timestamp"] < rates[1]["timestamp"]


class TestGetOrderbook:
    def test_returns_orderbook_dict(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(ORDERBOOK_BODY))
        ob = BybitClient().get_orderbook()
        assert "bids" in ob
        assert "asks" in ob

    def test_best_bid_ask_calculated(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(ORDERBOOK_BODY))
        ob = BybitClient().get_orderbook()
        assert ob["best_bid"] == 35000.0
        assert ob["best_ask"] == 35001.0

    def test_spread_positive(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(ORDERBOOK_BODY))
        ob = BybitClient().get_orderbook()
        assert ob["spread"] > 0
        assert ob["spread_pct"] > 0

    def test_bids_descending(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(ORDERBOOK_BODY))
        ob = BybitClient().get_orderbook()
        prices = [b[0] for b in ob["bids"]]
        assert prices == sorted(prices, reverse=True)

    def test_asks_ascending(self, mocker):
        mocker.patch("requests.Session.get", return_value=make_response(ORDERBOOK_BODY))
        ob = BybitClient().get_orderbook()
        prices = [a[0] for a in ob["asks"]]
        assert prices == sorted(prices)


class TestRetryLogic:
    def test_retries_on_network_error(self, mocker):
        # Fail twice, succeed on third attempt
        success = make_response(TICKER_BODY)
        mock_get = mocker.patch(
            "requests.Session.get",
            side_effect=[
                requests.ConnectionError("err1"),
                requests.ConnectionError("err2"),
                success,
            ],
        )
        mocker.patch("time.sleep")
        ticker = BybitClient().get_ticker()
        assert ticker["last_price"] == 35000.5
        assert mock_get.call_count == 3

    def test_http_error_triggers_retry(self, mocker):
        err_resp = requests.Response()
        err_resp.status_code = 503
        err_resp._content = b"Service Unavailable"

        mocker.patch(
            "requests.Session.get",
            side_effect=[
                requests.HTTPError(response=err_resp),
                requests.HTTPError(response=err_resp),
                requests.HTTPError(response=err_resp),
            ],
        )
        mocker.patch("time.sleep")
        with pytest.raises(requests.RequestException):
            BybitClient().get_ticker()
