"""
One-time script: analyze Bybit BTC funding rate history.
Fetches ~6 months of 8h funding data and computes the 95th percentile
to calibrate FUNDING_EXTREME_HIGH / FUNDING_EXTREME_LOW thresholds.

Usage:
    python scripts/analyze_funding.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import time

BYBIT_URL   = "https://api.bybit.com/v5/market/funding/history"
SYMBOL      = "BTCUSDT"
LIMIT       = 200          # max per request
TARGET_DAYS = 180          # 6 months
INTERVAL_H  = 8            # funding every 8h
TARGET_N    = TARGET_DAYS * (24 // INTERVAL_H)   # ~540 data points


def fetch_all_funding() -> list[float]:
    rates = []
    cursor = None

    print(f"Fetching ~{TARGET_N} funding rate records ({TARGET_DAYS} days)...")

    while len(rates) < TARGET_N:
        params = {
            "category": "linear",
            "symbol":   SYMBOL,
            "limit":    LIMIT,
        }
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(BYBIT_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("retCode") != 0:
            print(f"Bybit error: {data.get('retMsg')}")
            break

        rows = data["result"]["list"]
        if not rows:
            break

        for row in rows:
            rates.append(float(row["fundingRate"]))

        cursor = data["result"].get("nextPageCursor")
        if not cursor:
            break

        time.sleep(0.2)
        print(f"  Fetched {len(rates)} records...", end="\r")

    print(f"\nTotal records fetched: {len(rates)}")
    return rates


def analyze(rates: list[float]) -> None:
    if not rates:
        print("No data to analyze.")
        return

    abs_rates = [abs(r) for r in rates]
    abs_rates_sorted = sorted(abs_rates)

    n = len(abs_rates_sorted)
    p50  = abs_rates_sorted[int(n * 0.50)]
    p90  = abs_rates_sorted[int(n * 0.90)]
    p95  = abs_rates_sorted[int(n * 0.95)]
    p99  = abs_rates_sorted[int(n * 0.99)]
    pmax = abs_rates_sorted[-1]

    pos_rates = [r for r in rates if r > 0]
    neg_rates = [r for r in rates if r < 0]

    print("\n=== Bybit BTC Funding Rate Analysis ===================================")
    print(f"  Records analyzed : {n}")
    print(f"  Positive funding : {len(pos_rates)} ({100*len(pos_rates)/n:.1f}%)")
    print(f"  Negative funding : {len(neg_rates)} ({100*len(neg_rates)/n:.1f}%)")
    print(f"\n  |Funding| percentiles:")
    print(f"    50th : {p50*100:.4f}%")
    print(f"    90th : {p90*100:.4f}%")
    print(f"    95th : {p95*100:.4f}%  <-- recommended threshold")
    print(f"    99th : {p99*100:.4f}%")
    print(f"    max  : {pmax*100:.4f}%")
    print("=======================================================================")

    # Bybit client multiplies raw rate by 100 before storing (converts to %)
    # Config thresholds are in % units — so multiply p95 by 100
    p95_pct = p95 * 100
    recommended = round(p95_pct * 1000) / 1000   # round to 3 decimal places

    print(f"\nNote: bybit_client.py stores funding_rate as % (raw * 100)")
    print(f"  95th percentile raw  : {p95:.6f}  ->  as %: {p95_pct:.4f}%")
    print(f"\nRECOMMENDED config.py values:")
    print(f"  FUNDING_EXTREME_HIGH =  {recommended}   # 95th percentile of |funding| (in %)")
    print(f"  FUNDING_EXTREME_LOW  = -{recommended}")
    print(f"\n  (current values: FUNDING_EXTREME_HIGH=0.05, FUNDING_EXTREME_LOW=-0.05)")


if __name__ == "__main__":
    rates = fetch_all_funding()
    analyze(rates)
