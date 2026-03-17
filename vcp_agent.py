"""
VCPAgent — Volatility Contraction Pattern detector.
"My most powerful pattern." — Mark Minervini

VCP Structure (from Trade Like a Stock Market Wizard):
  - 2–6 pullbacks from a prior high, each shallower than prior
    e.g. -30%, -15%, -7%, -4%
  - Volatility (range width) reduces progressively
  - Volume dries up in contractions (below 20-day average)
  - Pullbacks shift rightward (time between troughs increases or stays)
  - Duration: 4–8 weeks typical
  - Prerequisites: Stage 2 + strong fundamentals

Algorithm:
  1. Find peak price over last 40–80 trading days (base start)
  2. Identify swing lows (local minima) within the base
  3. Measure depth of each drawdown from the preceding swing high
  4. Confirm each depth < prior depth (tightening)
  5. Confirm contraction volume < 20-day avg in pullback weeks
  6. Define pivot: the highest swing high before final contraction
  7. Stop: the base low (or S3_MAX_STOP_PCT below pivot if base low is too deep)

Uses DailyCache for OHLCV data (260 days) — zero REST during trading.
"""

import numpy as np
from config import (
    S3_VCP_MIN_CONTRACTIONS, S3_VCP_MAX_CONTRACTIONS, S3_MAX_STOP_PCT
)


class VCPAgent:

    # Maximum drawdown allowed for the FIRST (widest) pullback
    MAX_FIRST_DRAWDOWN = 0.45   # 45%
    # Minimum tightening ratio: each contraction must be < prior × this factor
    TIGHTENING_RATIO   = 0.85   # each pullback ≤ 85% of prior

    def __init__(self, daily_cache):
        self.dc = daily_cache

    def detect_vcp(self, token: int) -> dict:
        """
        Returns VCP info dict if a valid VCP is detected, else None.

        Return dict keys:
          pivot_price      — entry pivot (highest swing high in base)
          stop_price       — base low or capped at S3_MAX_STOP_PCT below pivot
          n_contractions   — number of VCP contractions found (2–6)
          final_depth_pct  — depth of final (tightest) contraction %
          base_start_idx   — index in closes[] where base begins
        """
        if not self.dc or not self.dc.is_loaded():
            return None

        closes  = self.dc.get_closes(token)
        volumes = self.dc.get(token).get("volumes", [])

        if len(closes) < 40:
            return None

        # Use last 80 days as the candidate base window
        window_closes  = closes[-80:]
        window_vols    = volumes[-80:] if len(volumes) >= 80 else volumes

        avg_vol_20 = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else 1.0

        # Find swing highs and lows in the window
        highs, lows = self._find_swings(window_closes)

        if len(lows) < S3_VCP_MIN_CONTRACTIONS:
            return None

        # Measure drawdown depth for each trough
        depths = []
        for i, trough_idx in enumerate(lows):
            # Find the preceding swing high
            preceding_highs = [h for h in highs if h < trough_idx]
            if not preceding_highs:
                continue
            prev_high_idx   = preceding_highs[-1]
            prev_high_price = window_closes[prev_high_idx]
            trough_price    = window_closes[trough_idx]
            if prev_high_price <= 0:
                continue
            depth = (prev_high_price - trough_price) / prev_high_price
            depths.append((trough_idx, trough_price, depth, avg_vol_20))

        if len(depths) < S3_VCP_MIN_CONTRACTIONS:
            return None
        if len(depths) > S3_VCP_MAX_CONTRACTIONS:
            depths = depths[-S3_VCP_MAX_CONTRACTIONS:]

        # Criterion: each depth must be tighter than prior
        depth_values = [d[2] for d in depths]
        if depth_values[0] > self.MAX_FIRST_DRAWDOWN:
            return None   # First pullback too deep
        for i in range(1, len(depth_values)):
            if depth_values[i] > depth_values[i - 1] * self.TIGHTENING_RATIO:
                return None   # Not tightening

        # Confirm VCP volume dry-up: volume in final contraction < 20d avg
        final_trough_idx = depths[-1][0]
        if final_trough_idx > 0 and len(window_vols) > final_trough_idx:
            final_zone_vols = window_vols[max(0, final_trough_idx - 5):
                                          final_trough_idx + 1]
            if final_zone_vols:
                vol_ratio = np.mean(final_zone_vols) / max(avg_vol_20, 1)
                if vol_ratio > 0.9:   # Volume still elevated — not dried up
                    return None

        # Pivot = highest high after last trough
        last_trough_idx = depths[-1][0]
        post_trough     = window_closes[last_trough_idx:]
        if len(post_trough) < 3:
            return None   # Insufficient price action after final contraction
        pivot_price = max(post_trough)

        # Stop = base low, capped at S3_MAX_STOP_PCT below pivot
        base_low    = min(d[1] for d in depths)
        stop_price  = max(base_low * 0.99, pivot_price * (1 - S3_MAX_STOP_PCT))

        return {
            "pivot_price":     round(pivot_price, 2),
            "stop_price":      round(stop_price, 2),
            "n_contractions":  len(depths),
            "final_depth_pct": round(depth_values[-1] * 100, 1),
            "base_start_idx":  len(closes) - 80,
        }

    def _find_swings(self, prices: list) -> tuple:
        """Find local swing highs and lows. Returns (highs_idx, lows_idx)."""
        highs, lows = [], []
        n = len(prices)
        for i in range(2, n - 2):
            # Swing high: higher than surrounding 2 bars on each side
            if (prices[i] > prices[i - 1] and prices[i] > prices[i - 2] and
                    prices[i] > prices[i + 1] and prices[i] > prices[i + 2]):
                highs.append(i)
            # Swing low: lower than surrounding 2 bars on each side
            elif (prices[i] < prices[i - 1] and prices[i] < prices[i - 2] and
                  prices[i] < prices[i + 1] and prices[i] < prices[i + 2]):
                lows.append(i)
        return highs, lows
