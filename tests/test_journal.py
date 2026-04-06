"""
Tests: SignalJournal — SQLite persistence, pending checks, stats.
Uses in-memory SQLite to avoid file creation.
"""

import json
from datetime import datetime, timedelta, timezone
import pytest

from src.journal.signal_journal import (
    SignalJournal, WIN_FULL, WIN_PARTIAL, LOSS, EXPIRED,
)
from src.models.signals import TradeSignal, Direction, Timeframe


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_journal() -> SignalJournal:
    """In-memory journal for tests."""
    return SignalJournal(db_path=":memory:")


def make_signal(
    direction: Direction = Direction.LONG,
    strength: int = 3,
    timeframe: Timeframe = Timeframe.INTRADAY,
    entry_low: float = 79900.0,
    entry_high: float = 80100.0,
    tp1: float = 82000.0,
    tp2: float = 84000.0,
    stop_loss: float = 78800.0,
) -> TradeSignal:
    risk = abs((entry_low + entry_high) / 2 - stop_loss)
    reward = abs(tp2 - (entry_low + entry_high) / 2)
    return TradeSignal(
        direction=direction,
        strength=strength,
        entry_low=entry_low,
        entry_high=entry_high,
        tp1=tp1,
        tp2=tp2,
        stop_loss=stop_loss,
        rr_ratio=round(reward / risk, 2),
        timeframe=timeframe,
        factors=["EMA_CROSS_UP", "RSI_OVERSOLD"],
    )


# ─── Tests: record ────────────────────────────────────────────────────────────

class TestRecord:
    def test_record_returns_id(self):
        j = make_journal()
        sig_id = j.record(make_signal())
        assert isinstance(sig_id, int)
        assert sig_id >= 1

    def test_record_increments_id(self):
        j = make_journal()
        id1 = j.record(make_signal())
        id2 = j.record(make_signal())
        assert id2 > id1

    def test_intraday_check_hours_24(self):
        j = make_journal()
        j.record(make_signal(timeframe=Timeframe.INTRADAY))
        rows = j._connect().execute("SELECT check_after_hours FROM signals").fetchall()
        assert rows[0][0] == 24

    def test_swing_check_hours_48(self):
        j = make_journal()
        j.record(make_signal(timeframe=Timeframe.SWING))
        rows = j._connect().execute("SELECT check_after_hours FROM signals").fetchall()
        assert rows[0][0] == 48

    def test_entry_mid_stored(self):
        j = make_journal()
        j.record(make_signal(entry_low=79900.0, entry_high=80100.0))
        rows = j._connect().execute("SELECT entry_mid FROM signals").fetchall()
        assert abs(rows[0][0] - 80000.0) < 0.1

    def test_factors_stored_as_json(self):
        j = make_journal()
        sig = make_signal()
        j.record(sig)
        rows = j._connect().execute("SELECT factors FROM signals").fetchall()
        factors = json.loads(rows[0][0])
        assert factors == sig.factors

    def test_outcome_null_after_record(self):
        j = make_journal()
        j.record(make_signal())
        rows = j._connect().execute("SELECT outcome FROM signals").fetchall()
        assert rows[0][0] is None


# ─── Tests: get_pending_checks ────────────────────────────────────────────────

class TestPendingChecks:
    def test_not_pending_before_window(self):
        """Signal just recorded — check window not expired yet."""
        j = make_journal()
        j.record(make_signal(timeframe=Timeframe.INTRADAY))
        assert j.get_pending_checks() == []

    def test_pending_after_window_expired(self):
        """Manually backdate timestamp to simulate expired window."""
        j = make_journal()
        j.record(make_signal(timeframe=Timeframe.INTRADAY))
        # Backdate timestamp by 25 hours
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        j._connect().execute("UPDATE signals SET timestamp = ?", (old_ts,))
        pending = j.get_pending_checks()
        assert len(pending) == 1

    def test_not_pending_if_outcome_set(self):
        """Already resolved signals are excluded."""
        j = make_journal()
        sig_id = j.record(make_signal(timeframe=Timeframe.INTRADAY))
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        j._connect().execute("UPDATE signals SET timestamp = ?", (old_ts,))
        j.update_outcome(sig_id, WIN_FULL, 84000.0)
        assert j.get_pending_checks() == []

    def test_swing_pending_after_48h(self):
        j = make_journal()
        j.record(make_signal(timeframe=Timeframe.SWING))
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        j._connect().execute("UPDATE signals SET timestamp = ?", (old_ts,))
        assert len(j.get_pending_checks()) == 1

    def test_swing_not_pending_at_24h(self):
        """SWING requires 48h — not pending at 24h."""
        j = make_journal()
        j.record(make_signal(timeframe=Timeframe.SWING))
        ts_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        j._connect().execute("UPDATE signals SET timestamp = ?", (ts_24h,))
        assert j.get_pending_checks() == []


# ─── Tests: update_outcome ────────────────────────────────────────────────────

class TestUpdateOutcome:
    def test_outcome_written(self):
        j = make_journal()
        sig_id = j.record(make_signal())
        j.update_outcome(sig_id, WIN_FULL, 84000.0)
        rows = j._connect().execute(
            "SELECT outcome, exit_price FROM signals WHERE id = ?", (sig_id,)
        ).fetchall()
        assert rows[0][0] == WIN_FULL
        assert abs(rows[0][1] - 84000.0) < 0.1

    def test_checked_at_set(self):
        j = make_journal()
        sig_id = j.record(make_signal())
        j.update_outcome(sig_id, LOSS, 78800.0)
        rows = j._connect().execute(
            "SELECT checked_at FROM signals WHERE id = ?", (sig_id,)
        ).fetchall()
        assert rows[0][0] is not None


# ─── Tests: get_stats ────────────────────────────────────────────────────────

class TestStats:
    def _record_resolved(self, journal, outcome, direction=Direction.LONG, hours_ago=1):
        sig_id = journal.record(make_signal(direction=direction))
        ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
        journal._connect().execute("UPDATE signals SET timestamp = ?", (ts,))
        journal.update_outcome(sig_id, outcome, 80000.0)

    def test_empty_stats(self):
        j = make_journal()
        stats = j.get_stats(days=7)
        assert stats["total"] == 0

    def test_win_rate_calculation(self):
        j = make_journal()
        self._record_resolved(j, WIN_FULL)
        self._record_resolved(j, WIN_PARTIAL)
        self._record_resolved(j, LOSS)
        self._record_resolved(j, EXPIRED)
        stats = j.get_stats(days=7)
        assert stats["total"] == 4
        assert abs(stats["win_rate"] - 0.5) < 0.01  # 2/4

    def test_direction_counts(self):
        j = make_journal()
        self._record_resolved(j, WIN_FULL, direction=Direction.LONG)
        self._record_resolved(j, WIN_FULL, direction=Direction.LONG)
        self._record_resolved(j, LOSS, direction=Direction.SHORT)
        stats = j.get_stats(days=7)
        assert stats["long_count"] == 2
        assert stats["short_count"] == 1

    def test_excludes_old_signals(self):
        """Signals older than `days` window are excluded."""
        j = make_journal()
        self._record_resolved(j, WIN_FULL, hours_ago=200)  # ~8 days ago
        stats = j.get_stats(days=7)
        assert stats["total"] == 0

    def test_excludes_unresolved(self):
        """Pending (unresolved) signals not counted in stats."""
        j = make_journal()
        j.record(make_signal())  # no outcome set
        stats = j.get_stats(days=7)
        assert stats["total"] == 0
