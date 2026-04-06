"""
Outcome Checker — verifies pending signal results against real price action.

Runs every 30 minutes. For each signal whose check window has expired:
  1. Fetches 1h OHLCV candles from signal timestamp to now (Bybit).
  2. Iterates candles chronologically, checking SL first (worst case).
  3. Classifies: WIN_FULL / WIN_PARTIAL / LOSS / EXPIRED.
  4. Writes result back to the journal.

Classification rules (per candle, in order):
  LONG:  LOW  <= stop_loss → LOSS | HIGH >= tp2 → WIN_FULL | HIGH >= tp1 → TP1 hit
  SHORT: HIGH >= stop_loss → LOSS | LOW  <= tp2 → WIN_FULL | LOW  <= tp1 → TP1 hit
"""

from datetime import datetime, timezone

from logger import get_logger
from src.bybit_client import BybitClient
from src.journal.signal_journal import (
    SignalJournal, WIN_FULL, WIN_PARTIAL, LOSS, EXPIRED,
)

log = get_logger(__name__)


class OutcomeChecker:
    """
    Checks pending journal signals against real Bybit price data.
    Stateless — safe to call repeatedly from a scheduler.
    """

    def __init__(self, journal: SignalJournal, client: BybitClient):
        self._journal = journal
        self._client  = client

    def check_pending(self) -> int:
        """
        Resolve all signals whose check window has expired.
        Returns number of signals resolved.
        """
        pending = self._journal.get_pending_checks()
        if not pending:
            log.debug("Outcome checker: no pending signals")
            return 0

        resolved = 0
        for row in pending:
            try:
                outcome, exit_price = self._resolve(row)
                self._journal.update_outcome(row["id"], outcome, exit_price)
                resolved += 1
            except Exception as exc:
                log.warning("Outcome checker: failed to resolve signal #%d: %s", row["id"], exc)

        if resolved:
            log.info("Outcome checker: resolved %d signal(s)", resolved)
        return resolved

    # ─── Resolution logic ────────────────────────────────────────────────────

    def _resolve(self, row: dict) -> tuple[str, float]:
        """
        Fetch candles for the signal period and classify the outcome.
        Returns (outcome, exit_price).
        """
        signal_ts = datetime.fromisoformat(row["timestamp"])
        now_ts    = datetime.now(timezone.utc)

        start_ms = int(signal_ts.timestamp() * 1000)
        end_ms   = int(now_ts.timestamp() * 1000)

        candles = self._client.get_klines_range(start_ms=start_ms, end_ms=end_ms, interval="60")

        if not candles:
            log.debug("Outcome checker: no candles for signal #%d — marking EXPIRED", row["id"])
            return EXPIRED, row["entry_mid"]

        return self._classify(
            direction = row["direction"],
            entry_mid = row["entry_mid"],
            tp1       = row["tp1"],
            tp2       = row["tp2"],
            stop_loss = row["stop_loss"],
            candles   = candles,
        )

    @staticmethod
    def _classify(
        direction: str,
        entry_mid: float,
        tp1:       float,
        tp2:       float,
        stop_loss: float,
        candles:   list[dict],
    ) -> tuple[str, float]:
        """
        Iterate candles chronologically.
        Check SL first within each candle (conservative / realistic).
        """
        tp1_reached = False

        for candle in candles:
            high = candle["high"]
            low  = candle["low"]

            if direction == "LONG":
                if low <= stop_loss:
                    return LOSS, stop_loss
                if high >= tp2:
                    return WIN_FULL, tp2
                if high >= tp1:
                    tp1_reached = True

            else:  # SHORT
                if high >= stop_loss:
                    return LOSS, stop_loss
                if low <= tp2:
                    return WIN_FULL, tp2
                if low <= tp1:
                    tp1_reached = True

        if tp1_reached:
            return WIN_PARTIAL, tp1
        return EXPIRED, entry_mid
