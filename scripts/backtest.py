"""
Backtest Engine.

Replays historical OHLCV data through the existing analyzer stack
(Blocks 1–4) and measures signal quality: win rate, R:R, drawdown.

Blocks 7–8 (Liquidity, VolumeProfile) are skipped — require live order book.

Usage:
    python scripts/backtest.py

Prerequisites:
    Run scripts/backtest_data.py first to download historical data.

Output:
    Console summary + data/backtest_results.json
"""

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from src.analyzers.price_action import PriceActionAnalyzer
from src.analyzers.technical import TechnicalAnalyzer
from src.analyzers.order_flow import OrderFlowAnalyzer
from src.models.signals import Direction
from src.engine.confluence import ConfluenceEngine

# ─── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR     = os.path.join(os.path.dirname(__file__), "..", "data")
OHLCV_PATH   = os.path.join(DATA_DIR, "ohlcv_1h.json")
FG_PATH      = os.path.join(DATA_DIR, "fear_greed.json")
RESULTS_PATH = os.path.join(DATA_DIR, "backtest_results.json")

# ─── Minimum candle window sizes ─────────────────────────────────────────────

MIN_PA_CANDLES   = 200   # price action needs 4h candles → we simulate from 1h
MIN_TECH_CANDLES = 200   # EMA 200 needs 200 candles
MIN_OF_CANDLES   = 20    # order flow needs at least 20

# How many 1h candles to look ahead when checking TP/SL hit
MAX_LOOKAHEAD = 120      # 5 days


# ─── Data structures ─────────────────────────────────────────────────────────

@dataclass
class BacktestSignal:
    timestamp:   int
    direction:   str
    strength:    int
    entry_mid:   float
    tp1:         float
    tp2:         float
    stop_loss:   float
    rr_ratio:    float
    factors:     list[str]
    outcome:     str = "OPEN"       # WIN_TP1 / WIN_TP2 / LOSS / OPEN
    achieved_rr: float = 0.0


@dataclass
class BacktestStats:
    total_signals:   int = 0
    win_tp1:         int = 0
    win_tp2:         int = 0
    loss:            int = 0
    open_:           int = 0
    avg_rr:          float = 0.0
    max_drawdown:    int = 0        # max consecutive losses
    signals_per_week: float = 0.0


# ─── Helper: simulate 4h candles from 1h ─────────────────────────────────────

def to_4h(candles_1h: list[dict]) -> list[dict]:
    """Aggregate 1h candles into 4h candles (groups of 4)."""
    result = []
    for i in range(0, len(candles_1h) - 3, 4):
        group = candles_1h[i: i + 4]
        result.append({
            "timestamp": group[0]["timestamp"],
            "open":      group[0]["open"],
            "high":      max(c["high"]   for c in group),
            "low":       min(c["low"]    for c in group),
            "close":     group[-1]["close"],
            "volume":    sum(c["volume"] for c in group),
        })
    return result


# ─── Helper: fake OI history from price ──────────────────────────────────────

def make_fake_oi(candles: list[dict]) -> list[dict]:
    """
    Approximate OI from volume (no historical OI available via free API).
    Uses rolling volume as a proxy — directionally similar to real OI.

    WARNING: volume ≠ open interest. Bybit does not provide historical OI
    via any free public endpoint. OI-based signals (OI_LONG_BUILDUP,
    OI_SHORT_BUILDUP, OI_LONG_UNWIND, OI_SHORT_UNWIND) will behave
    differently in backtest vs live trading. Treat OI signal results
    in backtests as indicative only.
    """
    return [
        {"timestamp": c["timestamp"], "open_interest": c["volume"]}
        for c in candles[-10:]
    ]


# ─── Helper: fake funding from price action ───────────────────────────────────

def make_fake_funding(candles: list[dict]) -> list[dict]:
    """
    No historical funding rate available via free Bybit endpoint.
    Return neutral funding (0.0) — funding signals won't fire in backtest.
    """
    return [{"timestamp": c["timestamp"], "funding_rate": 0.0} for c in candles[-5:]]


# ─── Helper: Fear & Greed lookup ─────────────────────────────────────────────

def get_fg_signals(ts_ms: int, fg_data: dict[str, int]) -> list[str]:
    """Return Fear & Greed signals for the given timestamp."""
    date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    value = fg_data.get(date)
    if value is None:
        return []
    if value <= config.FEAR_GREED_EXTREME_FEAR:
        return ["EXTREME_FEAR"]
    if value <= config.FEAR_GREED_FEAR:
        return ["FEAR"]
    if value >= config.FEAR_GREED_EXTREME_GREED:
        return ["EXTREME_GREED"]
    if value >= config.FEAR_GREED_GREED:
        return ["GREED"]
    return []


# ─── Outcome resolution ───────────────────────────────────────────────────────

def resolve_outcome(signal: BacktestSignal, future_candles: list[dict]) -> BacktestSignal:
    """
    Walk future candles and determine what was hit first: TP1, TP2 or SL.
    Updates signal.outcome and signal.achieved_rr in place.
    """
    for candle in future_candles[:MAX_LOOKAHEAD]:
        high = candle["high"]
        low  = candle["low"]

        if signal.direction == Direction.LONG:
            if low <= signal.stop_loss:
                signal.outcome     = "LOSS"
                risk               = abs(signal.entry_mid - signal.stop_loss)
                signal.achieved_rr = -1.0 if risk > 0 else 0.0
                return signal
            if high >= signal.tp2:
                signal.outcome     = "WIN_TP2"
                risk               = abs(signal.entry_mid - signal.stop_loss)
                reward             = abs(signal.tp2 - signal.entry_mid)
                signal.achieved_rr = round(reward / risk, 2) if risk > 0 else 0.0
                return signal
            if high >= signal.tp1:
                signal.outcome     = "WIN_TP1"
                risk               = abs(signal.entry_mid - signal.stop_loss)
                reward             = abs(signal.tp1 - signal.entry_mid)
                signal.achieved_rr = round(reward / risk, 2) if risk > 0 else 0.0
                return signal

        else:  # SHORT
            if high >= signal.stop_loss:
                signal.outcome     = "LOSS"
                risk               = abs(signal.entry_mid - signal.stop_loss)
                signal.achieved_rr = -1.0 if risk > 0 else 0.0
                return signal
            if low <= signal.tp2:
                signal.outcome     = "WIN_TP2"
                risk               = abs(signal.entry_mid - signal.stop_loss)
                reward             = abs(signal.entry_mid - signal.tp2)
                signal.achieved_rr = round(reward / risk, 2) if risk > 0 else 0.0
                return signal
            if low <= signal.tp1:
                signal.outcome     = "WIN_TP1"
                risk               = abs(signal.entry_mid - signal.stop_loss)
                reward             = abs(signal.entry_mid - signal.tp1)
                signal.achieved_rr = round(reward / risk, 2) if risk > 0 else 0.0
                return signal

    signal.outcome = "OPEN"
    return signal


# ─── Statistics ───────────────────────────────────────────────────────────────

def compute_stats(signals: list[BacktestSignal], total_candles: int) -> BacktestStats:
    """Compute summary statistics from resolved signals."""
    stats = BacktestStats()
    stats.total_signals = len(signals)

    if not signals:
        return stats

    resolved = [s for s in signals if s.outcome != "OPEN"]
    stats.win_tp1 = sum(1 for s in signals if s.outcome == "WIN_TP1")
    stats.win_tp2 = sum(1 for s in signals if s.outcome == "WIN_TP2")
    stats.loss    = sum(1 for s in signals if s.outcome == "LOSS")
    stats.open_   = sum(1 for s in signals if s.outcome == "OPEN")

    if resolved:
        rr_values    = [s.achieved_rr for s in resolved]
        stats.avg_rr = round(sum(rr_values) / len(rr_values), 2)

    # Max consecutive losses
    max_streak = streak = 0
    for s in signals:
        if s.outcome == "LOSS":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    stats.max_drawdown = max_streak

    # Signals per week (total_candles = 1h candles)
    total_weeks = total_candles / (24 * 7)
    stats.signals_per_week = round(stats.total_signals / total_weeks, 2) if total_weeks > 0 else 0.0

    return stats


# ─── Print report ─────────────────────────────────────────────────────────────

def print_report(stats: BacktestStats) -> None:
    resolved = stats.win_tp1 + stats.win_tp2 + stats.loss
    win_rate_tp1 = round(stats.win_tp1 / resolved * 100, 1) if resolved else 0
    win_rate_tp2 = round((stats.win_tp1 + stats.win_tp2) / resolved * 100, 1) if resolved else 0

    line = "=" * 52
    print(f"\n{line}")
    print(" BACKTEST RESULTS")
    print(line)
    print(f" Signals generated : {stats.total_signals}")
    print(f" Resolved          : {resolved}  (OPEN: {stats.open_})")
    print(f" Win rate (TP1)    : {win_rate_tp1}%")
    print(f" Win rate (TP2)    : {win_rate_tp2}%")
    print(f" Avg R:R achieved  : {stats.avg_rr}")
    print(f" Max consec. losses: {stats.max_drawdown}")
    print(f" Signals / week    : {stats.signals_per_week}")
    print(line)

    if stats.signals_per_week < 1.0:
        print(" WARNING: < 1 signal/week — strategy too conservative.")
        print("          Consider lowering MIN_SIGNAL_STRENGTH or _NORM_SCALE.")
    if resolved > 0 and win_rate_tp1 < 45:
        print(" WARNING: Win rate < 45% — review signal logic and weights.")
    if stats.avg_rr < 1.5:
        print(" WARNING: Avg R:R < 1.5 — review SL/TP multipliers.")
    print(" WARNING: OI signals use volume proxy — live results will differ.")

    print()


# ─── Main backtest loop ───────────────────────────────────────────────────────

def run_backtest() -> list[BacktestSignal]:
    # Load data
    if not os.path.exists(OHLCV_PATH):
        print(f"ERROR: {OHLCV_PATH} not found.")
        print("Run: python scripts/backtest_data.py")
        sys.exit(1)

    with open(OHLCV_PATH) as f:
        candles_1h: list[dict] = json.load(f)

    fg_data: dict[str, int] = {}
    if os.path.exists(FG_PATH):
        with open(FG_PATH) as f:
            fg_data = json.load(f)
    else:
        print("Fear & Greed data not found — sentiment signals disabled.")

    print(f"Loaded {len(candles_1h)} candles | F&G entries: {len(fg_data)}")

    candles_4h = to_4h(candles_1h)

    pa_analyzer   = PriceActionAnalyzer()
    tech_analyzer = TechnicalAnalyzer()
    of_analyzer   = OrderFlowAnalyzer()
    engine        = ConfluenceEngine()

    all_signals: list[BacktestSignal] = []
    step = len(candles_1h) // 20   # progress every 5%

    print("Running backtest...")

    for i in range(MIN_TECH_CANDLES, len(candles_1h)):
        if step and i % step == 0:
            pct = int(i / len(candles_1h) * 100)
            ts  = datetime.fromtimestamp(
                candles_1h[i]["timestamp"] / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d")
            print(f"  {pct}% — {ts}")

        window_1h = candles_1h[max(0, i - MIN_TECH_CANDLES): i + 1]
        window_4h = candles_4h[max(0, (i // 4) - MIN_PA_CANDLES): (i // 4) + 1]
        current_price = candles_1h[i]["close"]

        if len(window_1h) < MIN_TECH_CANDLES or len(window_4h) < 20:
            continue

        # Run analyzers
        try:
            pa_result   = pa_analyzer.analyze(window_4h)
            tech_result = tech_analyzer.analyze(window_1h)
            of_result   = of_analyzer.analyze(
                oi_history=make_fake_oi(window_1h),
                candles=window_1h,
                funding_history=make_fake_funding(window_1h),
                current_price=current_price,
            )
        except Exception:
            continue

        # Inject Fear & Greed signals
        fg_signals = get_fg_signals(candles_1h[i]["timestamp"], fg_data)
        if fg_signals:
            of_result.signals = of_result.signals + fg_signals

        # Evaluate confluence (pass candle time so anti-spam uses historical time, not now)
        candle_time = datetime.fromtimestamp(
            candles_1h[i]["timestamp"] / 1000, tz=timezone.utc
        )
        signal = engine.evaluate(pa_result, tech_result, of_result, now=candle_time, norm_scale=2.5)
        if signal is None:
            continue

        bt_signal = BacktestSignal(
            timestamp  = candles_1h[i]["timestamp"],
            direction  = signal.direction,
            strength   = signal.strength,
            entry_mid  = (signal.entry_low + signal.entry_high) / 2,
            tp1        = signal.tp1,
            tp2        = signal.tp2,
            stop_loss  = signal.stop_loss,
            rr_ratio   = signal.rr_ratio,
            factors    = signal.factors,
        )

        # Resolve outcome from future candles
        future = candles_1h[i + 1:]
        resolve_outcome(bt_signal, future)
        all_signals.append(bt_signal)

    return all_signals


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    signals = run_backtest()
    stats   = compute_stats(signals, len(signals))
    print_report(stats)

    # Save results
    os.makedirs(DATA_DIR, exist_ok=True)
    results = {
        "stats": {
            "total_signals":    stats.total_signals,
            "win_tp1":          stats.win_tp1,
            "win_tp2":          stats.win_tp2,
            "loss":             stats.loss,
            "open":             stats.open_,
            "avg_rr":           stats.avg_rr,
            "max_drawdown":     stats.max_drawdown,
            "signals_per_week": stats.signals_per_week,
        },
        "signals": [
            {
                "timestamp":   s.timestamp,
                "date":        datetime.fromtimestamp(
                                   s.timestamp / 1000, tz=timezone.utc
                               ).strftime("%Y-%m-%d %H:%M"),
                "direction":   str(s.direction),
                "strength":    s.strength,
                "entry_mid":   s.entry_mid,
                "tp1":         s.tp1,
                "tp2":         s.tp2,
                "stop_loss":   s.stop_loss,
                "rr_ratio":    s.rr_ratio,
                "outcome":     s.outcome,
                "achieved_rr": s.achieved_rr,
                "factors":     s.factors,
            }
            for s in signals
        ],
    }
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Results saved → {RESULTS_PATH}")
