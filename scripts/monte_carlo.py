"""
Monte Carlo Risk Analysis.

Loads resolved signals from data/backtest_results.json,
bootstraps trade returns, and answers: "Is it safe to go live?"

Usage:
    python scripts/monte_carlo.py

Prerequisites:
    Run scripts/backtest.py first to generate backtest_results.json.

Output:
    Console report + data/monte_carlo_results.json
"""

import json
import os
import sys

import numpy as np
import pandas as pd

# ─── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR          = os.path.join(os.path.dirname(__file__), "..", "data")
BACKTEST_PATH     = os.path.join(DATA_DIR, "backtest_results.json")
MC_RESULTS_PATH   = os.path.join(DATA_DIR, "monte_carlo_results.json")

MIN_RESOLVED      = 30
RISK_PER_TRADE    = 0.01   # 1% of capital per trade (fixed-fractional)

# GO/NO-GO thresholds
THRESHOLD_MAX_DD  = -0.30  # worst 95% drawdown must be better than -30%
THRESHOLD_LOSS_63 =  0.40  # prob of loss over 63 trades must be below 40%

# ─── Data loading ─────────────────────────────────────────────────────────────

def load_returns(path: str = BACKTEST_PATH) -> pd.Series:
    """Load resolved signals from backtest_results.json → per-trade returns."""
    if not os.path.exists(path):
        print(f"ERROR: {path} not found.")
        print("Run: python scripts/backtest.py")
        sys.exit(1)

    with open(path) as f:
        data = json.load(f)

    resolved = [s for s in data["signals"] if s["outcome"] != "OPEN"]

    if len(resolved) < MIN_RESOLVED:
        print(f"ERROR: Недостатньо даних — лише {len(resolved)} resolved сигналів "
              f"(мінімум {MIN_RESOLVED}).")
        print("Запустіть backtest на більшому датасеті.")
        sys.exit(1)

    returns = pd.Series([s["achieved_rr"] * RISK_PER_TRADE for s in resolved])
    return returns


# ─── Monte Carlo Analyzer ─────────────────────────────────────────────────────

class MonteCarloAnalyzer:
    """Bootstrap Monte Carlo simulation for strategy risk analysis."""

    def __init__(self, n_simulations: int = 1000, confidence: float = 0.95):
        self.n_simulations = n_simulations
        self.confidence    = confidence

    def bootstrap_returns(self, returns: pd.Series, n_periods: int = None) -> np.ndarray:
        """Resample returns with replacement. Shape: (n_simulations, n_periods)."""
        if n_periods is None:
            n_periods = len(returns)

        simulations = np.zeros((self.n_simulations, n_periods))
        for i in range(self.n_simulations):
            simulations[i] = np.random.choice(returns.values, size=n_periods, replace=True)
        return simulations

    def analyze_drawdowns(self, returns: pd.Series) -> dict:
        """Simulate max drawdown distribution via bootstrap."""
        simulations  = self.bootstrap_returns(returns)
        max_drawdowns = []

        for sim in simulations:
            equity      = (1 + sim).cumprod()
            rolling_max = np.maximum.accumulate(equity)
            drawdowns   = (equity - rolling_max) / rolling_max
            max_drawdowns.append(drawdowns.min())

        max_drawdowns = np.array(max_drawdowns)
        return {
            "expected_max_dd": float(np.mean(max_drawdowns)),
            "median_max_dd":   float(np.median(max_drawdowns)),
            f"worst_{int(self.confidence * 100)}pct": float(
                np.percentile(max_drawdowns, (1 - self.confidence) * 100)
            ),
            "worst_case": float(max_drawdowns.min()),
        }

    def probability_of_loss(
        self, returns: pd.Series, holding_periods: list = None
    ) -> dict:
        """P(total_return < 0) for each holding period (in trades)."""
        if holding_periods is None:
            holding_periods = [21, 63, 126, 252]

        results = {}
        for period in holding_periods:
            simulations   = self.bootstrap_returns(returns, period)
            total_returns = (1 + simulations).prod(axis=1) - 1
            results[period] = float((total_returns < 0).mean())
        return results

    def confidence_interval(self, returns: pd.Series, periods: int = 252) -> dict:
        """Bootstrap CI for total return over `periods` trades."""
        simulations   = self.bootstrap_returns(returns, periods)
        total_returns = (1 + simulations).prod(axis=1) - 1

        lower = (1 - self.confidence) / 2
        upper = 1 - lower
        return {
            "expected":    float(total_returns.mean()),
            "lower_bound": float(np.percentile(total_returns, lower * 100)),
            "upper_bound": float(np.percentile(total_returns, upper * 100)),
            "std":         float(total_returns.std()),
        }


# ─── Analysis runner ──────────────────────────────────────────────────────────

def run_analysis(returns: pd.Series) -> dict:
    """Run all three Monte Carlo analyses and return combined results."""
    mc = MonteCarloAnalyzer(n_simulations=1000, confidence=0.95)

    print("Запуск симуляцій...")
    drawdowns = mc.analyze_drawdowns(returns)
    pol       = mc.probability_of_loss(returns, holding_periods=[21, 63, 126, 252])
    ci        = mc.confidence_interval(returns, periods=252)

    return {"drawdowns": drawdowns, "probability_of_loss": pol, "confidence_interval": ci}


# ─── Report ───────────────────────────────────────────────────────────────────

def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def print_report(returns: pd.Series, results: dict) -> None:
    dd  = results["drawdowns"]
    pol = results["probability_of_loss"]
    ci  = results["confidence_interval"]

    worst_95    = dd["worst_95pct"]
    prob_loss63 = pol.get(63, 1.0)

    go = worst_95 > THRESHOLD_MAX_DD and prob_loss63 < THRESHOLD_LOSS_63

    line  = "=" * 58
    thin  = "─" * 54

    # trades per week estimate for time labels
    # ~3 signals/week based on typical backtest output
    period_labels = {
        21:  "~7 тижнів",
        63:  "~5 місяців",
        126: "~10 місяців",
        252: "~20 місяців",
    }

    print(f"\n{line}")
    print(f" MONTE CARLO RISK ANALYSIS  (1000 симуляцій, confidence=95%)")
    print(line)
    print(f" Вхідні дані : {len(returns)} resolved угод | {int(RISK_PER_TRADE*100)}% ризику / угода")
    print(f" {thin}")

    print(f" {'── Максимальне просідання (Drawdown Analysis) ──':}")
    print(f"   Очікуване max DD     : {_pct(dd['expected_max_dd']):>8}")
    print(f"   Медіанне max DD      : {_pct(dd['median_max_dd']):>8}")
    print(f"   Worst 95%            : {_pct(worst_95):>8}   ← критерій GO: < 30%")
    print(f"   Worst-case (100%)    : {_pct(dd['worst_case']):>8}")
    print(f" {thin}")

    print(f" {'── Ймовірність збитку на горизонті ──':}")
    for period in [21, 63, 126, 252]:
        prob  = pol.get(period)
        label = period_labels.get(period, "")
        marker = "   ← критерій GO: < 40%" if period == 63 else ""
        print(f"   {period:>3} угод  ({label:<12}) : {prob*100:>5.1f}%{marker}")
    print(f" {thin}")

    print(f" {'── Довірчий інтервал (252 угоди вперед) ──':}")
    print(f"   Очікуваний дохід     : {_pct(ci['expected']):>8}")
    print(f"   95% CI               :  [{_pct(ci['lower_bound'])} .. {_pct(ci['upper_bound'])}]")
    print(f"   Стд відхилення       : {_pct(ci['std']):>8}")
    print(f" {thin}")

    if go:
        print(f" ВИСНОВОК: GO — стратегія готова до live-торгівлі")
        print(f"   Worst 95% DD   : {_pct(worst_95)} < 30% ✓")
        print(f"   P(loss/63 угод): {prob_loss63*100:.1f}% < 40% ✓")
    else:
        print(f" ВИСНОВОК: NO-GO — стратегія НЕ готова до live-торгівлі")
        if worst_95 <= THRESHOLD_MAX_DD:
            print(f"   Worst 95% DD   : {_pct(worst_95)} >= 30% ✗  (перевищує допустимий рівень)")
        else:
            print(f"   Worst 95% DD   : {_pct(worst_95)} < 30% ✓")
        if prob_loss63 >= THRESHOLD_LOSS_63:
            print(f"   P(loss/63 угод): {prob_loss63*100:.1f}% >= 40% ✗  (стратегія нестабільна)")
        else:
            print(f"   P(loss/63 угод): {prob_loss63*100:.1f}% < 40% ✓")

    print(f"{line}\n")


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    returns = load_returns()
    results = run_analysis(returns)
    print_report(returns, results)

    payload = {
        "n_simulations":  1000,
        "confidence":     0.95,
        "n_trades":       len(returns),
        "risk_per_trade": RISK_PER_TRADE,
        **results,
        "verdict": (
            "GO"
            if results["drawdowns"]["worst_95pct"] > THRESHOLD_MAX_DD
            and results["probability_of_loss"].get(63, 1.0) < THRESHOLD_LOSS_63
            else "NO-GO"
        ),
    }
    # JSON doesn't support int keys — convert pol periods to strings
    payload["probability_of_loss"] = {
        str(k): v for k, v in results["probability_of_loss"].items()
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MC_RESULTS_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Results saved → {MC_RESULTS_PATH}")
