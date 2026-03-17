"""
StageAgent — Minervini Stage Analysis adapted for NSE India.
Determines the stage of a stock using 8 objective criteria.

"You want to be in a stock during Stage 2 and ONLY Stage 2."
— Mark Minervini

The 4 Stages:
  Stage 1: Sideways base — avoid
  Stage 2: Uptrend — buy here (all 8 criteria below must pass)
  Stage 3: Topping — exit
  Stage 4: Downtrend — avoid

Stage 2 Confirmation (all 8 criteria from Minervini):
  1. Price > SMA50
  2. Price > SMA150
  3. Price > SMA200
  4. SMA50 > SMA150 > SMA200  (proper order)
  5. SMA200 trending up for ≥ 1 month (20 trading days)
  6. Price ≥ 30% above 52-week low
  7. Price within 25% of 52-week high
  8. RS score ≥ 70  (top 30% of universe performers)

All inputs from DailyCache (pre-computed at 8:45 AM) + TickStore (live LTP).
Zero REST calls during trading hours.
"""

from config import S3_MIN_RS_SCORE


class StageAgent:

    def __init__(self, daily_cache):
        self.dc = daily_cache

    def is_stage_2(self, token: int, current_price: float = 0.0) -> bool:
        """
        Returns True only if ALL 8 Stage 2 criteria pass.
        current_price: live LTP from tick_store; if 0 uses latest close.
        """
        if not self.dc or not self.dc.is_loaded():
            return False

        sma50    = self.dc.get_sma50(token)
        sma150   = self.dc.get_sma150(token)
        sma200   = self.dc.get_sma200(token)
        sma200_up = self.dc.get_sma200_up(token)
        high_52w = self.dc.get_high_52w(token)
        low_52w  = self.dc.get_low_52w(token)
        rs_score = self.dc.get_rs_score(token)

        if sma50 <= 0 or sma150 <= 0 or sma200 <= 0:
            return False

        # Use latest close if no live price provided
        price = current_price
        if price <= 0:
            closes = self.dc.get_closes(token)
            price  = closes[-1] if closes else 0.0
        if price <= 0:
            return False

        # Criteria 1–3: price above all three SMAs
        if price <= sma50 or price <= sma150 or price <= sma200:
            return False
        # Criterion 4: proper SMA order
        if not (sma50 > sma150 > sma200):
            return False
        # Criterion 5: SMA200 trending up
        if not sma200_up:
            return False
        # Criterion 6: ≥30% above 52-week low
        if low_52w > 0 and price < low_52w * 1.30:
            return False
        # Criterion 7: within 25% of 52-week high
        if high_52w > 0 and price < high_52w * 0.75:
            return False
        # Criterion 8: RS score ≥ 70
        if rs_score < S3_MIN_RS_SCORE:
            return False

        return True

    def get_stage(self, token: int, current_price: float = 0.0) -> str:
        """
        Returns one of: STAGE_1, STAGE_2, STAGE_3, STAGE_4, UNKNOWN.
        STAGE_2 = all 8 criteria pass (most selective).
        Others are approximations based on SMA structure.
        """
        sma50  = self.dc.get_sma50(token)
        sma200 = self.dc.get_sma200(token)

        closes = self.dc.get_closes(token)
        price  = current_price or (closes[-1] if closes else 0.0)

        if sma50 <= 0 or sma200 <= 0 or price <= 0:
            return "UNKNOWN"

        if self.is_stage_2(token, price):
            return "STAGE_2"

        sma200_up = self.dc.get_sma200_up(token)
        if price < sma200 and not sma200_up:
            return "STAGE_4"   # Below SMA200, downtrend
        if price > sma200 and not sma200_up:
            return "STAGE_3"   # Above SMA200 but 200d rolling over
        return "STAGE_1"       # Sideways base
