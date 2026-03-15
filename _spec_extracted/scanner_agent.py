"""
ScannerAgent v6 — all intraday data reads from tick_store and daily_cache.
No REST calls during scan loops.

detect_regime()         → tick_store (VIX LTP, AD ratio) + daily_cache (Nifty EMA)
scan_s1_ema_divergence() → daily_cache (history/indicators) + tick_store (current price)
scan_s2_overreaction()  → tick_store only (day_open, LTP, volume, depth, candles)
"""

import datetime
import numpy as np
from data_agent import DataAgent
from config import *


class ScannerAgent:

    def __init__(self, data_agent: DataAgent, blackout_calendar):
        self.data     = data_agent
        self.blackout = blackout_calendar

    def detect_regime(self) -> str:
        """
        tick_store: VIX LTP + AD ratio (zero REST calls if ready).
        daily_cache: Nifty EMA-25 (pre-computed at 8:45 AM).
        REST fallback used pre-market.

        Returns "EXTREME_PANIC" if VIX >= VIX_EXTREME_STOP (30).
        Callers must block all new entries on EXTREME_PANIC.
        Open positions are still monitored and exited normally.
        """
        vix = self.data.get_india_vix()

        # Hard block — checked before anything else
        if vix >= VIX_EXTREME_STOP:
            return "EXTREME_PANIC"
        ad_ratio = self.data.get_advance_decline_ratio()

        # Nifty position relative to EMA-25
        if (self.data.daily_cache and
                self.data.daily_cache.is_loaded()):
            ema_25    = self.data.daily_cache.get_ema25(NIFTY50_TOKEN)
            ts        = self.data.tick_store
            nifty_ltp = (ts.get_ltp_if_fresh(NIFTY50_TOKEN)
                         if ts and ts.is_fresh() else 0.0)
            above_ema = nifty_ltp > ema_25 if nifty_ltp > 0 else False
        else:
            # REST fallback — used pre-market before cache loaded
            hist = self.data.get_daily_ohlcv(NIFTY50_TOKEN, days=60)
            if len(hist) < 30:
                return "CHOP"
            closes    = [d["close"] for d in hist]
            ema_25    = DataAgent.compute_ema(closes, 25)[-1]
            above_ema = closes[-1] > ema_25

        if vix > VIX_BEAR_PANIC and not above_ema and ad_ratio < 0.40:
            return "BEAR_PANIC"
        if VIX_NORMAL_LOW <= vix <= VIX_NORMAL_HIGH:
            return "NORMAL"
        if vix < VIX_BULL_MAX and above_ema and ad_ratio > 0.60:
            return "BULL"
        return "CHOP"

    def get_s1_min_deviation(self, regime: str) -> float:
        return {
            "BEAR_PANIC": S1_DEVIATION_MIN,
            "NORMAL":     S1_DEVIATION_NORMAL,
            "BULL":       S1_DEVIATION_BULL,
            "CHOP":       1.0,
        }.get(regime, 1.0)

    def is_valid_trading_time(self) -> tuple:
        now = now_ist()
        t   = now.time()
        if self.blackout.is_blackout(now.date()):
            return False, "BLACKOUT_DATE"
        if now.weekday() >= 5:
            return False, "WEEKEND"
        if t < datetime.time(9, 30):
            return False, "BEFORE_HUNT_WINDOW"
        if t >= datetime.time(15, 0):
            return False, "AFTER_LAST_ENTRY"
        # VIX hard block — checked last (requires a data call)
        vix = self.data.get_india_vix()
        if vix >= VIX_EXTREME_STOP:
            return False, f"EXTREME_PANIC_VIX_{vix:.1f}"
        return True, "VALID"

    def scan_s1_ema_divergence(self, regime: str) -> list:
        """
        Pre-market (9:00 AM): daily_cache for all indicator values.
        During session: tick_store supplies current price; cache has history.
        Loop itself makes ZERO REST calls if both cache and tick_store ready.
        """
        if regime == "CHOP":
            return []
        min_dev = self.get_s1_min_deviation(regime)
        signals = []
        ts_ready    = (self.data.tick_store and
                       self.data.tick_store.is_fresh())
        cache_ready = self.data.daily_cache and self.data.daily_cache.is_loaded()

        for token, symbol in self.data.UNIVERSE.items():
            # ── Turnover filter — cache ───────────────────────────────
            if self.data.get_avg_daily_turnover_cr(token) < S1_MIN_TURNOVER_CR:
                continue

            # ── Circuit breaker — cache + tick LTP ───────────────────
            if self.data.check_circuit_breaker(symbol):
                continue

            # ── Historical indicators — cache ─────────────────────────
            if cache_ready:
                closes  = self.data.daily_cache.get_closes(token)
                ema_25  = self.data.daily_cache.get_ema25(token)
                rsi     = self.data.daily_cache.get_rsi14(token)
                bb_lo   = self.data.daily_cache.get_bb_lower(token)
                d_cache = self.data.daily_cache.get(token)
                volumes = d_cache.get("volumes", [])
                if len(closes) < 30:
                    continue
            else:
                # REST fallback (pre-market before cache, or cache miss)
                hist = self.data.get_daily_ohlcv(token, days=70)
                if len(hist) < 30:
                    continue
                closes  = [d["close"] for d in hist]
                volumes = [d["volume"] for d in hist]
                ema_25  = DataAgent.compute_ema(closes, 25)[-1]
                rsi     = (DataAgent.compute_rsi(closes, 14) or [50])[-1]
                _, _, bb_lo = DataAgent.compute_bollinger(closes, 20, 2.0)

            # ── Current price — fresh tick first, cache close fallback ──
            current = (self.data.tick_store.get_ltp_if_fresh(token)
                       if ts_ready else 0.0)
            if current <= 0:
                current = closes[-1] if closes else 0.0
            if current <= 0:
                continue

            # ── Deviation ─────────────────────────────────────────────
            dev = (ema_25 - current) / ema_25
            if not (min_dev <= dev <= S1_DEVIATION_MAX):
                continue

            # ── RSI ───────────────────────────────────────────────────
            if rsi >= S1_RSI_THRESHOLD:
                continue

            # ── Bollinger ─────────────────────────────────────────────
            if current >= bb_lo:
                continue

            # ── Volume confirmation — last 3 days vs 20-day avg ───────
            if len(volumes) >= 20:
                vol_ratio = (np.mean(volumes[-3:]) /
                             max(np.mean(volumes[-20:]), 1))
            else:
                vol_ratio = 0.0
            if vol_ratio < S1_VOLUME_MULTIPLIER:
                continue

            # ── Depth — tick_store ────────────────────────────────────
            depth = self.data.get_order_depth(token)
            if depth and depth.get("bid_ask_ratio", 1.0) < 1.0:
                continue

            support    = self.data.compute_pivot_support(token)
            stop_price = max(current * (1 - S1_HARD_STOP_PCT), support * 0.98)

            signals.append({
                "strategy":       "S1_EMA_DIVERGENCE",
                "symbol":         symbol,
                "token":          token,
                "regime":         regime,
                "entry_price":    current,
                "partial_target": round(current + (ema_25 - current) * 0.50, 2),
                "target_price":   round(ema_25 * 0.97, 2),
                "stop_price":     round(stop_price, 2),
                "deviation_pct":  round(dev * 100, 2),
                "rsi":            round(rsi, 1),
                "rvol":           round(vol_ratio, 2),
                "product":        "CNC",
                "max_hold_days":  S1_MAX_HOLD_DAYS,
                "entry_time":     None,
                "entry_date":     None,
            })

        return sorted(signals, key=lambda x: x["deviation_pct"], reverse=True)[:5]

    def scan_s2_overreaction(self) -> list:
        """
        Entirely tick_store driven during trading hours.
        day_open, LTP, volume, depth, 5-min candles — all from TickStore.
        Zero REST calls per scan cycle when tick_store is ready.
        """
        now  = now_ist().time()
        in_p = datetime.time(9, 30) <= now <= datetime.time(11, 0)
        in_s = datetime.time(14, 0) <= now <= datetime.time(15, 0)
        if not (in_p or in_s):
            return []

        ts_ready = (self.data.tick_store and
                    self.data.tick_store.is_fresh())
        signals  = []

        for token, symbol in self.data.UNIVERSE.items():
            # ── Turnover — cache ──────────────────────────────────────
            if self.data.get_avg_daily_turnover_cr(token) < S2_MIN_TURNOVER_CR:
                continue

            # ── Current price and day open — fresh tick, REST fallback ──
            if ts_ready:
                current  = self.data.tick_store.get_ltp_if_fresh(token)
                day_open = self.data.tick_store.get_day_open(token)
            else:
                # REST fallback — WebSocket stale or pre-market
                candles  = self.data.get_intraday_ohlcv(token, "5minute")
                if len(candles) < 6:
                    continue
                day_open = candles[0]["open"]
                current  = candles[-1]["close"]

            if current <= 0 or day_open <= 0:
                continue

            # ── Gap-down filter ───────────────────────────────────────
            # Reject if stock gapped down >3% from previous close.
            # A gap-down indicates a structural/fundamental event (earnings,
            # news, macro). BNF avoided these — the bounce logic assumes
            # intraday panic, not overnight structural damage.
            prev_close = 0.0
            if self.data.daily_cache and self.data.daily_cache.is_loaded():
                closes = self.data.daily_cache.get_closes(token)
                if len(closes) >= 2:
                    prev_close = closes[-2]   # yesterday's close
            if prev_close > 0:
                gap_down_pct = (prev_close - day_open) / prev_close
                if gap_down_pct > 0.03:      # >3% gap — skip
                    continue

            # ── Circuit breaker — cache ───────────────────────────────
            if self.data.check_circuit_breaker(symbol):
                continue

            # ── Drop filter ───────────────────────────────────────────
            drop = (day_open - current) / day_open
            if not (S2_DROP_MIN <= drop <= S2_DROP_MAX):
                continue

            # ── RVOL — tick_store volume / daily_cache avg ────────────
            rvol = self.data.compute_rvol(token)
            if rvol < S2_RVOL_MIN:
                continue

            # ── Reversal candle — tick_store 5-min candles ────────────
            candles = self.data.get_intraday_ohlcv(token, "5minute")
            if len(candles) < 6:
                continue
            if not self._reversal_candle(candles):
                continue

            # ── Pivot support — daily_cache ───────────────────────────
            support = self.data.compute_pivot_support(token)
            if support > 0 and abs(current - support) / support > 0.015:
                continue

            # ── Bid/ask depth — tick_store ────────────────────────────
            depth = self.data.get_order_depth(token)
            if not depth or depth.get("bid_ask_ratio", 0) < 1.3:
                continue

            signals.append({
                "strategy":          "S2_OVERREACTION",
                "symbol":            symbol,
                "token":             token,
                "entry_price":       current,
                "partial_target_1":  round(current * (1 + S2_PARTIAL_TARGET_1), 2),
                "target_price":      round(current * (1 + S2_PARTIAL_TARGET_2), 2),
                "stop_price":        round(current * (1 - S2_HARD_STOP_PCT), 2),
                "drop_pct":          round(drop * 100, 2),
                "rvol":              round(rvol, 2),
                "bid_ask_ratio":     round(depth.get("bid_ask_ratio", 0), 2),
                "product":           "MIS",
                "time_stop_minutes": S2_TIME_STOP_MINUTES,
                "entry_time":        None,
            })

        return sorted(signals, key=lambda x: x["rvol"], reverse=True)[:3]

    def _reversal_candle(self, candles: list) -> bool:
        if len(candles) < 2:
            return False
        last, prev = candles[-1], candles[-2]
        body  = abs(last["close"] - last["open"])
        rng   = last["high"] - last["low"]
        if rng == 0:
            return False
        lo_wick = min(last["open"], last["close"]) - last["low"]
        hi_wick = last["high"] - max(last["open"], last["close"])
        hammer  = (last["close"] > last["open"] and
                   lo_wick >= 2 * body and hi_wick <= body * 0.5)
        engulf  = (prev["close"] < prev["open"] and
                   last["close"] > last["open"] and
                   last["open"] < prev["close"] and
                   last["close"] > prev["open"])
        return hammer or engulf
