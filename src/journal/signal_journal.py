"""
Signal Journal — SQLite-backed log of every emitted TradeSignal.

Records each signal immediately after Telegram delivery.
Tracks outcome (WIN_FULL / WIN_PARTIAL / LOSS / EXPIRED) after
the check window expires (24h for INTRADAY, 48h for SWING).
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from logger import get_logger
from src.models.signals import TradeSignal, Timeframe

log = get_logger(__name__)

# Outcome constants
WIN_FULL    = "WIN_FULL"     # TP2 reached before SL
WIN_PARTIAL = "WIN_PARTIAL"  # TP1 reached, TP2 not reached, SL not hit
LOSS        = "LOSS"         # SL reached before TP1
EXPIRED     = "EXPIRED"      # Neither TP nor SL reached within check window

_CHECK_HOURS = {
    Timeframe.INTRADAY: 24,
    Timeframe.SWING:    48,
}

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    direction           TEXT    NOT NULL,
    strength            INTEGER NOT NULL,
    timeframe           TEXT    NOT NULL,
    entry_mid           REAL    NOT NULL,
    tp1                 REAL    NOT NULL,
    tp2                 REAL    NOT NULL,
    stop_loss           REAL    NOT NULL,
    rr_ratio            REAL    NOT NULL,
    factors             TEXT    NOT NULL,
    check_after_hours   INTEGER NOT NULL,
    telegram_message_id INTEGER,
    outcome             TEXT,
    exit_price          REAL,
    checked_at          TEXT
)
"""

# Migration: add column to existing databases that predate this field
_MIGRATE_MESSAGE_ID = """
ALTER TABLE signals ADD COLUMN telegram_message_id INTEGER
"""


class SignalJournal:
    """
    Persists every emitted TradeSignal to SQLite.
    Keeps a single connection — required for :memory: (tests) and safe for
    single-threaded production use.
    """

    def __init__(self, db_path: str = "signals.db"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_CREATE_TABLE)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after initial schema (safe to run repeatedly)."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(signals)")
        }
        if "telegram_message_id" not in existing:
            self._conn.execute(_MIGRATE_MESSAGE_ID)

    def _connect(self) -> sqlite3.Connection:
        return self._conn

    # ─── Write ───────────────────────────────────────────────────────────────

    def record(self, signal: TradeSignal, telegram_message_id: Optional[int] = None) -> int:
        """
        Save a new signal. Returns the row id.
        Should be called immediately after successful Telegram send.
        Pass telegram_message_id to enable outcome reply notifications.
        """
        check_hours = _CHECK_HOURS.get(signal.timeframe, 24)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals
                  (timestamp, direction, strength, timeframe,
                   entry_mid, tp1, tp2, stop_loss, rr_ratio,
                   factors, check_after_hours, telegram_message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    signal.direction.value,
                    signal.strength,
                    signal.timeframe.value,
                    signal.entry_mid,
                    signal.tp1,
                    signal.tp2,
                    signal.stop_loss,
                    signal.rr_ratio,
                    json.dumps(signal.factors),
                    check_hours,
                    telegram_message_id,
                ),
            )
            signal_id = cur.lastrowid
            log.info(
                "Journal: recorded signal #%d %s [%d/5]",
                signal_id, signal.direction.value, signal.strength,
            )
            return signal_id

    def update_outcome(
        self,
        signal_id: int,
        outcome: str,
        exit_price: float,
    ) -> None:
        """Record the verified outcome for a pending signal."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE signals
                SET outcome = ?, exit_price = ?, checked_at = ?
                WHERE id = ?
                """,
                (outcome, exit_price, datetime.now(timezone.utc).isoformat(), signal_id),
            )
        log.info("Journal: signal #%d outcome = %s (exit=%.0f)", signal_id, outcome, exit_price)

    # ─── Read ────────────────────────────────────────────────────────────────

    def get_pending_checks(self) -> list[dict]:
        """
        Returns signals whose check window has expired but outcome is still NULL.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM signals
                WHERE outcome IS NULL
                  AND datetime(timestamp, '+' || check_after_hours || ' hours')
                      <= datetime('now')
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self, days: int = 7) -> dict:
        """
        Returns performance statistics for the last N days.
        Only includes signals with a resolved outcome.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM signals
                WHERE timestamp >= ?
                  AND outcome IS NOT NULL
                ORDER BY timestamp DESC
                """,
                (since,),
            ).fetchall()

        rows = [dict(r) for r in rows]
        total = len(rows)

        if total == 0:
            return {"total": 0, "days": days}

        win_full    = sum(1 for r in rows if r["outcome"] == WIN_FULL)
        win_partial = sum(1 for r in rows if r["outcome"] == WIN_PARTIAL)
        losses      = sum(1 for r in rows if r["outcome"] == LOSS)
        expired     = sum(1 for r in rows if r["outcome"] == EXPIRED)
        wins        = win_full + win_partial

        return {
            "days":         days,
            "total":        total,
            "win_rate":     wins / total,
            "win_full":     win_full,
            "win_partial":  win_partial,
            "losses":       losses,
            "expired":      expired,
            "long_count":   sum(1 for r in rows if r["direction"] == "LONG"),
            "short_count":  sum(1 for r in rows if r["direction"] == "SHORT"),
        }
