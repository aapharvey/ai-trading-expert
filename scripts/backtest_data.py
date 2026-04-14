"""
Backtest Data Loader.

Downloads historical OHLCV (1h candles) from Bybit and Fear & Greed archive
from alternative.me. Saves locally to avoid repeated API calls.

Usage:
    python scripts/backtest_data.py

Output:
    data/ohlcv_1h.json      — 2 years of 1h OHLCV for BTCUSDT
    data/fear_greed.json    — up to 365 days of Fear & Greed values
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

# ─── Config ──────────────────────────────────────────────────────────────────

BYBIT_BASE_URL  = "https://api.bybit.com"
SYMBOL          = "BTCUSDT"
INTERVAL        = "60"          # 1h candles
CANDLES_PER_REQ = 200           # Bybit max per request
YEARS_BACK      = 2             # how far back to fetch
SLEEP_BETWEEN   = 0.3           # seconds between Bybit requests (rate limit)

FG_URL          = "https://api.alternative.me/fng/?limit=365"
FG_TIMEOUT      = 8

DATA_DIR        = os.path.join(os.path.dirname(__file__), "..", "data")
OHLCV_PATH      = os.path.join(DATA_DIR, "ohlcv_1h.json")
FG_PATH         = os.path.join(DATA_DIR, "fear_greed.json")


# ─── Bybit OHLCV ─────────────────────────────────────────────────────────────

def fetch_ohlcv(years: int = YEARS_BACK) -> list[dict]:
    """
    Download up to `years` years of 1h OHLCV from Bybit via paginated requests.
    Returns list of candle dicts sorted oldest → newest.
    """
    now_ms    = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms  = now_ms - years * 365 * 24 * 3600 * 1000
    end_ms    = now_ms

    all_candles: list[dict] = []
    cursor_end = end_ms

    print(f"Fetching OHLCV from Bybit ({years}y, 1h candles)...")

    while cursor_end > start_ms:
        params = {
            "category": "linear",
            "symbol":   SYMBOL,
            "interval": INTERVAL,
            "end":      cursor_end,
            "limit":    CANDLES_PER_REQ,
        }
        try:
            resp = requests.get(
                f"{BYBIT_BASE_URL}/v5/market/kline",
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("retCode") != 0:
                print(f"  Bybit error: {data.get('retMsg')} — stopping")
                break

            rows = data["result"]["list"]
            if not rows:
                break

            # Each row: [startTime, open, high, low, close, volume, turnover]
            candles = [
                {
                    "timestamp": int(row[0]),
                    "open":      float(row[1]),
                    "high":      float(row[2]),
                    "low":       float(row[3]),
                    "close":     float(row[4]),
                    "volume":    float(row[5]),
                }
                for row in rows
                if int(row[0]) >= start_ms
            ]

            all_candles.extend(candles)

            oldest_ts = min(int(r[0]) for r in rows)
            print(f"  Fetched {len(candles)} candles, oldest: "
                  f"{datetime.fromtimestamp(oldest_ts / 1000, tz=timezone.utc).strftime('%Y-%m-%d')}")

            if oldest_ts <= start_ms:
                break

            cursor_end = oldest_ts - 1
            time.sleep(SLEEP_BETWEEN)

        except Exception as exc:
            print(f"  Request failed: {exc} — stopping")
            break

    # Deduplicate and sort oldest → newest
    seen = set()
    unique = []
    for c in all_candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)

    unique.sort(key=lambda c: c["timestamp"])
    print(f"Total candles fetched: {len(unique)}")
    return unique


# ─── Fear & Greed ─────────────────────────────────────────────────────────────

def fetch_fear_greed() -> dict[str, int]:
    """
    Download up to 365 days of Fear & Greed Index from alternative.me.
    Returns {date_str: value} e.g. {"2024-04-15": 72}.
    """
    print("Fetching Fear & Greed archive...")
    try:
        resp = requests.get(FG_URL, timeout=FG_TIMEOUT)
        resp.raise_for_status()
        entries = resp.json()["data"]

        result = {}
        for entry in entries:
            ts    = int(entry["timestamp"])
            date  = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            value = int(entry["value"])
            result[date] = value

        print(f"Fear & Greed entries fetched: {len(result)}")
        return result

    except Exception as exc:
        print(f"Fear & Greed fetch failed: {exc}")
        return {}


# ─── Save helpers ─────────────────────────────────────────────────────────────

def save_json(path: str, data: object) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)
    size_kb = os.path.getsize(path) // 1024
    print(f"Saved → {path} ({size_kb} KB)")


def load_json(path: str) -> object:
    with open(path) as f:
        return json.load(f)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # OHLCV
    if os.path.exists(OHLCV_PATH):
        print(f"OHLCV cache exists: {OHLCV_PATH} — skipping download")
        print("  Delete the file to re-fetch.")
    else:
        candles = fetch_ohlcv()
        if candles:
            save_json(OHLCV_PATH, candles)

    # Fear & Greed
    if os.path.exists(FG_PATH):
        print(f"Fear & Greed cache exists: {FG_PATH} — skipping download")
        print("  Delete the file to re-fetch.")
    else:
        fg = fetch_fear_greed()
        if fg:
            save_json(FG_PATH, fg)

    print("\nDone. Run scripts/backtest.py to start backtesting.")
