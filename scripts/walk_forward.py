"""
Walk-Forward Optimization of ConfluenceEngine parameters.

Splits historical data into rolling train/test windows.
Grid-searches norm_scale, min_strength, tp2_multiplier on each training window.
Evaluates best params on the following test window.

Usage:
    python scripts/walk_forward.py

Prerequisites:
    Run scripts/backtest_data.py first.

Output:
    Console table + data/walk_forward_results.json
"""

import json
import os
import sys
from collections import Counter
from itertools import product

# Add scripts dir so `backtest` is importable directly
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd

from backtest import (
    DATA_DIR,
    FG_PATH,
    OHLCV_PATH,
    BacktestSignal,
    calculate_metrics,
    run_backtest_window,
)

# ─── Config ───────────────────────────────────────────────────────────────────

TRAIN_CANDLES    = 4_380   # ~6 months of 1h candles (182.5 days)
TEST_CANDLES     = 1_460   # ~2 months of 1h candles  (60.8 days)
MIN_SIG_PER_WEEK = 0.5
WFO_RESULTS_PATH = os.path.join(DATA_DIR, "walk_forward_results.json")

PARAM_GRID = {
    "norm_scale":     [1.5, 2.0, 2.5, 3.0],
    "min_strength":   [3, 4, 5],
    "tp2_multiplier": [2.0, 2.5, 3.0, 3.5],
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sharpe_for_signals(signals: list, n_candles: int) -> float:
    """Sharpe ratio for a signal list. Returns -inf if signals/week < threshold."""
    resolved = [s for s in signals if s.outcome != "OPEN"]
    weeks    = n_candles / (24 * 7)
    if not resolved or len(signals) / weeks < MIN_SIG_PER_WEEK:
        return float("-inf")

    risk_pct   = 0.01
    returns    = pd.Series([s.achieved_rr * risk_pct for s in resolved])
    span_days  = (resolved[-1].timestamp - resolved[0].timestamp) / (1000 * 86_400)
    ann_factor = (len(resolved) / span_days * 365) if span_days > 0 else 52.0

    m = calculate_metrics(returns, rf_rate=0.0, ann_factor=ann_factor)
    return m["sharpe_ratio"]


def _signals_per_week(signals: list, n_candles: int) -> float:
    weeks = n_candles / (24 * 7)
    return round(len(signals) / weeks, 2) if weeks > 0 else 0.0


# ─── Walk-Forward ─────────────────────────────────────────────────────────────

def run_walk_forward(candles_1h: list[dict], fg_data: dict) -> list[dict]:
    """Sliding WFO: train 6 months → grid search, test 2 months → evaluate."""
    param_names  = list(PARAM_GRID.keys())
    param_combos = list(product(*PARAM_GRID.values()))
    n            = len(candles_1h)
    results      = []
    split_num    = 0
    cursor       = TRAIN_CANDLES

    while cursor + TEST_CANDLES <= n:
        split_num  += 1
        train_slice = candles_1h[cursor - TRAIN_CANDLES : cursor]
        test_slice  = candles_1h[cursor : cursor + TEST_CANDLES]

        print(
            f"\n── Split {split_num}"
            f"  ({len(train_slice)}h train / {len(test_slice)}h test,"
            f"  {len(param_combos)} combos) ──"
        )

        # Grid search on train window
        best_params = None
        best_sharpe = float("-inf")

        for idx, values in enumerate(param_combos):
            params     = dict(zip(param_names, values))
            train_sigs = run_backtest_window(train_slice, fg_data, params)
            sharpe     = _sharpe_for_signals(train_sigs, len(train_slice))

            if sharpe > best_sharpe:
                best_sharpe = sharpe
                best_params = params

            sys.stdout.write(
                f"\r  [{idx+1:>2}/{len(param_combos)}]"
                f"  best Sharpe={best_sharpe:>7.3f}"
                f"  @ ns={best_params['norm_scale'] if best_params else '?'}"
                f"  ms={best_params['min_strength'] if best_params else '?'}"
                f"  tp={best_params['tp2_multiplier'] if best_params else '?'}"
            )
            sys.stdout.flush()

        print()  # newline after grid search line

        if best_params is None:
            print("  No viable params found — skipping split.")
            cursor += TEST_CANDLES
            continue

        # Evaluate best params on test window
        test_sigs   = run_backtest_window(test_slice, fg_data, best_params)
        test_sharpe = _sharpe_for_signals(test_sigs, len(test_slice))
        test_spw    = _signals_per_week(test_sigs, len(test_slice))
        viable      = test_sharpe != float("-inf")

        results.append({
            "split":                 split_num,
            "best_params":           best_params,
            "train_sharpe":          round(best_sharpe, 3),
            "test_sharpe":           round(test_sharpe, 3) if viable else None,
            "test_signals_per_week": test_spw,
            "viable":                viable,
        })

        cursor += TEST_CANDLES

    return results


# ─── Print table ──────────────────────────────────────────────────────────────

def print_table(results: list[dict]) -> None:
    line = "=" * 84
    print(f"\n{line}")
    print(" WALK-FORWARD RESULTS")
    print(line)
    print(
        f" {'Split':>5}  {'norm_scale':>10}  {'min_str':>7}  "
        f"{'tp2_mult':>8}  {'Train Sh':>8}  {'Test Sh':>8}  {'Sig/wk':>6}"
    )
    print(f" {'─' * 79}")

    for r in results:
        p       = r["best_params"]
        test_sh = f"{r['test_sharpe']:>8.3f}" if r["test_sharpe"] is not None else "     N/A"
        flag    = "" if r["viable"] else "  ✗ low signals"
        print(
            f" {r['split']:>5}  {p['norm_scale']:>10}  {p['min_strength']:>7}  "
            f"{p['tp2_multiplier']:>8}  {r['train_sharpe']:>8.3f}  "
            f"{test_sh}  {r['test_signals_per_week']:>6}{flag}"
        )

    print(line)

    viable = [r for r in results if r["viable"]]
    if viable:
        all_params  = [tuple(sorted(r["best_params"].items())) for r in viable]
        top_params  = dict(Counter(all_params).most_common(1)[0][0])
        avg_test_sh = sum(r["test_sharpe"] for r in viable) / len(viable)
        print(f"\n Viable splits     : {len(viable)} / {len(results)}")
        print(f" Avg test Sharpe   : {avg_test_sh:.3f}")
        print(f" Recommended params (most frequent across viable splits):")
        for k, v in top_params.items():
            print(f"   {k} = {v}")
    else:
        print("\n No viable splits — review strategy or extend dataset.")

    print()


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
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

    n_combos         = len(list(product(*PARAM_GRID.values())))
    n_splits_approx  = max(0, (len(candles_1h) - TRAIN_CANDLES) // TEST_CANDLES)
    print(f"Loaded {len(candles_1h)} candles | F&G entries: {len(fg_data)}")
    print(f"Train: {TRAIN_CANDLES}h (~6 months)  Test: {TEST_CANDLES}h (~2 months)")
    print(f"Estimated splits: {n_splits_approx}  |  Grid: {n_combos} combos per split")
    print("NOTE: This may take 30–90 minutes depending on dataset size.")

    wf_results = run_walk_forward(candles_1h, fg_data)
    print_table(wf_results)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(WFO_RESULTS_PATH, "w") as f:
        json.dump({"results": wf_results}, f, indent=2)
    print(f"Results saved → {WFO_RESULTS_PATH}")
