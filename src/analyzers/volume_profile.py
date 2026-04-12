"""
Block 8: Volume Profile Analyzer.

Builds a volume profile from OHLCV candle data and identifies key levels:
  POC  — Point of Control: price level with highest traded volume
  VAH  — Value Area High:  upper bound of the 70% value area
  VAL  — Value Area Low:   lower bound of the 70% value area
  Naked POC — POC from the previous session not yet revisited by price

Signals emitted:
  PRICE_AT_POC_FROM_BELOW — price returning to POC from below (LONG)
  PRICE_AT_POC_FROM_ABOVE — price returning to POC from above (SHORT)
  ABOVE_VALUE_AREA        — price broke above VAH (LONG momentum)
  BELOW_VALUE_AREA        — price broke below VAL (SHORT momentum)
  NAKED_POC_ABOVE         — unvisited prior-session POC above price (LONG magnet)
  NAKED_POC_BELOW         — unvisited prior-session POC below price (SHORT magnet)
"""

from dataclasses import dataclass, field
from typing import Optional

from logger import get_logger

log = get_logger(__name__)

# Price bucket size in USD for volume distribution
_BUCKET_SIZE = 50.0
# Fraction of total volume defining the Value Area
_VALUE_AREA_PCT = 0.70
# Proximity threshold: how close price must be to POC to trigger AT_POC signal (%)
_AT_POC_THRESHOLD_PCT = 0.005   # 0.5%


@dataclass
class VolumeProfileResult:
    poc:        Optional[float]   = None   # current session POC
    vah:        Optional[float]   = None   # value area high
    val:        Optional[float]   = None   # value area low
    naked_poc:  Optional[float]   = None   # prior session POC not yet revisited
    signals:    list[str]         = field(default_factory=list)


class VolumeProfileAnalyzer:
    """
    Builds a volume profile from 1h OHLCV candles.

    Volume distribution method:
      For each candle, volume is distributed uniformly across $50 price buckets
      spanning [candle_low, candle_high]. This is a standard approximation
      used when tick-by-tick data is unavailable.

    Session definition:
      Current session  = last 24 candles (24h)
      Previous session = candles 48–25 (prior 24h) — used for Naked POC
    """

    def analyze(
        self,
        candles_48h: list[dict],
        current_price: float,
    ) -> VolumeProfileResult:
        """
        Main entry point.

        candles_48h:   list of 1h OHLCV dicts covering ~48h (oldest first)
                       keys: open, high, low, close, volume
        current_price: latest traded price
        """
        if len(candles_48h) < 2:
            log.warning("VolumeProfile: insufficient candles (%d)", len(candles_48h))
            return VolumeProfileResult()

        # Split into current and prior session
        current_candles = candles_48h[-24:] if len(candles_48h) >= 24 else candles_48h
        prior_candles   = candles_48h[-48:-24] if len(candles_48h) >= 48 else []

        # Build profiles
        current_profile = self._build_profile(current_candles)
        prior_profile   = self._build_profile(prior_candles) if prior_candles else {}

        if not current_profile:
            return VolumeProfileResult()

        poc = self._find_poc(current_profile)
        vah, val = self._find_value_area(current_profile, poc)
        naked_poc = self._find_naked_poc(prior_profile, current_candles, current_price)

        signals = self._generate_signals(current_price, poc, vah, val, naked_poc, candles_48h)

        log.debug(
            "VolumeProfile: POC=$%.0f VAH=$%.0f VAL=$%.0f NakedPOC=%s signals=%s",
            poc or 0, vah or 0, val or 0,
            f"${naked_poc:.0f}" if naked_poc else "none",
            signals,
        )

        return VolumeProfileResult(
            poc=poc,
            vah=vah,
            val=val,
            naked_poc=naked_poc,
            signals=signals,
        )

    # ─── Profile building ─────────────────────────────────────────────────────

    def _build_profile(self, candles: list[dict]) -> dict[float, float]:
        """
        Build a {bucket_price: volume} dict from OHLCV candles.
        Volume is distributed uniformly across $BUCKET_SIZE buckets within each candle.
        """
        profile: dict[float, float] = {}
        for candle in candles:
            low    = candle["low"]
            high   = candle["high"]
            volume = candle["volume"]

            if high <= low or volume <= 0:
                continue

            buckets = self._price_buckets(low, high)
            if not buckets:
                continue

            vol_per_bucket = volume / len(buckets)
            for bucket in buckets:
                profile[bucket] = profile.get(bucket, 0.0) + vol_per_bucket

        return profile

    @staticmethod
    def _price_buckets(low: float, high: float) -> list[float]:
        """
        Return list of bucket prices (lower edge, rounded to BUCKET_SIZE)
        covering the range [low, high].
        """
        start = (low // _BUCKET_SIZE) * _BUCKET_SIZE
        buckets = []
        price = start
        while price <= high:
            buckets.append(price)
            price += _BUCKET_SIZE
        return buckets

    # ─── POC ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_poc(profile: dict[float, float]) -> Optional[float]:
        """Return the bucket price with the highest volume."""
        if not profile:
            return None
        return max(profile, key=lambda p: profile[p])

    # ─── Value Area ───────────────────────────────────────────────────────────

    def _find_value_area(
        self,
        profile: dict[float, float],
        poc:     Optional[float],
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Find VAH and VAL: the price range containing VALUE_AREA_PCT of total volume,
        built outward from the POC (highest-volume bucket first).
        """
        if not profile or poc is None:
            return None, None

        total_vol   = sum(profile.values())
        target_vol  = total_vol * _VALUE_AREA_PCT

        sorted_buckets = sorted(profile.keys())
        poc_idx        = sorted_buckets.index(poc) if poc in sorted_buckets else len(sorted_buckets) // 2

        included = [poc]
        accumulated = profile[poc]

        lo_idx = poc_idx - 1
        hi_idx = poc_idx + 1

        while accumulated < target_vol:
            lo_vol = profile.get(sorted_buckets[lo_idx], 0) if lo_idx >= 0 else 0
            hi_vol = profile.get(sorted_buckets[hi_idx], 0) if hi_idx < len(sorted_buckets) else 0

            if lo_vol == 0 and hi_vol == 0:
                break

            if hi_vol >= lo_vol and hi_idx < len(sorted_buckets):
                included.append(sorted_buckets[hi_idx])
                accumulated += hi_vol
                hi_idx += 1
            elif lo_idx >= 0:
                included.append(sorted_buckets[lo_idx])
                accumulated += lo_vol
                lo_idx -= 1
            else:
                included.append(sorted_buckets[hi_idx])
                accumulated += hi_vol
                hi_idx += 1

        if not included:
            return None, None

        return max(included) + _BUCKET_SIZE, min(included)

    # ─── Naked POC ────────────────────────────────────────────────────────────

    def _find_naked_poc(
        self,
        prior_profile:   dict[float, float],
        current_candles: list[dict],
        current_price:   float,
    ) -> Optional[float]:
        """
        Find the prior session's POC that price has NOT revisited in the current session.
        A POC is "visited" if any current-session candle's [low, high] range crossed it.
        """
        if not prior_profile:
            return None

        prior_poc = self._find_poc(prior_profile)
        if prior_poc is None:
            return None

        # Check if current session price action has touched prior POC
        for candle in current_candles:
            if candle["low"] <= prior_poc <= candle["high"]:
                return None  # Already visited — not naked

        return prior_poc

    # ─── Signal generation ────────────────────────────────────────────────────

    def _generate_signals(
        self,
        current_price: float,
        poc:           Optional[float],
        vah:           Optional[float],
        val:           Optional[float],
        naked_poc:     Optional[float],
        candles_48h:   list[dict],
    ) -> list[str]:
        signals = []

        if poc is not None:
            dist_pct = abs(current_price - poc) / poc
            if dist_pct <= _AT_POC_THRESHOLD_PCT:
                # Determine approach direction from prior candle
                if len(candles_48h) >= 2:
                    prev_close = candles_48h[-2]["close"]
                    if prev_close < poc:
                        signals.append("PRICE_AT_POC_FROM_BELOW")
                    else:
                        signals.append("PRICE_AT_POC_FROM_ABOVE")

        if vah is not None and current_price > vah:
            signals.append("ABOVE_VALUE_AREA")

        if val is not None and current_price < val:
            signals.append("BELOW_VALUE_AREA")

        if naked_poc is not None:
            if naked_poc > current_price:
                signals.append("NAKED_POC_ABOVE")
            else:
                signals.append("NAKED_POC_BELOW")

        return signals
