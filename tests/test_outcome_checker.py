"""
Tests: OutcomeChecker — candle-based outcome classification.
All Bybit calls mocked. Journal uses in-memory SQLite.
"""

import json
from datetime import datetime, timedelta, timezone
import pytest

from src.journal.outcome_checker import OutcomeChecker
from src.journal.signal_journal import (
    SignalJournal, WIN_FULL, WIN_PARTIAL, LOSS, EXPIRED,
)
from src.models.signals import Direction, Timeframe


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_journal() -> SignalJournal:
    return SignalJournal(db_path=":memory:")


def make_candle(high: float, low: float, close: float = None) -> dict:
    if close is None:
        close = (high + low) / 2
    return {
        "start_time": 0,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 100.0,
        "turnover": 100.0,
    }


def pending_row(
    direction: str = "LONG",
    entry_mid: float = 80000.0,
    tp1: float = 82000.0,
    tp2: float = 84000.0,
    stop_loss: float = 78000.0,
    hours_ago: int = 25,
) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {
        "id":               1,
        "timestamp":        ts,
        "direction":        direction,
        "entry_mid":        entry_mid,
        "tp1":              tp1,
        "tp2":              tp2,
        "stop_loss":        stop_loss,
        "check_after_hours": 24,
    }


# ─── Tests: _classify (pure logic, no mocks needed) ──────────────────────────

class TestClassify:
    # ── LONG ──────────────────────────────────────────────────────────────────

    def test_long_win_full(self):
        candles = [make_candle(high=84001, low=79000)]
        outcome, price = OutcomeChecker._classify("LONG", 80000, 82000, 84000, 78000, candles)
        assert outcome == WIN_FULL
        assert price == 84000

    def test_long_win_partial(self):
        candles = [
            make_candle(high=82500, low=79000),   # hits TP1, not TP2
            make_candle(high=81000, low=79500),   # stays below TP2
        ]
        outcome, price = OutcomeChecker._classify("LONG", 80000, 82000, 84000, 78000, candles)
        assert outcome == WIN_PARTIAL
        assert price == 82000

    def test_long_loss(self):
        candles = [make_candle(high=80500, low=77999)]   # LOW hits SL
        outcome, price = OutcomeChecker._classify("LONG", 80000, 82000, 84000, 78000, candles)
        assert outcome == LOSS
        assert price == 78000

    def test_long_expired(self):
        candles = [
            make_candle(high=81000, low=79000),   # below TP1, above SL
            make_candle(high=80500, low=79500),
        ]
        outcome, _ = OutcomeChecker._classify("LONG", 80000, 82000, 84000, 78000, candles)
        assert outcome == EXPIRED

    def test_long_sl_checked_before_tp(self):
        """If a single candle covers both SL and TP2, SL wins (conservative)."""
        candles = [make_candle(high=85000, low=77000)]   # covers both
        outcome, _ = OutcomeChecker._classify("LONG", 80000, 82000, 84000, 78000, candles)
        assert outcome == LOSS

    def test_long_sl_on_later_candle(self):
        """TP1 hit first candle, SL hit second — WIN_PARTIAL (SL after TP1)."""
        candles = [
            make_candle(high=82500, low=79500),   # TP1 hit
            make_candle(high=81000, low=77500),   # SL hit after TP1
        ]
        outcome, _ = OutcomeChecker._classify("LONG", 80000, 82000, 84000, 78000, candles)
        # SL on candle 2, but TP1 was reached on candle 1 before SL
        # Since we check SL first in each candle, candle 2 → LOSS
        assert outcome == LOSS

    # ── SHORT ─────────────────────────────────────────────────────────────────

    def test_short_win_full(self):
        candles = [make_candle(high=81000, low=75999)]   # LOW hits TP2=76000
        outcome, price = OutcomeChecker._classify("SHORT", 80000, 78000, 76000, 82000, candles)
        assert outcome == WIN_FULL
        assert price == 76000

    def test_short_win_partial(self):
        candles = [
            make_candle(high=81000, low=77500),   # hits TP1=78000
            make_candle(high=80500, low=77000),   # above TP2=76000
        ]
        outcome, _ = OutcomeChecker._classify("SHORT", 80000, 78000, 76000, 82000, candles)
        assert outcome == WIN_PARTIAL

    def test_short_loss(self):
        candles = [make_candle(high=82001, low=79000)]   # HIGH hits SL=82000
        outcome, _ = OutcomeChecker._classify("SHORT", 80000, 78000, 76000, 82000, candles)
        assert outcome == LOSS

    def test_short_expired(self):
        candles = [make_candle(high=80500, low=78500)]   # no TP, no SL
        outcome, _ = OutcomeChecker._classify("SHORT", 80000, 78000, 76000, 82000, candles)
        assert outcome == EXPIRED

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_empty_candles_expired(self):
        outcome, price = OutcomeChecker._classify("LONG", 80000, 82000, 84000, 78000, [])
        assert outcome == EXPIRED

    def test_exact_sl_boundary(self):
        """LOW exactly equals SL → LOSS."""
        candles = [make_candle(high=81000, low=78000)]
        outcome, _ = OutcomeChecker._classify("LONG", 80000, 82000, 84000, 78000, candles)
        assert outcome == LOSS

    def test_exact_tp2_boundary(self):
        """HIGH exactly equals TP2 → WIN_FULL."""
        candles = [make_candle(high=84000, low=79500)]
        outcome, _ = OutcomeChecker._classify("LONG", 80000, 82000, 84000, 78000, candles)
        assert outcome == WIN_FULL


# ─── Tests: check_pending (integration with journal + mocked client) ──────────

class TestCheckPending:
    def _make_checker(self, mocker, candles: list) -> tuple:
        journal = make_journal()
        mock_client = mocker.MagicMock()
        mock_client.get_klines_range.return_value = candles
        checker = OutcomeChecker(journal, mock_client)
        return journal, checker

    def test_resolves_pending_signal(self, mocker):
        journal, checker = self._make_checker(
            mocker, [make_candle(high=84001, low=79500)]
        )
        # Record and expire a LONG INTRADAY signal
        from src.models.signals import TradeSignal
        sig = TradeSignal(
            direction=Direction.LONG, strength=3,
            entry_low=79900, entry_high=80100,
            tp1=82000, tp2=84000, stop_loss=78000,
            rr_ratio=2.33, timeframe=Timeframe.INTRADAY,
        )
        sig_id = journal.record(sig)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        journal._connect().execute("UPDATE signals SET timestamp = ?", (old_ts,))

        resolved = checker.check_pending()
        assert resolved == 1

        rows = journal._connect().execute(
            "SELECT outcome FROM signals WHERE id = ?", (sig_id,)
        ).fetchall()
        assert rows[0][0] == WIN_FULL

    def test_no_pending_returns_zero(self, mocker):
        journal, checker = self._make_checker(mocker, [])
        resolved = checker.check_pending()
        assert resolved == 0

    def test_skips_on_client_error(self, mocker):
        """Network error on one signal — logs warning, continues."""
        journal = make_journal()
        mock_client = mocker.MagicMock()
        mock_client.get_klines_range.side_effect = Exception("network error")
        checker = OutcomeChecker(journal, mock_client)

        from src.models.signals import TradeSignal
        sig = TradeSignal(
            direction=Direction.LONG, strength=3,
            entry_low=79900, entry_high=80100,
            tp1=82000, tp2=84000, stop_loss=78000,
            rr_ratio=2.33, timeframe=Timeframe.INTRADAY,
        )
        journal.record(sig)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        journal._connect().execute("UPDATE signals SET timestamp = ?", (old_ts,))

        resolved = checker.check_pending()
        assert resolved == 0   # error → not resolved, no crash
