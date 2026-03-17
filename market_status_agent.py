"""
MarketStatusAgent — Minervini market timing for NSE India.
"The market is your boss." — Strategy 6

Market states (Minervini adapted for NSE/Nifty50):
  BULL          → Nifty Stage 2 + FTD confirmed + ≤3 distribution days in 25d
                   Run all strategies: S1, S2, S3, S4 at full size
  BULL_WATCH    → Nifty Stage 2 but distribution days mounting (4 in 25d)
                   S1/S2 normal; S3 at 50% size; S4 paused
  RALLY_ATTEMPT → Nifty in correction; watching for Follow-Through Day
                   S1/S2 intraday only; no new S3/S4 entries
  BEAR          → Nifty Stage 4 (below SMA200, 200d declining)
                   S2 scalp only; S1/S3/S4 all blocked
  CHOP          → Stage 1 (sideways, no trend)
                   Reduced S2 only

Follow-Through Day (FTD):
  Day 4+ of a rally attempt from a correction low,
  Nifty closes up ≥1.25% on higher volume than prior day.
  Confirms the correction is over and a new uptrend has begun.

Distribution Day:
  Nifty closes down ≥0.2% on higher volume than prior day.
  4–5 in 25 days signals institutional selling — reduce exposure.

All data from DailyCache + TickStore — zero REST during trading.
FTD/Distribution tracking persisted in StateManager kv_store.
"""

import datetime
import json
import sqlite3
import numpy as np
from config import (
    NIFTY50_TOKEN, NIFTY_DIST_DAYS_LIMIT,
    NIFTY_FTD_MIN_PCT, NIFTY_FTD_MIN_DAY,
    STATE_DB, today_ist, now_ist
)


class MarketStatusAgent:

    DIST_DAY_WINDOW = 25   # Rolling window for distribution day count

    def __init__(self, daily_cache, tick_store, nifty_token: int):
        self.dc    = daily_cache
        self.ts    = tick_store
        self.token = nifty_token
        self._init_kv()

    def _init_kv(self):
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                )
            """)
            conn.commit()

    def _kv_get(self, key: str, default: str = "") -> str:
        try:
            with sqlite3.connect(STATE_DB) as conn:
                row = conn.execute(
                    "SELECT value FROM kv_store WHERE key=?", (key,)
                ).fetchone()
            return row[0] if row else default
        except Exception:
            return default

    def _kv_set(self, key: str, value: str):
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_store VALUES (?,?)", (key, value)
            )
            conn.commit()

    def _nifty_stage_2(self) -> bool:
        """Nifty Stage 2: simplified 4-criteria (no RS rank for index)."""
        if not self.dc or not self.dc.is_loaded():
            return True   # Default to allowing trades if cache not ready
        sma50   = self.dc.get_sma50(self.token)
        sma150  = self.dc.get_sma150(self.token)
        sma200  = self.dc.get_sma200(self.token)
        sma200u = self.dc.get_sma200_up(self.token)

        closes = self.dc.get_closes(self.token)
        price  = closes[-1] if closes else 0.0
        if price <= 0 or sma50 <= 0:
            return True

        return (price > sma50 > sma150 > sma200 and sma200u)

    def _count_distribution_days(self) -> int:
        """Count distribution days in last 25 trading days from daily_cache."""
        closes  = self.dc.get_closes(self.token)
        volumes = self.dc.get(self.token).get("volumes", [])

        if len(closes) < self.DIST_DAY_WINDOW + 1:
            return 0

        tail_c = closes[-(self.DIST_DAY_WINDOW + 1):]
        tail_v = (volumes[-(self.DIST_DAY_WINDOW + 1):]
                  if len(volumes) >= self.DIST_DAY_WINDOW + 1 else [])

        count = 0
        for i in range(1, len(tail_c)):
            change_pct = (tail_c[i] - tail_c[i - 1]) / tail_c[i - 1]
            # Distribution: down ≥0.2% on higher volume than prior day
            if change_pct <= -0.002:
                if (len(tail_v) > i and len(tail_v) > i - 1 and
                        tail_v[i] > tail_v[i - 1]):
                    count += 1
        return count

    def _check_ftd(self) -> bool:
        """
        Check for a Follow-Through Day.
        FTD: Day 4+ of rally attempt, Nifty up ≥1.25% on higher volume.
        Tracks rally attempt start in kv_store.
        """
        closes  = self.dc.get_closes(self.token)
        volumes = self.dc.get(self.token).get("volumes", [])
        if len(closes) < 10:
            return True   # Default allow

        # Check current close vs 20-day low to see if we're in rally attempt
        low_20 = min(closes[-20:]) if len(closes) >= 20 else closes[0]
        last   = closes[-1]

        if last <= low_20 * 1.01:
            # Still near lows — not a rally attempt yet
            self._kv_set("nifty_rally_start", "")
            return False

        # Rally attempt: price bounced from recent low
        rally_start = self._kv_get("nifty_rally_start")
        today_str   = today_ist().isoformat()
        if not rally_start:
            self._kv_set("nifty_rally_start", today_str)
            return False

        try:
            start_date = datetime.date.fromisoformat(rally_start)
            rally_days = (today_ist() - start_date).days
        except Exception:
            rally_days = 0

        if rally_days < NIFTY_FTD_MIN_DAY:
            return False   # Too early for FTD

        # Check if today is a FTD candidate: up ≥1.25% on higher volume
        if len(closes) >= 2 and len(volumes) >= 2:
            daily_chg = (closes[-1] - closes[-2]) / closes[-2] * 100
            vol_up    = volumes[-1] > volumes[-2] if len(volumes) >= 2 else False
            if daily_chg >= NIFTY_FTD_MIN_PCT and vol_up:
                self._kv_set("nifty_ftd_confirmed", today_str)
                return True

        # Check if FTD was confirmed recently (within 20 trading days)
        ftd_date_str = self._kv_get("nifty_ftd_confirmed")
        if ftd_date_str:
            try:
                ftd_date = datetime.date.fromisoformat(ftd_date_str)
                if (today_ist() - ftd_date).days <= 20:
                    return True
            except Exception:
                pass
        return False

    def detect(self) -> str:
        """
        Returns the current Minervini market status string.
        Called at pre-market and every 30 minutes by BNFEngine.
        """
        if not self.dc or not self.dc.is_loaded():
            return "BULL"   # Default: allow trading if cache not ready

        stage2 = self._nifty_stage_2()
        dist   = self._count_distribution_days()
        ftd    = self._check_ftd()

        # Nifty Stage 4 (below SMA200, declining) → BEAR
        sma200  = self.dc.get_sma200(self.token)
        sma200u = self.dc.get_sma200_up(self.token)
        closes  = self.dc.get_closes(self.token)
        price   = closes[-1] if closes else 0.0

        if sma200 > 0 and price < sma200 * 0.98 and not sma200u:
            return "BEAR"

        if not stage2:
            return "RALLY_ATTEMPT" if ftd else "CHOP"

        if dist >= NIFTY_DIST_DAYS_LIMIT:
            return "BULL_WATCH"

        return "BULL"
