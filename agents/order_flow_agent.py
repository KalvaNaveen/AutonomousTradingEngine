"""
OrderFlowAgent v14.1 — Full Institutional Tape Reading.

Professional tape readers focus on 4 key signals:

1. CUMULATIVE DELTA: Running total of (buyer aggressor vol - seller aggressor vol).
   Positive delta = institutions are lifting asks (accumulating).
   Negative delta = institutions are hitting bids (distributing).

2. ABSORPTION: Price stays flat while aggressive selling volume pours in,
   but price doesn't drop. This means a large buyer is absorbing all the
   selling — extremely bullish hidden demand.

3. LARGE ORDER DETECTION: Blocks > 5x the average trade size indicate
   institutional activity (iceberg orders, block deals).

4. L2 IMBALANCE: Real-time bid/ask quantity ratio from Top-5 depth.

These 4 signals are combined into a composite FLOW SCORE:
  Score > +50  → Institutional Accumulation (strong buy confirmation)
  Score < -50  → Institutional Distribution (block entries)
  -50 to +50   → Neutral / mixed flow

Data source: tick_store._store[token] (populated by Kite WebSocket FULL mode)
"""

import threading
from collections import defaultdict


class OrderFlowAgent:

    # Composite score thresholds
    ACCUMULATION_THRESHOLD  =  50   # Strong institutional buying
    DISTRIBUTION_THRESHOLD  = -50   # Strong institutional selling

    # L2 imbalance thresholds (kept from v14.0)
    STRONG_BUY_RATIO  = 1.5
    STRONG_SELL_RATIO  = 0.65

    def __init__(self, tick_store):
        self.ticks = tick_store
        self._lock = threading.Lock()

    # ── 1. L2 Bid/Ask Imbalance (Snapshot) ─────────────────────────────

    def get_imbalance(self, token: int) -> float:
        """Real-time bid/ask ratio. > 1.0 = more buyers, < 1.0 = more sellers."""
        with self.ticks._lock:
            s = self.ticks._store.get(token)
            if not s:
                return 1.0
            return s.get("depth", {}).get("bid_ask_ratio", 1.0)

    # ── 2. Cumulative Delta ────────────────────────────────────────────

    def get_cumulative_delta(self, token: int) -> float:
        """
        Running (buy_aggressor_vol - sell_aggressor_vol) since market open.
        Positive = net buying, Negative = net selling.
        """
        with self.ticks._lock:
            s = self.ticks._store.get(token)
            if not s:
                return 0.0
            return s.get("_cum_delta", 0.0)

    # ── 3. Absorption Detection ────────────────────────────────────────

    def detect_absorption(self, token: int) -> str:
        """
        Checks the last 20 depth snapshots for price holding while
        one side has overwhelming volume — the hallmark of a large
        hidden buyer or seller absorbing the flow.

        Returns: "BUY_ABSORPTION", "SELL_ABSORPTION", or "NONE"
        """
        with self.ticks._lock:
            s = self.ticks._store.get(token)
            if not s:
                return "NONE"
            hist = s.get("_depth_history", [])

        if len(hist) < 10:
            return "NONE"

        recent = hist[-20:]
        prices = [h["ltp"] for h in recent if h["ltp"] > 0]
        if not prices:
            return "NONE"

        # Price range (should be tight for absorption)
        price_range_pct = (max(prices) - min(prices)) / max(prices) * 100

        # Average ask-side volume vs bid-side volume
        avg_aq = sum(h["aq"] for h in recent) / len(recent)
        avg_bq = sum(h["bq"] for h in recent) / len(recent)

        # BUY ABSORPTION: heavy ask volume but price NOT dropping
        # Someone is absorbing all the selling
        if avg_aq > avg_bq * 1.8 and price_range_pct < 0.3:
            return "BUY_ABSORPTION"

        # SELL ABSORPTION: heavy bid volume but price NOT rising
        # Someone is absorbing all the buying (distribution)
        if avg_bq > avg_aq * 1.8 and price_range_pct < 0.3:
            return "SELL_ABSORPTION"

        return "NONE"

    # ── 4. Large Order Detection ───────────────────────────────────────

    def has_large_orders(self, token: int) -> bool:
        """True if the most recent trade was > 5x the average trade size."""
        with self.ticks._lock:
            s = self.ticks._store.get(token)
            if not s:
                return False
            return s.get("_last_large_order", False)

    # ── Composite Flow Score ───────────────────────────────────────────

    def get_flow_score(self, token: int) -> int:
        """
        Combines all 4 signals into a single composite score [-100, +100].

        Scoring:
          L2 Imbalance:      -25 to +25
          Cumulative Delta:   -25 to +25
          Absorption:         -25 to +25
          Large Orders:       -25 to +25
        """
        score = 0

        # 1. L2 Imbalance (+/- 25)
        ratio = self.get_imbalance(token)
        if ratio >= self.STRONG_BUY_RATIO:
            score += 25
        elif ratio <= self.STRONG_SELL_RATIO:
            score -= 25
        else:
            # Linear interpolation between sell and buy thresholds
            mid = (self.STRONG_BUY_RATIO + self.STRONG_SELL_RATIO) / 2
            score += int((ratio - mid) / (self.STRONG_BUY_RATIO - mid) * 25)

        # 2. Cumulative Delta (+/- 25)
        delta = self.get_cumulative_delta(token)
        if delta > 0:
            score += min(25, int(delta / 1000))  # Scale: +1000 vol = +1 point
        elif delta < 0:
            score += max(-25, int(delta / 1000))

        # 3. Absorption (+/- 25)
        absorption = self.detect_absorption(token)
        if absorption == "BUY_ABSORPTION":
            score += 25   # Hidden buyer — very bullish
        elif absorption == "SELL_ABSORPTION":
            score -= 25   # Hidden seller — very bearish

        # 4. Large Orders (+/- 25)
        if self.has_large_orders(token):
            # Direction depends on cumulative delta
            if delta > 0:
                score += 15  # Large buy block
            elif delta < 0:
                score -= 15  # Large sell block

        return max(-100, min(100, score))

    # ── Decision Methods (used by execution_agent.py) ──────────────────

    def is_sell_pressure(self, token: int) -> bool:
        """Returns True if composite flow indicates distribution."""
        return self.get_flow_score(token) <= self.DISTRIBUTION_THRESHOLD

    def is_buy_pressure(self, token: int) -> bool:
        """Returns True if composite flow indicates accumulation."""
        return self.get_flow_score(token) >= self.ACCUMULATION_THRESHOLD

    def get_flow_label(self, token: int) -> str:
        """Human-readable label for Telegram alerts and logging."""
        score = self.get_flow_score(token)
        delta = self.get_cumulative_delta(token)
        absorption = self.detect_absorption(token)

        if score >= self.ACCUMULATION_THRESHOLD:
            tag = "ACCUMULATION"
        elif score <= self.DISTRIBUTION_THRESHOLD:
            tag = "DISTRIBUTION"
        elif score > 0:
            tag = "MILD_BUY"
        elif score < 0:
            tag = "MILD_SELL"
        else:
            tag = "NEUTRAL"

        parts = [f"{tag}(score={score})"]
        if delta != 0:
            parts.append(f"Δ={delta:+,.0f}")
        if absorption != "NONE":
            parts.append(absorption)
        return " | ".join(parts)
