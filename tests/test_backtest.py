"""
Tests for backtest engine: data helpers, outcome resolution, statistics.
"""

import pytest
from scripts.backtest import (
    to_4h,
    make_fake_oi,
    make_fake_funding,
    get_fg_signals,
    resolve_outcome,
    compute_stats,
    BacktestSignal,
)
from src.models.signals import Direction


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_candle(ts=0, open_=100.0, high=105.0, low=95.0, close=102.0, volume=10.0):
    return {"timestamp": ts, "open": open_, "high": high, "low": low,
            "close": close, "volume": volume}


def make_signal(direction=Direction.LONG, entry=100.0, tp1=110.0, tp2=120.0, sl=90.0):
    return BacktestSignal(
        timestamp=0,
        direction=direction,
        strength=3,
        entry_mid=entry,
        tp1=tp1,
        tp2=tp2,
        stop_loss=sl,
        rr_ratio=2.0,
        factors=[],
    )


# ─── to_4h ────────────────────────────────────────────────────────────────────

class TestTo4h:
    def test_groups_4_candles(self):
        candles = [make_candle(ts=i * 3600000) for i in range(8)]
        result = to_4h(candles)
        assert len(result) == 2

    def test_high_is_max_of_group(self):
        candles = [
            make_candle(ts=0,           high=110.0),
            make_candle(ts=3600000,     high=115.0),
            make_candle(ts=7200000,     high=108.0),
            make_candle(ts=10800000,    high=112.0),
        ]
        result = to_4h(candles)
        assert result[0]["high"] == 115.0

    def test_low_is_min_of_group(self):
        candles = [
            make_candle(ts=i * 3600000, low=100.0 - i) for i in range(4)
        ]
        result = to_4h(candles)
        assert result[0]["low"] == 97.0

    def test_volume_is_sum(self):
        candles = [make_candle(ts=i * 3600000, volume=5.0) for i in range(4)]
        result = to_4h(candles)
        assert result[0]["volume"] == 20.0

    def test_open_is_first_candle(self):
        candles = [make_candle(ts=i * 3600000, open_=100.0 + i) for i in range(4)]
        result = to_4h(candles)
        assert result[0]["open"] == 100.0

    def test_close_is_last_candle(self):
        candles = [make_candle(ts=i * 3600000, close=100.0 + i) for i in range(4)]
        result = to_4h(candles)
        assert result[0]["close"] == 103.0

    def test_incomplete_group_excluded(self):
        candles = [make_candle(ts=i * 3600000) for i in range(6)]
        result = to_4h(candles)
        assert len(result) == 1   # only one full group of 4


# ─── Fake OI / Funding ────────────────────────────────────────────────────────

class TestFakeHelpers:
    def test_fake_oi_length(self):
        candles = [make_candle(ts=i) for i in range(15)]
        oi = make_fake_oi(candles)
        assert len(oi) == 10   # last 10

    def test_fake_oi_uses_volume(self):
        candles = [make_candle(ts=i, volume=float(i)) for i in range(15)]
        oi = make_fake_oi(candles)
        assert oi[-1]["open_interest"] == 14.0

    def test_fake_funding_neutral(self):
        candles = [make_candle(ts=i) for i in range(10)]
        funding = make_fake_funding(candles)
        assert all(f["funding_rate"] == 0.0 for f in funding)


# ─── Fear & Greed signals ─────────────────────────────────────────────────────

class TestGetFgSignals:
    FG = {"2024-04-15": 10, "2024-04-16": 30, "2024-04-17": 65, "2024-04-18": 85}

    def _ts(self, date: str) -> int:
        from datetime import datetime, timezone
        return int(datetime.strptime(date, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)

    def test_extreme_fear(self):
        assert get_fg_signals(self._ts("2024-04-15"), self.FG) == ["EXTREME_FEAR"]

    def test_fear(self):
        assert get_fg_signals(self._ts("2024-04-16"), self.FG) == ["FEAR"]

    def test_greed(self):
        assert get_fg_signals(self._ts("2024-04-17"), self.FG) == ["GREED"]

    def test_extreme_greed(self):
        assert get_fg_signals(self._ts("2024-04-18"), self.FG) == ["EXTREME_GREED"]

    def test_missing_date_returns_empty(self):
        assert get_fg_signals(self._ts("2020-01-01"), self.FG) == []


# ─── Outcome resolution ───────────────────────────────────────────────────────

class TestResolveOutcome:
    def test_long_tp1_hit(self):
        sig = make_signal(direction=Direction.LONG, entry=100, tp1=110, tp2=120, sl=90)
        future = [make_candle(high=111.0, low=98.0)]
        resolve_outcome(sig, future)
        assert sig.outcome == "WIN_TP1"
        assert sig.achieved_rr > 0

    def test_long_tp2_hit(self):
        sig = make_signal(direction=Direction.LONG, entry=100, tp1=110, tp2=120, sl=90)
        future = [make_candle(high=125.0, low=98.0)]
        resolve_outcome(sig, future)
        assert sig.outcome == "WIN_TP2"

    def test_long_sl_hit(self):
        sig = make_signal(direction=Direction.LONG, entry=100, tp1=110, tp2=120, sl=90)
        future = [make_candle(high=101.0, low=88.0)]
        resolve_outcome(sig, future)
        assert sig.outcome == "LOSS"
        assert sig.achieved_rr == -1.0

    def test_short_tp1_hit(self):
        sig = make_signal(direction=Direction.SHORT, entry=100, tp1=90, tp2=80, sl=110)
        future = [make_candle(high=102.0, low=89.0)]
        resolve_outcome(sig, future)
        assert sig.outcome == "WIN_TP1"

    def test_short_tp2_hit(self):
        sig = make_signal(direction=Direction.SHORT, entry=100, tp1=90, tp2=80, sl=110)
        future = [make_candle(high=102.0, low=78.0)]
        resolve_outcome(sig, future)
        assert sig.outcome == "WIN_TP2"

    def test_short_sl_hit(self):
        sig = make_signal(direction=Direction.SHORT, entry=100, tp1=90, tp2=80, sl=110)
        future = [make_candle(high=112.0, low=98.0)]
        resolve_outcome(sig, future)
        assert sig.outcome == "LOSS"

    def test_no_future_candles_is_open(self):
        sig = make_signal()
        resolve_outcome(sig, [])
        assert sig.outcome == "OPEN"

    def test_end_of_data_is_open(self):
        sig = make_signal(direction=Direction.LONG, entry=100, tp1=200, tp2=300, sl=50)
        future = [make_candle(high=105.0, low=99.0)]  # never hits tp1/sl
        resolve_outcome(sig, future)
        assert sig.outcome == "OPEN"

    def test_sl_before_tp_wins_sl(self):
        """SL candle comes before TP — should be LOSS."""
        sig = make_signal(direction=Direction.LONG, entry=100, tp1=110, tp2=120, sl=90)
        future = [
            make_candle(high=102.0, low=88.0),   # SL hit on candle 1
            make_candle(high=115.0, low=105.0),   # TP1 would hit on candle 2
        ]
        resolve_outcome(sig, future)
        assert sig.outcome == "LOSS"


# ─── Statistics ───────────────────────────────────────────────────────────────

class TestComputeStats:
    def _resolved_signals(self, outcomes: list[str]) -> list[BacktestSignal]:
        signals = []
        for outcome in outcomes:
            s = make_signal()
            s.outcome = outcome
            s.achieved_rr = 2.0 if "WIN" in outcome else -1.0
            signals.append(s)
        return signals

    def test_empty_signals(self):
        stats = compute_stats([], 1000)
        assert stats.total_signals == 0

    def test_counts_correct(self):
        signals = self._resolved_signals(["WIN_TP1", "WIN_TP2", "LOSS", "OPEN"])
        stats = compute_stats(signals, 1000)
        assert stats.win_tp1 == 1
        assert stats.win_tp2 == 1
        assert stats.loss    == 1
        assert stats.open_   == 1

    def test_avg_rr_calculation(self):
        signals = self._resolved_signals(["WIN_TP1", "WIN_TP2", "LOSS"])
        stats = compute_stats(signals, 1000)
        # (2.0 + 2.0 + (-1.0)) / 3 = 1.0
        assert stats.avg_rr == 1.0

    def test_max_drawdown_consecutive_losses(self):
        signals = self._resolved_signals(
            ["WIN_TP1", "LOSS", "LOSS", "LOSS", "WIN_TP1", "LOSS"]
        )
        stats = compute_stats(signals, 1000)
        assert stats.max_drawdown == 3

    def test_signals_per_week(self):
        # 168 candles = 1 week (1h), 7 signals → 7/week
        signals = self._resolved_signals(["WIN_TP1"] * 7)
        stats = compute_stats(signals, 168)
        assert stats.signals_per_week == 7.0

    def test_all_wins_no_warning_needed(self):
        signals = self._resolved_signals(["WIN_TP2"] * 10)
        stats = compute_stats(signals, 1680)
        assert stats.win_tp2 == 10
        assert stats.loss == 0
