"""
Block 7: Liquidity Map Analyzer.

Detects order walls (large limit-order clusters) in the order book
and measures trade delta (buyer vs seller aggression) from recent trades.

Signals emitted:
  ORDER_WALL_ABOVE  — large ask cluster within 2% above price (liquidity magnet / resistance)
  ORDER_WALL_BELOW  — large bid cluster within 2% below price (liquidity magnet / support)
  DELTA_BULL        — buyers dominating recent trades (net positive delta)
  DELTA_BEAR        — sellers dominating recent trades (net negative delta)
"""

from dataclasses import dataclass, field
from typing import Optional

import config
from logger import get_logger

log = get_logger(__name__)


@dataclass
class OrderWall:
    price:  float   # cluster center price
    volume: float   # total quoted volume in cluster
    side:   str     # "bid" or "ask"


@dataclass
class LiquidityResult:
    signals:    list[str]         = field(default_factory=list)
    bid_walls:  list[OrderWall]   = field(default_factory=list)   # below price
    ask_walls:  list[OrderWall]   = field(default_factory=list)   # above price
    delta_ratio: Optional[float]  = None   # buy_vol / total_vol  (>0.5 = bullish)


class LiquidityAnalyzer:
    """
    Analyzes order book and recent trades to detect liquidity clusters and delta.

    Order wall detection:
      Cluster all order-book levels within WALL_CLUSTER_PCT of each other.
      A cluster is a "wall" when its total volume > WALL_VOLUME_MULTIPLIER × mean
      cluster volume AND it sits within WALL_SCAN_RANGE_PCT of current price.

    Delta:
      Net delta = buy_volume − sell_volume over the last N trades.
      Bullish when buy_volume / total_volume > DELTA_THRESHOLD.
    """

    WALL_CLUSTER_PCT       = 0.003   # merge levels within 0.3% of each other
    WALL_SCAN_RANGE_PCT    = 0.02    # only look at walls within 2% of price
    WALL_VOLUME_MULTIPLIER = 5.0     # wall volume must be >5× mean cluster volume
    DELTA_THRESHOLD        = 0.55    # >55% buy volume → DELTA_BULL; <45% → DELTA_BEAR

    def analyze(
        self,
        orderbook:     dict,
        recent_trades: list[dict],
        current_price: float,
    ) -> LiquidityResult:
        """
        Main entry point.

        orderbook:     output of BybitClient.get_orderbook()
        recent_trades: output of BybitClient.get_recent_trades()
        current_price: latest traded price
        """
        if not current_price or current_price <= 0:
            log.warning("Liquidity: invalid current_price=%.2f", current_price)
            return LiquidityResult()

        bid_walls = self._detect_walls(orderbook.get("bids", []), current_price, "bid")
        ask_walls = self._detect_walls(orderbook.get("asks", []), current_price, "ask")
        delta_ratio = self._calc_delta(recent_trades)

        signals: list[str] = []

        if ask_walls:
            signals.append("ORDER_WALL_ABOVE")
            log.debug(
                "Liquidity: ORDER_WALL_ABOVE — %d cluster(s), nearest $%.0f vol=%.2f",
                len(ask_walls), ask_walls[0].price, ask_walls[0].volume,
            )

        if bid_walls:
            signals.append("ORDER_WALL_BELOW")
            log.debug(
                "Liquidity: ORDER_WALL_BELOW — %d cluster(s), nearest $%.0f vol=%.2f",
                len(bid_walls), bid_walls[0].price, bid_walls[0].volume,
            )

        if delta_ratio is not None:
            if delta_ratio > self.DELTA_THRESHOLD:
                signals.append("DELTA_BULL")
                log.debug("Liquidity: DELTA_BULL ratio=%.3f", delta_ratio)
            elif delta_ratio < (1 - self.DELTA_THRESHOLD):
                signals.append("DELTA_BEAR")
                log.debug("Liquidity: DELTA_BEAR ratio=%.3f", delta_ratio)

        return LiquidityResult(
            signals=signals,
            bid_walls=bid_walls,
            ask_walls=ask_walls,
            delta_ratio=delta_ratio,
        )

    # ─── Order wall detection ─────────────────────────────────────────────────

    def _detect_walls(
        self,
        levels:        list[list],   # [[price, qty], ...]
        current_price: float,
        side:          str,
    ) -> list[OrderWall]:
        """
        Detect liquidity walls on one side of the book.
        Returns walls sorted by distance to current_price (nearest first).
        """
        if not levels:
            return []

        scan_lo = current_price * (1 - self.WALL_SCAN_RANGE_PCT)
        scan_hi = current_price * (1 + self.WALL_SCAN_RANGE_PCT)

        # Filter to scan range
        in_range = [
            (float(p), float(q)) for p, q in levels
            if scan_lo <= float(p) <= scan_hi
        ]
        if not in_range:
            return []

        # Cluster nearby levels
        clusters = self._cluster_levels(in_range)

        if not clusters:
            return []

        # Mean cluster volume
        volumes   = [vol for _, vol in clusters]
        mean_vol  = sum(volumes) / len(volumes)
        threshold = mean_vol * self.WALL_VOLUME_MULTIPLIER

        walls = [
            OrderWall(price=price, volume=vol, side=side)
            for price, vol in clusters
            if vol >= threshold
        ]

        # Sort nearest to price first
        walls.sort(key=lambda w: abs(w.price - current_price))
        return walls

    def _cluster_levels(
        self, levels: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        """
        Merge price levels within WALL_CLUSTER_PCT of each other.
        Returns list of (cluster_center_price, total_volume).
        """
        if not levels:
            return []

        sorted_levels = sorted(levels, key=lambda x: x[0])
        clusters: list[list[tuple[float, float]]] = [[sorted_levels[0]]]

        for price, qty in sorted_levels[1:]:
            last = clusters[-1]
            center = sum(p for p, _ in last) / len(last)
            if abs(price - center) / center <= self.WALL_CLUSTER_PCT:
                last.append((price, qty))
            else:
                clusters.append([(price, qty)])

        result = []
        for cluster in clusters:
            center = sum(p for p, _ in cluster) / len(cluster)
            total  = sum(q for _, q in cluster)
            result.append((center, total))
        return result

    # ─── Delta calculation ────────────────────────────────────────────────────

    def _calc_delta(self, trades: list[dict]) -> Optional[float]:
        """
        Calculate buy volume ratio from recent trades.
        Returns buy_vol / total_vol, or None if no trades.
        """
        if not trades:
            return None

        buy_vol  = sum(t["qty"] for t in trades if t.get("side") == "Buy")
        sell_vol = sum(t["qty"] for t in trades if t.get("side") == "Sell")
        total    = buy_vol + sell_vol

        if total == 0:
            return None

        return buy_vol / total
