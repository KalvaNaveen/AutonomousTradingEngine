import datetime
import numpy as np
from data_agent import DataAgent
from config import *


class ScannerAgent:

    def __init__(self, data_agent: DataAgent, blackout_calendar):
        self.data     = data_agent
        self.blackout = blackout_calendar

    def detect_regime(self) -> str:
        vix      = self.data.get_india_vix()
        ad_ratio = self.data.get_advance_decline_ratio()
        hist     = self.data.get_daily_ohlcv(NIFTY50_TOKEN, days=60)
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
        now = datetime.datetime.now()
        t   = now.time()
        if self.blackout.is_blackout(now.date()):
            return False, "BLACKOUT_DATE"
        if now.weekday() >= 5:
            return False, "WEEKEND"
        if t < datetime.time(9, 30):
            return False, "BEFORE_HUNT_WINDOW"
        if t >= datetime.time(15, 0):
            return False, "AFTER_LAST_ENTRY"
        return True, "VALID"

    def scan_s1_ema_divergence(self, regime: str) -> list:
        if regime == "CHOP":
            return []
        min_dev  = self.get_s1_min_deviation(regime)
        signals  = []

        for token, symbol in self.data.UNIVERSE.items():
            if self.data.get_avg_daily_turnover_cr(token) < S1_MIN_TURNOVER_CR:
                continue
            if self.data.check_circuit_breaker(symbol):
                continue
            hist = self.data.get_daily_ohlcv(token, days=70)
            if len(hist) < 30:
                continue

            closes  = [d["close"] for d in hist]
            volumes = [d["volume"] for d in hist]
            current = closes[-1]
            ema_25  = DataAgent.compute_ema(closes, 25)[-1]
            dev     = (ema_25 - current) / ema_25

            if not (min_dev <= dev <= S1_DEVIATION_MAX):
                continue
            rsi = (DataAgent.compute_rsi(closes, 14) or [50])[-1]
            if rsi >= S1_RSI_THRESHOLD:
                continue
            _, _, bb_lo = DataAgent.compute_bollinger(closes, 20, 2.0)
            if current >= bb_lo:
                continue
            vol_ratio = np.mean(volumes[-3:]) / max(np.mean(volumes[-20:]), 1)
            if vol_ratio < S1_VOLUME_MULTIPLIER:
                continue
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
        now = datetime.datetime.now().time()
        in_p = datetime.time(9, 30) <= now <= datetime.time(11, 0)
        in_s = datetime.time(14, 0) <= now <= datetime.time(15, 0)
        if not (in_p or in_s):
            return []

        signals = []
        for token, symbol in self.data.UNIVERSE.items():
            if self.data.get_avg_daily_turnover_cr(token) < S2_MIN_TURNOVER_CR:
                continue
            if self.data.check_circuit_breaker(symbol):
                continue
            candles = self.data.get_intraday_ohlcv(token, "5minute")
            if len(candles) < 6:
                continue

            day_open = candles[0]["open"]
            current  = candles[-1]["close"]
            drop     = (day_open - current) / day_open

            if not (S2_DROP_MIN <= drop <= S2_DROP_MAX):
                continue
            rvol = self.data.compute_rvol(token)
            if rvol < S2_RVOL_MIN:
                continue
            if not self._reversal_candle(candles):
                continue
            support = self.data.compute_pivot_support(token)
            if support > 0 and abs(current - support) / support > 0.015:
                continue
            depth = self.data.get_order_depth(token)
            if not depth or depth.get("bid_ask_ratio", 0) < 1.3:
                continue

            signals.append({
                "strategy":         "S2_OVERREACTION",
                "symbol":           symbol,
                "token":            token,
                "entry_price":      current,
                "partial_target_1": round(current * (1 + S2_PARTIAL_TARGET_1), 2),
                "target_price":     round(current * (1 + S2_PARTIAL_TARGET_2), 2),
                "stop_price":       round(current * (1 - S2_HARD_STOP_PCT), 2),
                "drop_pct":         round(drop * 100, 2),
                "rvol":             round(rvol, 2),
                "bid_ask_ratio":    round(depth.get("bid_ask_ratio", 0), 2),
                "product":          "MIS",
                "time_stop_minutes": S2_TIME_STOP_MINUTES,
                "entry_time":       None,
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
        hammer = (last["close"] > last["open"] and
                  lo_wick >= 2 * body and hi_wick <= body * 0.5)
        engulf = (prev["close"] < prev["open"] and
                  last["close"] > last["open"] and
                  last["open"] < prev["close"] and
                  last["close"] > prev["open"])
        return hammer or engulf
